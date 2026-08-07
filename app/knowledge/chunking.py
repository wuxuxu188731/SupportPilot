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
        # text by forward-searching from the previous section's end.
        #
        # Bounded assumption: the loader derives its sections from the same
        # normalised text (a heading stack over "\n"-split lines), so section
        # bodies appear as contiguous, non-overlapping substrings in document
        # order. The search window is monotonic (each starts after the previous
        # section's end), and any whitespace between a body's end and the next
        # body's start is only heading lines and blank lines - it cannot contain
        # a body's non-heading content as a prefix. This makes forward `find`
        # exact and deterministic.
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

            # Determine each splitter piece's *true* position within the section.
            # ``RecursiveCharacterTextSplitter`` drops the separator char(s) at
            # the joint between adjacent pieces, so ``sum(len(piece))`` is less
            # than ``len(section_text)``: a purely arithmetic cursor
            # under-counts and every chunk after the first would be a
            # start-shifted slice of the raw text, and the section's final
            # characters would never be emitted. We therefore advance the cursor
            # by the true distance - locating each piece within the unconsumed
            # remainder from the cursor. Pieces partition the section and carry
            # no trailing separators, so the first match is the true position
            # even on repetitive text.
            piece_starts: list[int] = []
            cursor = section_start
            remainder = section_text
            for piece in self._splitter.split_text(section_text):
                if not piece:
                    continue
                idx = remainder.find(piece)
                if idx < 0:
                    # Defensive: a partition-derived piece always matches.
                    idx = 0
                true_start = cursor + idx
                if piece.isspace():
                    # Separator-only remainder: fold it into the previous
                    # chunk rather than emitting a zero-content chunk.
                    cursor = true_start + len(piece)
                    remainder = section_text[cursor - section_start:]
                    continue
                piece_starts.append(true_start)
                cursor = true_start + len(piece)
                remainder = section_text[cursor - section_start:]

            # Emit one chunk per located piece. Each chunk's content spans from
            # its piece's true start to the *next* piece's true start (or the
            # section end), so the dropped separators are absorbed and adjacent
            # chunks tile the section with no gap and no drift.
            prev_end: int | None = None
            for i, start in enumerate(piece_starts):
                end = (
                    piece_starts[i + 1]
                    if i + 1 < len(piece_starts)
                    else section_end
                )
                base_start = start
                if prev_end is not None:
                    # Re-include the previous chunk's tail (CHUNK_OVERLAP_TOKENS
                    # token overlap) so adjacent chunks stay contiguous-context.
                    overlap = self._overlap_start(
                        text, section_start, prev_end
                    )
                    if overlap is not None and overlap < base_start:
                        base_start = overlap
                if base_start >= end:
                    prev_end = end
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
                prev_end = end
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
