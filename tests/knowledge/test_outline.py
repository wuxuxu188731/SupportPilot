"""版本正文标题目录提取的单元测试。

目录偏移与知识块的字符偏移必须同源同口径，否则前端要么滚错位置，要么
"引用无精确偏移时跳到所属章节"的降级路径失效。
"""

from app.knowledge.outline import DocumentOutlineItem, extract_outline


def test_extract_outline_reports_level_title_path_and_char_offset():
    # 保护行为：目录项给出层级、标题文本、完整标题路径，以及标题行首在
    # 正文中的绝对字符偏移（Python 字符计数；中文一个字算 1）。
    text = "# 退货总则\n\n正文一段。\n\n## 3.2 退货流程\n\n签收后 7 日内可申请退货。"

    outline = extract_outline(text)

    assert outline == (
        DocumentOutlineItem(
            level=1,
            title="退货总则",
            heading_path="退货总则",
            char_offset=0,
        ),
        DocumentOutlineItem(
            level=2,
            title="3.2 退货流程",
            heading_path="退货总则/3.2 退货流程",
            char_offset=text.index("## 3.2 退货流程"),
        ),
    )
    # 偏移必须真的落在该标题行首，而不是"差不多"。
    for item in outline:
        assert text[item.char_offset :].startswith("#" * item.level)


def test_extract_outline_pops_same_or_shallower_levels():
    # 边界情况：同级标题必须重置路径（不继承上一个同级标题），更浅的标题
    # 要把更深的层级全部弹出——与 loader 的标题栈语义一致。
    text = "# A\na\n## B\nb\n## C\nc\n# D\nd\n### E\ne"

    paths = [item.heading_path for item in extract_outline(text)]

    assert paths == ["A", "A/B", "A/C", "D", "D/E"]


def test_extract_outline_ignores_non_heading_lines():
    # 边界情况：不是标题的行不得被当成标题；'#' 后没有空格（如 '#标签'）、
    # 超过 6 个 '#'、行中出现 '#' 都不算 ATX 标题（规则与 loader 完全一致）。
    text = "#标签\n####### 七个井号\n正文里的 # 井号\n# 真标题"

    outline = extract_outline(text)

    assert [(item.title, item.char_offset) for item in outline] == [
        ("真标题", text.index("# 真标题"))
    ]


def test_extract_outline_strips_trailing_whitespace_from_title():
    # 边界情况：标题尾部空白属于行内噪声，不进入 title（与 loader 一致）。
    outline = extract_outline("# 退货总则   \n\n正文")

    assert outline[0].title == "退货总则"


def test_extract_outline_returns_empty_for_text_without_headings():
    # 边界情况：TXT 等没有 Markdown 标题的文档返回空目录，而不是抛错
    # 或伪造一个标题为空的条目。
    assert extract_outline("第一行\n第二行") == ()
    assert extract_outline("") == ()


def test_extract_outline_char_offset_counts_newlines_as_one_char():
    # 保护行为：偏移必须与切块使用的同一套字符计数一致——换行符算 1 个字符，
    # 且不因为"最后一行没有换行"而漂移。
    text = "第一行\n第二行\n# 标题\n正文"

    outline = extract_outline(text)

    assert outline[0].char_offset == len("第一行\n第二行\n")
    assert text[outline[0].char_offset :] == "# 标题\n正文"
