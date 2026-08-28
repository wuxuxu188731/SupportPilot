"""Markdown/TXT/Word loader: normalise bytes into ``LoadedDocument``.

The loader is deliberately dependency-light: Markdown/TXT decoding remains
local, while Word uses python-docx to validate the OOXML package and extract
all body paragraphs, including paragraphs nested in tables. Splitting into
token-bounded chunks happens later in ``app.knowledge.chunking``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from io import BytesIO
from zipfile import BadZipFile

from docx import Document
from docx.opc.exceptions import PackageNotFoundError
from docx.oxml.ns import qn

from app.knowledge.base import DocumentSourceType, InvalidDocumentError

LOADER_VERSION = "supportpilot-loader-v1"
# Hard upper bound on a single document's raw byte size.
MAX_DOCUMENT_BYTES = 2 * 1024 * 1024

# ATX headings: line-initial run of 1-6 '#' then whitespace then the title.
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)\s*$")

_WORD_TEXT_TAG = qn("w:t")
_WORD_TAB_TAG = qn("w:tab")
_WORD_BREAK_TAGS = {qn("w:br"), qn("w:cr")}


class WordDocumentExtractor:
    """Extract visible body text from a DOCX document in reading order.

    Traversing OOXML paragraphs instead of only ``Document.paragraphs`` is
    intentional: python-docx excludes paragraphs inside tables from that
    collection, while policy documents commonly place substantial content in
    table cells. Inline tabs and explicit line breaks are retained.
    """

    def extract(self, content: bytes) -> str:
        try:
            document = Document(BytesIO(content))
        except (BadZipFile, PackageNotFoundError, ValueError, KeyError) as exc:
            raise InvalidDocumentError(
                reason="document is not a valid docx"
            ) from exc

        paragraphs: list[str] = []
        for paragraph in document.element.body.iter(qn("w:p")):
            parts: list[str] = []
            for node in paragraph.iter():
                if node.tag == _WORD_TEXT_TAG:
                    parts.append(node.text or "")
                elif node.tag == _WORD_TAB_TAG:
                    parts.append("\t")
                elif node.tag in _WORD_BREAK_TAGS:
                    parts.append("\n")
            text = "".join(parts)
            if text.strip():
                paragraphs.append(text)

        extracted = "\n".join(paragraphs)
        if not extracted.strip():
            raise InvalidDocumentError(reason="document is empty")
        return extracted


@dataclass(frozen=True)
class LoadedSection:
    """A contiguous run of text that lives under one heading path.

    ``heading_path`` is ``None`` for documents without markdown headings
    (TEXT) and for content that precedes the first heading.
    """

    heading_path: str | None
    content: str


@dataclass(frozen=True)
class LoadedDocument:
    """Normalised document text plus a lightweight heading outline."""

    text: str
    sections: tuple[LoadedSection, ...]


class DocumentLoader:
    """Turns raw uploaded bytes into a normalised, sectioned document."""

    def __init__(
        self,
        *,
        word_extractor: WordDocumentExtractor | None = None,
    ) -> None:
        self._word_extractor = word_extractor or WordDocumentExtractor()

    def load(
        self,
        content: bytes,
        source_type: DocumentSourceType,
    ) -> LoadedDocument:
        if len(content) > MAX_DOCUMENT_BYTES:
            raise InvalidDocumentError(reason="document exceeds maximum size")

        if source_type is DocumentSourceType.WORD:
            text = self._word_extractor.extract(content)
            return LoadedDocument(
                text=text,
                sections=(LoadedSection(heading_path=None, content=text),),
            )

        try:
            text = content.decode("utf-8")
        except (UnicodeDecodeError, UnicodeError):
            raise InvalidDocumentError(reason="document is not valid utf-8")

        text = text.replace("\r\n", "\n").replace("\r", "\n")

        if not text.strip():
            raise InvalidDocumentError(reason="document is empty")

        if source_type is DocumentSourceType.MARKDOWN:
            sections = self._extract_markdown_sections(text)
        else:
            sections = (LoadedSection(heading_path=None, content=text),)

        return LoadedDocument(text=text, sections=sections)

    @staticmethod
    def _extract_markdown_sections(text: str) -> tuple[LoadedSection, ...]:
        """Build heading paths and section bounds from ATX headings.

        The stack holds ``(level, title)`` for every heading currently open.
        A heading at level ``N`` truncates the stack to headings strictly
        shallower than ``N`` (so siblings at the same level reset the path)
        then appends itself. Each chunk of non-heading text is flushed under
        the current path at the moment the following heading (or EOF) opens.
        """
        sections: list[LoadedSection] = []
        stack: list[tuple[int, str]] = []
        buffer: list[str] = []

        def flush() -> None:
            if not buffer:
                return
            content = "\n".join(buffer)
            # Skip whitespace-only runs (e.g. a heading with no body text) so
            # a heading-only document yields no sections and the chunker can
            # convert that into InvalidDocumentError.
            if content.isspace():
                buffer.clear()
                return
            path = "/".join(title for _, title in stack) or None
            sections.append(LoadedSection(heading_path=path, content=content))
            buffer.clear()

        for line in text.split("\n"):
            match = _HEADING_RE.match(line)
            if match is not None:
                flush()
                level = len(match.group(1))
                title = match.group(2).strip()
                while stack and stack[-1][0] >= level:
                    stack.pop()
                stack.append((level, title))
            else:
                buffer.append(line)

        flush()
        return tuple(sections)
