"""版本正文的标题目录提取（纯函数，无 IO）。

用途：``GET /knowledge/documents/{id}/versions/{id}/content/`` 需要把版本正文
（``document_versions.raw_text``，归一化 Markdown）的标题结构连同**字符偏移**
一起返回，前端据此渲染目录、并在引用缺少精确偏移时降级为「跳到所属章节」。

与 :mod:`app.knowledge.document_loader` 的关系：标题识别规则必须与切块时的
section 划分完全一致，否则目录偏移与知识块的 ``heading_path`` 会对不上。
因此本模块保持与 loader **同一套正则**（``^(#{1,6})[ \\t]+(.+?)\\s*$``）和
**同一套标题栈语义**：

* 同层或更深层的标题会先弹出栈，因此 ``heading_path`` 表达的是"完整层级链"；
* ``heading_path`` **包含该标题自身**，与 loader 给 section 赋的
  ``"/".join(title for _, title in stack)`` 语义一致（例如 ``"3 退货总则/3.2 退货流程"``），
  这样前端可以用引用的 ``heading_path`` 反查目录项。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ATX 标题：行首 1-6 个 '#'，随后至少一个空白，再是标题文本。
# 必须与 document_loader._HEADING_RE 保持逐字一致（见模块文档字符串）。
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)\s*$")

# 标题路径的分隔符，与 loader 生成 section heading_path 时使用的分隔符一致。
_PATH_SEPARATOR = "/"


@dataclass(frozen=True)
class DocumentOutlineItem:
    """正文中的一个标题（目录项）。"""

    # 标题层级：1-6，对应 Markdown 中 '#' 的个数
    level: int
    # 标题文本（已去掉行首 '#' 与首尾空白）
    title: str
    # 完整标题路径（各级标题以 '/' 连接，且包含本标题自身）；
    # 与知识块的 heading_path 同一口径，便于按引用反查章节
    heading_path: str
    # 该标题所在行首字符在正文中的绝对偏移（Python 字符计数，非字节、非行号）
    char_offset: int


def extract_outline(text: str) -> tuple[DocumentOutlineItem, ...]:
    """提取 ``text`` 中的 ATX 标题及其行首字符偏移。

    偏移口径与知识块的 ``start_offset``/``end_offset`` 完全一致：都是同一个
    字符串上的 Python 字符下标。换行计 1 个字符（loader 已把 ``\\r\\n`` 归一化为
    ``\\n``）。没有标题、或传入空串时返回空元组。
    """
    items: list[DocumentOutlineItem] = []
    # 栈中保存当前打开的各层标题 (level, title)，用于拼出完整路径。
    stack: list[tuple[int, str]] = []
    # 逐行扫描时维护的"下一行行首"字符偏移。
    char_offset = 0

    for raw_line in text.split("\n"):
        match = _HEADING_RE.match(raw_line)
        if match is not None:
            level = len(match.group(1))
            title = match.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            items.append(
                DocumentOutlineItem(
                    level=level,
                    title=title,
                    heading_path=_PATH_SEPARATOR.join(
                        open_title for _, open_title in stack
                    ),
                    char_offset=char_offset,
                )
            )
        # 换行符本身占 1 个字符；最后一行没有换行符时不影响已产出的偏移。
        char_offset += len(raw_line) + 1

    return tuple(items)
