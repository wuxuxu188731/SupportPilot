"""归一化层测试：HTML 表格 → Markdown 管道表。

测试数据分两类：
* 内联 HTML 片段——覆盖解析器的各条分支与边界（缺表头、列数不齐、单元格含
  竖线、多表格、无表格等）；
* 真实 LlamaParse 产物 fixture（``tests/knowledge/fixtures/llamaparse/``）——
  它们是本次评估实际抓下来的原始输出，docx 与 pdf 两种来源的表格 HTML
  结构不同（pdf 带 ``<thead>/<tbody>``，docx 是裸 ``<tr>``），必须都能吃下。
"""

from pathlib import Path

import tiktoken

from app.knowledge.base import DocumentSourceType
from app.knowledge.document_loader import DocumentLoader
from app.knowledge.markdown_normalizer import normalize_extracted_markdown

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "llamaparse"
# 基准文档：docx/pdf 两个 fixture 都是由它经 pandoc 转换、再经 LlamaParse 解析得来。
REFERENCE_MARKDOWN = Path(__file__).parents[2] / "docs" / "knowledge" / "01-售后服务总则.md"


def test_html_table_with_header_becomes_pipe_table():
    # 保护行为：带 <th> 表头的 HTML 表格必须转成「表头 + 分隔行 + 数据行」的管道表，
    # 单元格文字保持原样。
    raw = (
        "正文前\n\n"
        "<table>\n"
        "  <tr><th>环节</th><th>时限</th></tr>\n"
        "  <tr><td>审核响应</td><td>不超过24小时</td></tr>\n"
        "</table>\n\n"
        "正文后\n"
    )

    converted = normalize_extracted_markdown(raw)

    assert "| 环节 | 时限 |" in converted
    assert "| --- | --- |" in converted
    assert "| 审核响应 | 不超过24小时 |" in converted
    assert "<table" not in converted
    assert "正文前" in converted and "正文后" in converted


def test_thead_tbody_table_becomes_pipe_table():
    # 保护行为：pdf 来源的表格带 <thead>/<tbody> 包裹，结构标签必须被正确忽略，
    # 不能把 thead/tbody 当成额外的行。
    raw = (
        "<table>\n"
        "  <thead>\n"
        "    <tr><th>专项政策</th><th>处理事项</th></tr>\n"
        "  </thead>\n"
        "  <tbody>\n"
        "    <tr><td>《退款政策》</td><td>退款方式</td></tr>\n"
        "  </tbody>\n"
        "</table>\n"
    )

    converted = normalize_extracted_markdown(raw)

    assert converted.count("| --- |") == 1
    assert "| 《退款政策》 | 退款方式 |" in converted


def test_table_without_header_promotes_first_row():
    # 边界情况：整张表没有任何 <th> 时，Markdown 管道表在语法上仍必须有表头行，
    # 因此首行被提升为表头，且不得因此丢失任何数据行。
    raw = (
        "<table>\n"
        "  <tr><td>甲</td><td>乙</td></tr>\n"
        "  <tr><td>丙</td><td>丁</td></tr>\n"
        "</table>\n"
    )

    converted = normalize_extracted_markdown(raw)

    assert converted.strip().splitlines() == [
        "| 甲 | 乙 |",
        "| --- | --- |",
        "| 丙 | 丁 |",
    ]


def test_ragged_rows_are_padded_to_the_widest_row():
    # 边界情况：某行列数少于其它行时按最宽行补齐空单元格，
    # 否则 Markdown 表格会因列数不一致而错位。
    raw = (
        "<table>\n"
        "  <tr><th>甲</th><th>乙</th><th>丙</th></tr>\n"
        "  <tr><td>1</td></tr>\n"
        "</table>\n"
    )

    converted = normalize_extracted_markdown(raw)

    assert "| 1 |  |  |" in converted


def test_pipe_inside_cell_is_escaped():
    # 边界情况：单元格文本里的竖线必须转义，否则会被 Markdown 解析成多余的列。
    raw = (
        "<table>\n"
        "  <tr><th>说明</th></tr>\n"
        "  <tr><td>丢件|破损</td></tr>\n"
        "</table>\n"
    )

    converted = normalize_extracted_markdown(raw)

    assert r"| 丢件\|破损 |" in converted


