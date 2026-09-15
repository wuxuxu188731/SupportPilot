"""Loader boundary tests: decoding, Word extraction and section handling."""

import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import ZipFile

import pytest

from app.knowledge.base import (
    DocumentSourceType,
    InvalidDocumentError,
    ParsingUnavailableError,
)

from app.knowledge.document_loader import (
    MAX_DOCUMENT_BYTES,
    DocumentLoader,
    WordDocumentExtractor,
)


WORD_FIXTURES = Path(__file__).parents[2] / "TestWordDocuments"
WORD_NAMESPACE = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class FakeDocumentExtractor:
    """记录调用参数的假提取器，使 loader 测试完全离线。"""

    def __init__(self, markdown: str) -> None:
        self.markdown = markdown
        self.calls: list[tuple[int, str]] = []

    def extract(self, content: bytes, *, suffix: str) -> str:
        self.calls.append((len(content), suffix))
        return self.markdown


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


# 保护行为：DOCX 现在走外部解析，loader 必须把提取结果当 Markdown 处理，
# 按标题层级切出带 heading_path 的 section，而不是像 v1 那样压成单个无标题 section。
def test_document_loader_builds_sections_from_extracted_word_markdown():
    path = next(WORD_FIXTURES.glob("*.docx"))
    extractor = FakeDocumentExtractor("# 招聘录用制度\n\n## 第一层：总则\n\n正文内容")

    loaded = DocumentLoader(document_extractor=extractor).load(
        path.read_bytes(), DocumentSourceType.WORD
    )

    assert loaded.text == "# 招聘录用制度\n\n## 第一层：总则\n\n正文内容"
    # 只断言有正文的 section：标题后紧跟一个空行时，既有的 flush() 会额外产出
    # 一个 content 为空字符串的 section（"".isspace() 为 False，逃过了空内容
    # 检查）。这是本次改造之前就存在的行为，chunker 会跳过它，与本次改动无关。
    assert [
        section.heading_path for section in loaded.sections if section.content.strip()
    ] == ["招聘录用制度/第一层：总则"]
    # 上报给解析服务的扩展名必须与来源类型一致，服务端靠它选择解析器。
    assert extractor.calls == [(path.stat().st_size, ".docx")]


# 保护行为：PDF 与新引入的解析层对齐——走同一条提取 + 归一化路径，
# 并且归一化会把 HTML 表格转成管道表。
def test_document_loader_converts_pdf_tables_before_sectioning():
    extractor = FakeDocumentExtractor(
        "# 政策\n\n## 时限\n\n<table><tr><th>环节</th><th>时限</th></tr>"
        "<tr><td>审核</td><td>24小时</td></tr></table>\n"
    )

    loaded = DocumentLoader(document_extractor=extractor).load(
        b"%PDF-1.4 fake", DocumentSourceType.PDF
    )

    assert "<table" not in loaded.text
    assert "| 环节 | 时限 |" in loaded.text
    assert "| 审核 | 24小时 |" in loaded.text
    assert [
        section.heading_path for section in loaded.sections if section.content.strip()
    ] == ["政策/时限"]
    assert extractor.calls == [(len(b"%PDF-1.4 fake"), ".pdf")]


# 边界情况：没有配置提取器时加载 PDF/DOCX，必须报「解析能力不可用」，
# 而不是伪装成文档非法（那是把服务故障归咎于用户上传的文件）。
@pytest.mark.parametrize(
    "source_type", [DocumentSourceType.PDF, DocumentSourceType.WORD]
)
def test_loader_without_extractor_reports_parsing_unavailable(source_type):
    with pytest.raises(ParsingUnavailableError) as excinfo:
        DocumentLoader().load(b"whatever", source_type)

    assert excinfo.value.code == "PARSING_UNAVAILABLE"
    # safe_message 不得夹带密钥或上游细节。
    assert excinfo.value.safe_message == "document parsing service unavailable"


# 边界情况：解析服务返回空白内容时必须报文档为空，而不是产出一个无 section 的空文档。
def test_loader_rejects_empty_extraction_result():
    extractor = FakeDocumentExtractor("   \n\n  ")

    with pytest.raises(InvalidDocumentError):
        DocumentLoader(document_extractor=extractor).load(
            b"%PDF-1.4 fake", DocumentSourceType.PDF
        )


# 边界情况：提取结果完全没有标题时，退化成单个 heading_path 为 None 的 section，
# 与 TEXT 路径行为一致，让下游 chunker 仍能正常分块。
def test_loader_falls_back_to_single_unnamed_section_without_headings():
    extractor = FakeDocumentExtractor("只有正文，没有任何标题")

    loaded = DocumentLoader(document_extractor=extractor).load(
        b"%PDF-1.4 fake", DocumentSourceType.PDF
    )

    assert [section.heading_path for section in loaded.sections] == [None]
    assert loaded.sections[0].content == "只有正文，没有任何标题"


# 边界情况：损坏或伪造的 DOCX 字节必须转换为统一的文档校验错误。
def test_word_extractor_rejects_invalid_docx():
    with pytest.raises(InvalidDocumentError):
        WordDocumentExtractor().extract(b"not a docx package")
