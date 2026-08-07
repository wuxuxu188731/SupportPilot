"""Stable, deterministic chunker over a normalised ``LoadedDocument``.

Design notes
------------
The brief prescribes ``MarkdownHeaderTextSplitter`` to preserve heading paths
followed by a ``RecursiveCharacterTextSplitter.from_tiktoken_encoder()``
secondary split. Two third-party details matter here:

1. ``MarkdownHeaderTextSplitter`` *rewrites* the source (it normalises heading
   separators to two spaces and drops blank lines), so its ``page_content`` is
   not a literal substring of the normalised document. Trusting ``find()`` on
   its output cannot satisfy the hard stability contract
   ``content == document.text[start_offset:end_offset]``.

2. ``RecursiveCharacterTextSplitter`` with *character* overlap produces adjacent
   pieces whose text overlaps. Re-locating such pieces by forward ``find()`` is
   ambiguous on repetitive text (logs, repeated sentences): the same piece can
   match at an earlier offset, collapsing every chunk toward the document start.

This chunker therefore never re-searches splitter output. It:

* derives exact section spans and heading paths from the loader, whose sections
  partition the *normalised* text exactly (regex-derived heading stack);
* asks ``RecursiveCharacterTextSplitter`` to split each section with
  ``chunk_overlap=0``, producing a clean non-overlapping partition whose offsets
  are known arithmetically by a running cursor (unambiguous on any text);
* reintroduces the brief's ``CHUNK_OVERLAP_TOKENS`` overlap by widening each
  chunk's start back into the previous chunk's tail, keeping
  ``content == text[start:end]`` exact by construction.

Token counts use the same ``cl100k_base`` encoder the brief's
``from_tiktoken_encoder()`` splitter uses.
"""

from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

import tiktoken
from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.knowledge.base import DocumentChunk, InvalidDocumentError
from app.knowledge.document_loader import LoadedDocument

CHUNKER_VERSION = "supportpilot-chunker-v1"
TARGET_CHUNK_TOKENS = 600
MAX_CHUNK_TOKENS = 700
CHUNK_OVERLAP_TOKENS = 80


class KnowledgeChunker:
    """Splits a normalised document into deterministic, token-bounded chunks."""

    def __init__(self) -> None:
        # Secondary split: natural-boundary, token-bounded pieces using the
        # exact cl100k_base encoder the brief prescribes. Splitter-managed
        # overlap is disabled (we widen chunk starts ourselves so offsets stay
        # exact) - see module docstring.
        self._splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
            encoding_name="cl100k_base",
            chunk_size=TARGET_CHUNK_TOKENS,
            chunk_overlap=0,
        )
        self._enc = tiktoken.get_encoding("cl100k_base")

    def _tokens(self, text_part: str) -> int:
        return len(self._enc.encode(text_part))

    def split(
        self,
        document: LoadedDocument,
        *,
        organization_id: str,
        document_id: str,
        version_id: str,
    ) -> list[DocumentChunk]:
        text = document.text
        sections = document.sections
        if not sections:
            raise InvalidDocumentError(reason="document has no sectionable content")

        # Resolve each section to its exact [start, end) span in the normalised
        # text by forward-searching from the previous section's end. Sections
        # partition the text, so this is exact and deterministic.
        bounds: list[tuple[str | None, int, int]] = []
        search_from = 0
        for section in sections:
            start = text.find(section.content, search_from)
            if start < 0:
                raise InvalidDocumentError(
                    reason="section text lost during normalisation"
                )
            end = start + len(section.content)
            if end > start:
                bounds.append((section.heading_path, start, end))
            search_from = end

        if not bounds:
            raise InvalidDocumentError(reason="no sectionable content")

        chunks: list[DocumentChunk] = []
        ordinal = 0
        for heading_path, section_start, section_end in bounds:
            section_text = text[section_start:section_end]
            cursor = section_start  # running absolute cursor over the partition
            for piece in self._splitter.split_text(section_text):
                if not piece:
                    continue
                if piece.isspace():
                    cursor += len(piece)
                    continue
                base_start = cursor
                # Re-include the previous chunk's tail (CHUNK_OVERLAP_TOKENS
                # token overlap) so adjacent chunks stay contiguous-context.
                if chunks:
                    overlap = self._overlap_start(
                        text, section_start, cursor
                    )
                    if overlap is not None and overlap < base_start:
                        base_start = overlap
                end = cursor + len(piece)
                if base_start >= end:
                    cursor = end
                    continue
                chunks.append(
                    DocumentChunk(
                        chunk_id=self._chunk_id(
                            organization_id, version_id, ordinal
                        ),
                        organization_id=organization_id,
                        document_id=document_id,
                        version_id=version_id,
                        ordinal=ordinal,
                        heading_path=heading_path,
                        content=text[base_start:end],
                        token_count=self._tokens(text[base_start:end]),
                        start_offset=base_start,
                        end_offset=end,
                    )
                )
                cursor = end
                ordinal += 1

        if not chunks:
            raise InvalidDocumentError(reason="splitter produced no chunks")
        return chunks

    def _overlap_start(
        self,
        text: str,
        section_start: int,
        cursor: int,
    ) -> int | None:
        """Absolute offset re-including the last ``CHUNK_OVERLAP_TOKENS``
        tokens of ``text[section_start:cursor]`` (the previously emitted chunk).

        Returns ``None`` when there is nothing to overlap (first chunk in its
        section) or the previous chunk is too short.
        """
        if cursor <= section_start:
            return None
        encoded = self._enc.encode(text[section_start:cursor])
        take = min(len(encoded), CHUNK_OVERLAP_TOKENS)
        if take <= 0:
            return None
        tail_chars = len(self._enc.decode(encoded[len(encoded) - take:]))
        return cursor - tail_chars

    @staticmethod
    def _chunk_id(organization_id: str, version_id: str, ordinal: int) -> str:
        # Stable point id for a given tenant/version/ordinal: re-running
        # ingestion for the same version yields identical ids.
        return str(
            uuid5(NAMESPACE_URL, f"{organization_id}:{version_id}:{ordinal}")
        )
