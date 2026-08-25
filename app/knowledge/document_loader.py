"""Markdown/TXT loader: normalise raw bytes into a stable ``LoadedDocument``.

The loader is deliberately dependency-light: it only decodes utf-8, folds
CRLF/the bare carriage return to LF, guards the byte size, and builds a
heading-path stack from markdown ATX headings. Splitting into token-bounded
chunks happens later in ``app.knowledge.chunking``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.knowledge.base import DocumentSourceType, InvalidDocumentError

LOADER_VERSION = "supportpilot-loader-v1"
# Hard upper bound on a single document's raw byte size.
MAX_DOCUMENT_BYTES = 2 * 1024 * 1024

# ATX headings: line-initial run of 1-6 '#' then whitespace then the title.
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)\s*$")


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

    def load(
        self,
        content: bytes,
        source_type: DocumentSourceType,
    ) -> LoadedDocument:
        if len(content) > MAX_DOCUMENT_BYTES:
            raise InvalidDocumentError(reason="document exceeds maximum size")

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
