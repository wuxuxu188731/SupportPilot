"""Markdown/TXT/Word/PDF loader: normalise bytes into ``LoadedDocument``.

两条提取路径：

* **本地解码**：Markdown/TXT 直接 UTF-8 解码，零外部依赖；
* **外部解析**：DOCX/PDF 交给注入的 :class:`~app.knowledge.llamaparse_extractor.DocumentExtractor`
  提取，再经 :mod:`app.knowledge.markdown_normalizer` 归一化成项目规范的
  Markdown（HTML 表 → 管道表），最后与原生 Markdown 走同一套标题栈切分。

两条路径最终都产出带 ``heading_path`` 的 section，因此下游 chunker
与检索完全不需要区分文档来源。

历史说明：``WordDocumentExtractor`` 是本项目早期的本地 DOCX 提取实现。
DOCX 改走外部解析后它已不再被 ``DocumentLoader`` 使用，但予以保留——
它是一份不依赖网络的降级实现，也是 OOXML 直读行为的参考实现。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from io import BytesIO
from zipfile import BadZipFile

from docx import Document
from docx.opc.exceptions import PackageNotFoundError
from docx.oxml.ns import qn

from app.knowledge.base import (
    DocumentSourceType,
    InvalidDocumentError,
    ParsingUnavailableError,
)
from app.knowledge.llamaparse_extractor import DocumentExtractor
from app.knowledge.markdown_normalizer import normalize_extracted_markdown

# 升到 v2：同一份 DOCX 在 v1（本地 OOXML 直读、无标题层级）与 v2（外部解析、
# 带标题层级）下会产出不同的 chunk，版本号必须随之变化，否则历史版本的
# 可追溯性会被破坏。
LOADER_VERSION = "supportpilot-loader-v2"
# Hard upper bound on a single document's raw byte size.
MAX_DOCUMENT_BYTES = 2 * 1024 * 1024

# ATX headings: line-initial run of 1-6 '#' then whitespace then the title.
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)\s*$")

_WORD_TEXT_TAG = qn("w:t")
_WORD_TAB_TAG = qn("w:tab")
_WORD_BREAK_TAGS = {qn("w:br"), qn("w:cr")}

# 需要外部解析的格式 -> 上报给解析服务的文件扩展名（服务端据此选择解析器）。
_REMOTE_SUFFIX_BY_SOURCE_TYPE = {
    DocumentSourceType.WORD: ".docx",
    DocumentSourceType.PDF: ".pdf",
}


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
        document_extractor: DocumentExtractor | None = None,
    ) -> None:
        # DOCX/PDF 依赖外部解析服务，因此显式注入而不是内部构造：没有配置
        # 提取器时，加载这类文档会明确报「解析能力不可用」，而不是悄悄
        # 退回一个内容残缺的本地解析结果。
        self._document_extractor = document_extractor

    def load(
        self,
        content: bytes,
        source_type: DocumentSourceType,
    ) -> LoadedDocument:
        if len(content) > MAX_DOCUMENT_BYTES:
            raise InvalidDocumentError(reason="document exceeds maximum size")

        if source_type in _REMOTE_SUFFIX_BY_SOURCE_TYPE:
            return self._load_extracted(content, source_type)

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

    def _load_extracted(
        self,
        content: bytes,
        source_type: DocumentSourceType,
    ) -> LoadedDocument:
        """走外部解析 + 归一化，再复用 Markdown 的标题栈切分。

        归一化后与原生 Markdown 完全同构，因此 section 划分、heading_path
        与偏移定位都能直接复用同一套逻辑，下游 chunker 无需感知文档来源。
        """
        if self._document_extractor is None:
            raise ParsingUnavailableError(
                reason="no document extractor is configured for this loader"
            )

        raw_markdown = self._document_extractor.extract(
            content,
            suffix=_REMOTE_SUFFIX_BY_SOURCE_TYPE[source_type],
        )
        text = normalize_extracted_markdown(raw_markdown)
        text = text.replace("\r\n", "\n").replace("\r", "\n")

        if not text.strip():
            raise InvalidDocumentError(reason="document is empty")

        # 提取结果没有标题时，_extract_markdown_sections 会给出一个
        # heading_path 为 None 的 section，与 TEXT 路径的行为一致。
        sections = self._extract_markdown_sections(text)
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