def test_cell_whitespace_is_collapsed():
    # 边界情况：单元格内的换行与连续空格压成单个空格，避免把排版换行带进管道表。
    raw = (
        "<table>\n"
        "  <tr><th>环节</th></tr>\n"
        "  <tr><td>\n      售后申请\n      审核响应\n  </td></tr>\n"
        "</table>\n"
    )

    converted = normalize_extracted_markdown(raw)

    assert "| 售后申请 审核响应 |" in converted


def test_text_without_table_is_returned_unchanged():
    # 边界情况：没有表格的文本必须原样返回——归一化只处理它真正转换过的部分，
    # 不做任何顺带的「清洗」。
    raw = "# 标题\n\n正文一\n\n正文二\n\n\n\n正文三\n"

    assert normalize_extracted_markdown(raw) == raw


def test_multiple_tables_are_all_converted():
    # 保护行为：一篇文档里的多张表格都要转换，不能只处理第一张。
    raw = (
        "<table><tr><th>A</th></tr><tr><td>1</td></tr></table>\n\n"
        "中间正文\n\n"
        "<table><tr><th>B</th></tr><tr><td>2</td></tr></table>\n"
    )

    converted = normalize_extracted_markdown(raw)

    assert "| A |" in converted and "| B |" in converted
    assert "中间正文" in converted
    assert "<table" not in converted


def test_empty_table_produces_no_table_text():
    # 边界情况：空的 <table></table> 不应产出一张只有分隔行的畸形表格。
    raw = "前\n\n<table></table>\n\n后\n"

    converted = normalize_extracted_markdown(raw)

    assert "---" not in converted
    assert "前" in converted and "后" in converted


def test_real_llamaparse_fixtures_convert_to_pipe_tables():
    # 保护行为：docx 与 pdf 两种真实 LlamaParse 产物都必须被完整转换，
    # 表格单元格文字逐字保留，且不再残留任何 HTML 表格标签。
    for fixture_name in (
        "service-policy.docx.raw.md",
        "service-policy.pdf.raw.md",
    ):
        raw = (FIXTURE_DIR / fixture_name).read_text(encoding="utf-8")

        converted = normalize_extracted_markdown(raw)

        assert "<table" not in converted, fixture_name
        assert "</td>" not in converted, fixture_name
        # 两张表的表头与逐行数据都在（docx 无空格、pdf 有空格，分别断言）。
        assert "| 专项政策 | 处理事项 |" in converted, fixture_name
        assert "| 环节 | 时限 |" in converted, fixture_name
        assert "| 售后申请审核响应 |" in converted, fixture_name
        assert "《退货与换货政策》" in converted, fixture_name


def test_fixture_normalises_to_the_same_heading_paths_as_native_markdown():
    # 保护行为：归一化后的提取结果，经真实 DocumentLoader 切分后，
    # heading_path 集合必须与原生 Markdown 完全一致——这是本项目 RAG
    # 证据定位的硬契约，也是整个 loader 改造的核心目标。
    loader = DocumentLoader()
    reference_paths = [
        section.heading_path
        for section in loader.load(
            REFERENCE_MARKDOWN.read_bytes(), DocumentSourceType.MARKDOWN
        ).sections
    ]

    for fixture_name in (
        "service-policy.docx.raw.md",
        "service-policy.pdf.raw.md",
    ):
        raw = (FIXTURE_DIR / fixture_name).read_text(encoding="utf-8")
        loaded = loader.load(
            normalize_extracted_markdown(raw).encode("utf-8"),
            DocumentSourceType.MARKDOWN,
        )

        assert [section.heading_path for section in loaded.sections] == reference_paths


def test_normalisation_removes_the_table_token_overhead():
    # 保护行为：转换必须把 HTML 表格的 token 开销压回原生 Markdown 的水平。
    # 评估实测 docx 2601→2251、pdf 2534→2262，原生基准为 2264；
    # 这里留 3% 余量断言，既能抓住「转换失效」的回归，又不会因细微差异误报。
    encoding = tiktoken.get_encoding("cl100k_base")
    reference_tokens = len(encoding.encode(REFERENCE_MARKDOWN.read_text(encoding="utf-8")))

    for fixture_name in (
        "service-policy.docx.raw.md",
        "service-policy.pdf.raw.md",
    ):
        raw = (FIXTURE_DIR / fixture_name).read_text(encoding="utf-8")
        converted = normalize_extracted_markdown(raw)

        assert len(encoding.encode(converted)) <= reference_tokens * 1.03, fixture_name
