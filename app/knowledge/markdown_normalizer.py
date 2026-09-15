"""把外部解析服务产出的 Markdown 归一化成项目规范形态。

为什么需要这一层
----------------
LlamaParse 的 markdown 视图会把表格还原成 HTML ``<table>``，而不是 Markdown
管道表。实测同一份《售后服务总则》（见
``docs/evals/llamaparse-document-extraction-eval.md``）：

* 承载「处理时限汇总」表格的那个 chunk，基准 Markdown 是 114 token，
  HTML 版本是 234 token（**2.05 倍**），其中 53.4% 的 token 是表格标签；
* 整篇文档层面 token 多花 12%~15%。

内容本身零丢失（归一化后与原生 Markdown 的字符序列完全一致），所以这不是
正确性问题，而是**成本与检索余量问题**：将近一半的 embedding 预算和证据窗口
被标签占掉了，而没有换来任何信息。把 HTML 表转回管道表即可抹平这笔开销。

职责边界
--------
本模块**只**做表格表示的转换，以及为让表格块独立成段而做的连续空行压缩。
它刻意不做下列事情，以免引入难以追踪的内容变更：

* 不规整中英文之间的空格（``双 11`` → ``双11``）——是否需要取决于 DashScope
  sparse 向量是否归一化，尚未验证，留待实验定论；
* 不压紧 LlamaParse 的「松散列表」（列表项之间的空行）；
* 不改写表格之外任何标签或文本。
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

# 连续 3 个以上换行压成 1 个空行：表格块与正文之间不会残留多余空行。
_EXTRA_BLANK_LINES = re.compile(r"\n{3,}")
# 单元格内空白（含换行）压成单个空格。
_CELL_WHITESPACE = re.compile(r"\s+")
# 触发转换的探测串；没有表格时整段文本原样返回，不做任何改动。
_TABLE_MARKER = "<table"


class _HtmlTableToPipeTableParser(HTMLParser):
    """把 HTML 表格收集成管道表，表格之外的文本原样透传。

    只识别 ``<table>`` / ``<tr>`` / ``<td>`` / ``<th>`` 四个结构标签；
    单元格内部的其它标签（如 ``<b>``）只取其文字，标签本身丢弃——管道表
    没有对应的表达能力，丢弃比原样保留更不容易污染检索。

    嵌套表格的处理是「拍平」：内层表格的结构标签被忽略，其单元格文字会
    并入外层当前单元格。政策类文档极少嵌套表格，这里选择保守拍平而不是
    递归渲染，避免产出行列错位的畸形表格。
    """

    def __init__(self) -> None:
        # convert_charrefs=True 先把 &amp; 这类实体还原成字符，
        # 否则会与后续的管道转义叠加成双重转义。
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._table_depth = 0
        # 每一行记录为 (该行是否含 <th>, 单元格文本列表)。
        self._rows: list[tuple[bool, list[str]]] = []
        self._row_cells: list[str] | None = None
        self._row_has_header = False
        self._cell_parts: list[str] | None = None

    # ------------------------------------------------------------ 解析回调
    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "table":
            self._table_depth += 1
            if self._table_depth == 1:
                self._rows = []
            return
        if self._table_depth != 1:
            return
        if tag == "tr":
            self._row_cells = []
            self._row_has_header = False
        elif tag in {"td", "th"}:
            self._cell_parts = []
            if tag == "th":
                self._row_has_header = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "table":
            if self._table_depth == 1:
                self._parts.append(self._render_table(self._rows))
                self._rows = []
            self._table_depth = max(0, self._table_depth - 1)
            return
        if self._table_depth != 1:
            return
        if tag in {"td", "th"}:
            if self._cell_parts is not None and self._row_cells is not None:
                self._row_cells.append(self._clean_cell("".join(self._cell_parts)))
            self._cell_parts = None
        elif tag == "tr":
            if self._row_cells is not None:
                self._rows.append((self._row_has_header, self._row_cells))
            self._row_cells = None

    def handle_data(self, data: str) -> None:
        if self._cell_parts is not None:
            self._cell_parts.append(data)
        elif self._table_depth == 0:
            # 表格之外的一切文本原样保留。
            self._parts.append(data)

    def handle_startendtag(self, tag: str, attrs) -> None:
        # 自闭合标签（如单元格里的 <br/>）在管道表里没有对应表示，直接丢弃。
        return

    # ---------------------------------------------------------------- 出口
    def render(self) -> str:
        """返回转换后的完整 Markdown。"""
        return "".join(self._parts)

    # ------------------------------------------------------------ 内部实现
    @staticmethod
    def _clean_cell(raw: str) -> str:
        """规整单元格文本：压缩空白、去首尾空格、转义列分隔符。"""
        cleaned = _CELL_WHITESPACE.sub(" ", raw).strip()
        # 单元格内的 | 若不转义，会被 Markdown 解析成列分隔符，把一行撑出多余的列。
        return cleaned.replace("|", "\\|")

    @staticmethod
    def _render_row(cells: list[str], width: int) -> str:
        padded = list(cells) + [""] * (width - len(cells))
        return "| " + " | ".join(padded) + " |"

    @classmethod
    def _render_table(cls, rows: list[tuple[bool, list[str]]]) -> str:
        """把收集到的行渲染成管道表。

        表头一律取第一行：Markdown 管道表在语法上必须有表头行，而 LlamaParse
        输出的首行本就带 ``<th>``（docx 与 pdf 两种来源都是），因此「首行即
        表头」既符合输入现实，也满足语法要求。若某张表整表没有 ``<th>``，
        首行会被提升为表头——这是唯一无损的选择，否则无法表达这张表。
        """
        if not rows:
            return ""
        header = rows[0][1]
        body = [cells for _, cells in rows[1:]]
        # 以最宽的一行为准补齐列数，避免短行导致行列错位。
        width = max([len(header)] + [len(cells) for cells in body])
        lines = [
            cls._render_row(header, width),
            cls._render_row(["---"] * width, width),
        ]
        lines.extend(cls._render_row(cells, width) for cells in body)
        # 前后各留一个空行，保证渲染器把它当成独立的表格块。
        return "\n\n" + "\n".join(lines) + "\n\n"


def normalize_extracted_markdown(text: str) -> str:
    """把外部解析服务产出的 Markdown 归一化成项目规范形态。

    当前唯一规则是 HTML 表格 → Markdown 管道表。没有表格时原样返回，
    连空行压缩都不做——归一化只处理它真正转换过的部分。
    """
    if _TABLE_MARKER not in text.lower():
        return text

    parser = _HtmlTableToPipeTableParser()
    parser.feed(text)
    parser.close()
    return _EXTRA_BLANK_LINES.sub("\n\n", parser.render())
