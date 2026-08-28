"""Loader boundary tests: decoding, Word extraction and section handling."""

import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import ZipFile

import pytest

from app.knowledge.base import DocumentSourceType, InvalidDocumentError

from app.knowledge.document_loader import (
    MAX_DOCUMENT_BYTES,
    DocumentLoader,
    WordDocumentExtractor,
)


WORD_FIXTURES = Path(__file__).parents[2] / "TestWordDocuments"
WORD_NAMESPACE = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def test_markdown_loader_normalizes_newlines_and_headings():
    loaded = DocumentLoader().load(
        b"# Returns\r\n\r\nBody\r\n## Exceptions\rMore",
        DocumentSourceType.MARKDOWN,
    )
    assert loaded.text == "# Returns\n\nBody\n## Exceptions\nMore"
    assert [section.heading_path for section in loaded.sections] == [
        "Returns",
        "Returns/Exceptions",
    ]


def test_markdown_loader_heading_stack_resets_on_higher_level():
    # H3 nested under H2 keeps the full ancestor path; a later H1 resets the
    # stack back to the root. Sections are emitted only for text-bearing
    # bodies, so heading-only levels collapse onto the deepest body.
    loaded = DocumentLoader().load(
        b"# A\n## B\n### C\nbody\n# A2\nbody2",
        DocumentSourceType.MARKDOWN,
    )
    assert [s.heading_path for s in loaded.sections] == [
        "A/B/C",
        "A2",
    ]


def test_text_loader_builds_single_unnamed_section():
    loaded = DocumentLoader().load(b"just\nsome\ntext", DocumentSourceType.TEXT)
    assert loaded.text == "just\nsome\ntext"
    assert [s.heading_path for s in loaded.sections] == [None]


def test_text_loader_normalizes_bare_carriage_returns():
    loaded = DocumentLoader().load(b"one\rtwo\r\nthree", DocumentSourceType.TEXT)
    assert loaded.text == "one\ntwo\nthree"


@pytest.mark.parametrize("content", [b"", b" \r\n\t"])
def test_loader_rejects_empty_document(content):
    with pytest.raises(InvalidDocumentError):
        DocumentLoader().load(content, DocumentSourceType.TEXT)


def test_loader_rejects_invalid_utf8():
    with pytest.raises(InvalidDocumentError):
        DocumentLoader().load(b"\xff\xfe", DocumentSourceType.TEXT)


def test_loader_rejects_oversized_document():
    content = b"x" * (MAX_DOCUMENT_BYTES + 1)
    with pytest.raises(InvalidDocumentError):
        DocumentLoader().load(content, DocumentSourceType.TEXT)


def test_loader_accepts_exactly_max_size_document():
    content = b"x" * MAX_DOCUMENT_BYTES
    loaded = DocumentLoader().load(content, DocumentSourceType.TEXT)
    assert loaded.text == "x" * MAX_DOCUMENT_BYTES


# 保护行为：Word 提取器必须覆盖样例正文和全部表格单元格，且保持文字顺序。
def test_word_extractor_reads_all_text_from_sample_document():
    path = next(WORD_FIXTURES.glob("*.docx"))

    extracted = WordDocumentExtractor().extract(path.read_bytes())

    with ZipFile(path) as package:
        document_xml = ET.fromstring(package.read("word/document.xml"))
    expected_text = "".join(
        node.text or "" for node in document_xml.iter(f"{WORD_NAMESPACE}t")
    )
    assert extracted.replace("\n", "").replace("\t", "") == expected_text
    assert "招聘录用制度" in extracted
    assert "第一层：总则" in extracted


# 保护行为：DocumentLoader 应把 Word 全文包装成单个未命名 section。
def test_document_loader_builds_single_section_for_word_document():
    path = next(WORD_FIXTURES.glob("*.docx"))

    loaded = DocumentLoader().load(path.read_bytes(), DocumentSourceType.WORD)

    assert loaded.text
    assert len(loaded.sections) == 1
    assert loaded.sections[0].heading_path is None
    assert loaded.sections[0].content == loaded.text


# 边界情况：损坏或伪造的 DOCX 字节必须转换为统一的文档校验错误。
def test_word_extractor_rejects_invalid_docx():
    with pytest.raises(InvalidDocumentError):
        WordDocumentExtractor().extract(b"not a docx package")
