"""Demo: the two third-party splitter problems documented in chunking.py."""

from langchain_text_splitters import (
    MarkdownHeaderTextSplitter,
    RecursiveCharacterTextSplitter,
)

print("=" * 70)
print("问题 1: MarkdownHeaderTextSplitter 会改写源文本")
print("=" * 70)

markdown = """# 项目简介

这里有一些内容，包含\t制表符。

## 详细说明

第二段内容。

第三段内容。"""

print("--- 原始文本 (repr 显示空白字符) ---")
for line in markdown.split("\n"):
    print(repr(line))

header_splitter = MarkdownHeaderTextSplitter(
    headers_to_split_on=[("#", "H1"), ("##", "H2")]
)
docs = header_splitter.split_text(markdown)
print("\n--- splitter 输出 ---")
for d in docs:
    print(f"metadata={d.metadata}")
    print(f"page_content={d.page_content!r}")
    print()

# 验证: page_content 是否是原始文本的逐字子串
print("--- 验证 page_content 是否为原始文本的子串 ---")
for d in docs:
    content = d.page_content
    found = markdown.find(content)
    print(f"find('{content[:20]}...') -> {found}, len={len(content)}")
    if found >= 0:
        slice_ok = markdown[found : found + len(content)] == content
        print(f"  切片复现 == content ? {slice_ok}")
    else:
        print("  在原始文本中根本找不到该内容!")

print()
print("=" * 70)
print("问题 2: RecursiveCharacterTextSplitter 字符级重叠 + find() 重定位")
print("=" * 70)

# 模拟重复性文本(日志): 同一行出现多次
log_text = (
    "2026-08-10 INFO request ok\n"
    * 3
    + "2026-08-10 ERROR boom\n"
    + "2026-08-10 INFO request ok\n" * 3
)
print("--- 文本(每行出现多次, 极具重复性) ---")
print(log_text)

splitter = RecursiveCharacterTextSplitter(
    chunk_size=40,
    chunk_overlap=10,
    keep_separator=False,
)
pieces = splitter.split_text(log_text)
print("\n--- splitter 输出片段(带 overlap) ---")
for i, p in enumerate(pieces):
    print(f"piece[{i}] = {p!r}")

print("\n--- 天真做法: 用 find() 把每个片段重定位回原始文本 ---")
cursor = 0
for i, p in enumerate(pieces):
    idx = log_text.find(p, cursor)
    print(
        f"piece[{i}] find 于 offset={idx} "
        f"(期望:{'在第' + str(idx) + '个位置' if idx >= 0 else '未找到'})"
    )
    if idx >= 0:
        cursor = idx + len(p)

# 展示坍缩: 找出的位置是否单调递增且互不重叠
print("\n--- 模拟一个真正重复的文本: 'aaaaaa' chunk_size=4 overlap=2 ---")
rep = "aaaaaa"
rep_splitter = RecursiveCharacterTextSplitter(
    chunk_size=4, chunk_overlap=2, keep_separator=False
)
rep_pieces = rep_splitter.split_text(rep)
print(f"splitter 输出: {rep_pieces!r}")
print(f"真实位置: 第一个 'aaaa' 在 [0,4), 第二个 'aaaa' 在 [2,6)")
print(f"find('aaaa') 每次都返回 {rep.find('aaaa')}  -> 第二个片段坍缩到了开头!")

print()
print("=" * 70)
print("附加: 分离符被吞掉 -> sum(len(piece)) < len(text)")
print("=" * 70)
sep_text = "第一段。\n\n第二段超级长必须被再次切分的内容。\n\n第三段。"
sep_splitter = RecursiveCharacterTextSplitter(
    chunk_size=10, chunk_overlap=0, keep_separator=False
)
sep_pieces = sep_splitter.split_text(sep_text)
print(f"原文长度    : {len(sep_text)}")
print(f"片段长度总和 : {sum(len(p) for p in sep_pieces)}")
print(f"片段内容    : {sep_pieces!r}")
print("=> 中间段落太长被递归切分后, 它两侧的 '\\n\\n' 分隔符从输出中消失了")
print("   (sum(len) < len), 纯算术游标会因此把后续偏移算错")
