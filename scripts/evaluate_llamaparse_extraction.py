"""评估 LlamaParse 提取结果：保真度 + 是否满足 RAG 检索需要。

评估对象是 ``scripts/run_llamaparse_extract.py`` 的产物（每个文档一个子目录，
内含 ``markdown.md`` / ``text.txt``）。评估基准是仓库里同名的 ground-truth
Markdown（``docs/knowledge/01-售后服务总则.md``）——docx 与 pdf 都是由它经
pandoc 转换而来，因此它可以作为"提取应达到的上限"。

五类检查
--------
1. **文本保真度**：归一化（去 Markdown 标记、去 HTML 标签、去所有空白）后做
   字符级比对，给出相似度、丢失片段与新增片段。
2. **段落召回**：ground-truth 的每个正文段落是否都能在提取文本中找到。
3. **标题结构**：把提取结果喂给真实的 ``DocumentLoader``，看它能否还原出
   ground-truth 的 heading_path 集合——heading_path 是本项目 RAG 的证据定位
   键，丢了它检索就退化。
4. **关键事实**：用 ``evals/knowledge/stage_c/cases.jsonl`` 金标样例里的
   ``key_answer_facts`` 与关键数值做探针，检查事实句是否仍可被检索到。
5. **分块适配**：把提取结果喂给真实的 ``KnowledgeChunker``，验证
   ``content == text[start:end]`` 这条硬约束、token 上限、以及"承载金标
   heading_path 的 chunk 是否真的包含对应事实"。

结论以 JSON + Markdown 双份落盘，便于人工复核与后续回归对比。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 脚本引导：保证 ``python scripts/xxx.py`` 也能导入 app 包。
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import argparse
import difflib
import json
import re
from dataclasses import asdict, dataclass, field

import tiktoken

from app.knowledge.base import DocumentSourceType
from app.knowledge.chunking import (
    MAX_CHUNK_TOKENS,
    TARGET_CHUNK_TOKENS,
    KnowledgeChunker,
)
from app.knowledge.document_loader import DocumentLoader, WordDocumentExtractor

DEFAULT_EXTRACT_DIR = _REPO_ROOT / ".artifacts" / "llamaparse-extract"
DEFAULT_REFERENCE_DIR = _REPO_ROOT / "docs" / "knowledge"
DEFAULT_CASES_PATH = _REPO_ROOT / "evals" / "knowledge" / "stage_c" / "cases.jsonl"
# 金标样例里本文档的 document_key（见 app/evals/stage_c/fixtures.py）。
GENERAL_SERVICE_KEY = "general_service"

# 关键数值探针：这些数字是客服问答的最终答案本身，任何一个丢失都会直接
# 导致答错，因此单独列出做硬性检查。
CRITICAL_LITERALS = (
    "400-860-0000",   # 客服热线
    "9:00-21:00",     # 热线服务时间
    "24小时",          # 售后申请审核响应时限
    "48小时",          # 退换货验收时限
    "5个工作日",        # 质量检测结果出具时限
    "1～3个工作日",     # 退款到账时限
    "15天",            # 质量问题退换货时限
    "7天",             # 大促无理由退货时限
    "618",            # 大促活动之一
    "双11",           # 大促活动之一
    "2026-07-01",     # 生效日期
)

# 页眉页脚/页码噪声的典型形态：出现即说明提取串入了版式信息。
NOISE_PATTERNS = (
    r"^\s*第\s*\d+\s*页\s*$",       # 中文页码
    r"^\s*Page\s*\d+\s*$",         # 英文页码
    r"^\s*\d+\s*/\s*\d+\s*$",      # "3 / 12" 形态页码
    r"^\s*\d{1,3}\s*$",            # 孤立的纯数字行（pandoc 生成的页码）
)

_MARKDOWN_MARKER_RE = re.compile(r"[#*>`_~|]+")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_PIPE_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_PIPE_TABLE_SEP_RE = re.compile(r"^\s*\|[\s:|-]+\|\s*$")
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)\s*$")
# 列表项：无序（- * +）或有序（1. / 1、）。用于把连续列表行拆成独立段落。
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.、)])\s+\S")


def normalize(text: str) -> str:
    """把文本压成"可比字符流"：去列表标记、去 HTML 标签、去 Markdown 标记、去空白。

    去空白是关键：LlamaParse 会在中英文之间插入空格（``双 11``、``VIP 会员``），
    PDF 视图还会硬换行；而中文检索关心的是字符序列本身，不是排版空格。

    列表标记只按"行首"剥除：ground-truth 写 ``- 项``，LlamaParse 写 ``*   项``
    或 ``1. 项``，标记符号与缩进量都不稳定；但 ``400-860-0000`` 这类词内连字符
    必须保留，所以不能用全局字符替换。
    """
    text = re.sub(r"(?m)^[ \t]*(?:[-*+]|\d+[.、)])(?:[ \t]+|$)", " ", text)
    text = _HTML_TAG_RE.sub(" ", text)
    text = _MARKDOWN_MARKER_RE.sub(" ", text)
    return re.sub(r"\s+", "", text)


def spaced_char_stream(text: str) -> tuple[str, list[bool]]:
    """把文本压成字符流，并标出"每个字符是否被行内空格与前一字符隔开"。

    返回 ``(字符流, 标记列表)``。标记列表与字符流等长，第 i 个标记为 True
    表示该字符在原文中前面有一个**行内**空白（不含换行；行首缩进不算）。

    这里刻意把 Markdown 标记**删除**而不是替换成空格——否则 ``**24**`` 这类
    粗体标记会在比对里伪装成"被插入的空格"，制造大量假阳性。
    """
    cleaned = re.sub(r"(?m)^[ \t]*(?:[-*+]|\d+[.、)])(?:[ \t]+|$)", " ", text)
    cleaned = _HTML_TAG_RE.sub("", cleaned)
    cleaned = _MARKDOWN_MARKER_RE.sub("", cleaned)

    chars: list[str] = []
    flags: list[bool] = []
    pending_space = False   # 当前是否累计了一个"行内"空白
    seen_newline = True     # 上一个字符是换行/开头：其后的空白属于行首缩进
    for char in cleaned:
        if char == "\n":
            pending_space = False
            seen_newline = True
            continue
        if char.isspace():
            if not seen_newline:
                pending_space = bool(chars)
            continue
        chars.append(char)
        flags.append(pending_space)
        pending_space = False
        seen_newline = False
    return "".join(chars), flags


def inserted_space_pairs(extracted: str, reference: str) -> list[str]:
    """返回提取结果中被插入行内空格的字符对（以其在基准中的原始形态表示）。

    前提：两者的字符流必须完全一致；只有在这种前提下逐字符对齐才不会错位，
    找到的空格位置才是可信的。若内容本身就有增删，本项返回空列表——
    那种情况已由缺失/新增片段检查覆盖。

    为什么单独测这一项：``双11`` 被写成 ``双 11`` 后，BM25/关键词召回会直接
    漏掉，但语义向量召回几乎不受影响。是否要在 loader 里做空格规整，
    取决于向量库是否走稀疏/关键词通道。
    """
    extracted_chars, extracted_flags = spaced_char_stream(extracted)
    reference_chars, reference_flags = spaced_char_stream(
        strip_table_separators(reference)
    )
    if extracted_chars != reference_chars:
        return []

    pairs = [
        extracted_chars[index - 1] + extracted_chars[index]
        for index in range(1, len(extracted_chars))
        # 提取结果里有行内空格、基准里却紧挨着 => 这个空格是提取过程插入的。
        if extracted_flags[index] and not reference_flags[index]
    ]
    return list(dict.fromkeys(pairs))


def reference_paragraphs(markdown_text: str) -> list[str]:
    """从 ground-truth Markdown 中切出正文段落。

    跳过标题行与表格行：标题由"标题结构"检查单独负责，表格由关键事实探针覆盖，
    混在一起会让段落召回率失去解释力。

    连续的列表项要拆成独立段落。ground-truth 里列表项常常一行一项、中间不空行，
    而 LlamaParse 会输出"松散列表"（每项之间插空行）。若不拆开，基准里的
    "三行合并块"在提取结果里天然不可能连续出现，会产生假阴性。
    """
    paragraphs: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        if not buffer:
            return
        if all(_LIST_ITEM_RE.match(line) for line in buffer):
            paragraphs.extend(line.strip() for line in buffer if line.strip())
        else:
            joined = "".join(buffer).strip()
            if joined:
                paragraphs.append(joined)
        buffer.clear()

    for line in markdown_text.split("\n"):
        stripped = line.strip()
        if not stripped:
            flush()
            continue
        if _HEADING_RE.match(stripped) or _PIPE_TABLE_ROW_RE.match(stripped):
            flush()
            continue
        # 引用块（>）保留正文、去掉标记，其内容同样应被提取出来。
        buffer.append(re.sub(r"^>\s*", "", stripped))
    flush()
    return paragraphs


def strip_table_separators(markdown_text: str) -> str:
    """去掉 Markdown 管道表格的分隔行（``|---|---|``）。

    分隔行是纯排版产物，LlamaParse 输出 HTML 表格时不存在对应内容，
    留在基准里会让字符级比对出现无意义的"缺失片段"。
    """
    return "\n".join(
        line for line in markdown_text.split("\n") if not _PIPE_TABLE_SEP_RE.match(line)
    )


def reference_tables(markdown_text: str) -> list[list[list[str]]]:
    """解析 ground-truth 里的 Markdown 管道表格，返回 [表][行][单元格]。"""
    tables: list[list[list[str]]] = []
    current: list[list[str]] = []
    for line in markdown_text.split("\n"):
        if _PIPE_TABLE_ROW_RE.match(line):
            if _PIPE_TABLE_SEP_RE.match(line):
                continue
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            current.append(cells)
        elif current:
            tables.append(current)
            current = []
    if current:
        tables.append(current)
    return tables


def heading_paths(markdown_text: str, loader: DocumentLoader) -> list[str | None]:
    """用真实的 DocumentLoader 还原 heading_path 序列。"""
    loaded = loader.load(markdown_text.encode("utf-8"), DocumentSourceType.MARKDOWN)
    return [section.heading_path for section in loaded.sections]


def noise_hits(text: str) -> list[str]:
    """返回命中的页眉页脚/页码噪声行（去重、最多留 10 条便于展示）。"""
    hits: list[str] = []
    for line in text.split("\n"):
        for pattern in NOISE_PATTERNS:
            if re.match(pattern, line):
                hits.append(line.strip())
                break
    return sorted(set(hits))[:10]


def hard_wrap_lines(text: str) -> list[str]:
    """找出 PDF 硬换行残留：行尾没有句末标点、且下一行不是空行/列表/标题。

    这类断行会把一个完整句子切成两行，是纯文本视图最典型的噪声，
    会直接损害"一句话被完整召回"的能力。
    """
    lines = text.split("\n")
    wrapped: list[str] = []
    for index, line in enumerate(lines[:-1]):
        stripped = line.strip()
        if not stripped or stripped[-1] in "。！？：；」）》|":
            continue
        next_stripped = lines[index + 1].strip()
        if not next_stripped:
            continue
        if _HEADING_RE.match(next_stripped) or next_stripped[0] in "*•-|" or next_stripped[:2] in {"1.", "2.", "3.", "4.", "5.", "6.", "7.", "8.", "9."}:
            continue
        wrapped.append(stripped)
    return wrapped


def diff_fragments(
    reference: str, extracted: str, *, min_length: int = 6
) -> tuple[list[str], list[str]]:
    """比较两段归一化文本，返回（基准里有而提取缺失的片段, 提取新增的片段）。"""
    matcher = difflib.SequenceMatcher(None, reference, extracted, autojunk=False)
    missing: list[str] = []
    added: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in {"delete", "replace"} and i2 - i1 >= min_length:
            missing.append(reference[i1:i2])
        if tag in {"insert", "replace"} and j2 - j1 >= min_length:
            added.append(extracted[j1:j2])
    return missing[:20], added[:20]


@dataclass
class FactProbe:
    """单条检索事实探针的检查结果。"""

    label: str          # 探针名称（金标 fact_id 或关键数值原文）
    source: str         # 来源：golden_case（金标样例） / critical_literal（关键数值）
    hit: bool           # 该事实是否仍能在提取文本中找到
    detail: str         # 命中/未命中的说明，未命中时给出缺失片段


@dataclass
class DocumentEvaluation:
    """一个文档的完整评估结果。"""

    document_name: str            # 被提取的文件名（含扩展名）
    reference_name: str           # 作为基准的 ground-truth Markdown 文件名
    markdown_view: dict = field(default_factory=dict)  # Markdown 视图的各项指标
    text_view: dict = field(default_factory=dict)      # 纯文本视图的对照指标
    existing_loader_view: dict = field(default_factory=dict)  # 现有 loader 的对照指标
    fact_probes: list[FactProbe] = field(default_factory=list)  # 事实探针

    @property
    def fact_hit_rate(self) -> float:
        """事实探针命中率；无探针时记为 1.0（不参与扣分）。"""
        if not self.fact_probes:
            return 1.0
        return sum(1 for probe in self.fact_probes if probe.hit) / len(self.fact_probes)


def load_golden_expectations(cases_path: Path) -> tuple[list[str], list[tuple[str, str]]]:
    """读取 stage_c 金标样例，抽出与本文档相关的期望 heading_path 与事实。

    返回 ``(期望 heading_path 列表, [(fact_id, statement), ...])``。
    """
    paths: list[str] = []
    facts: list[tuple[str, str]] = []
    if not cases_path.exists():
        return paths, facts

    for raw in cases_path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        case = json.loads(raw)
        relevant_here = False
        for group in case.get("required_evidence_groups") or []:
            for option in group.get("any_of") or []:
                if option.get("document_key") != GENERAL_SERVICE_KEY:
                    continue
                relevant_here = True
                heading_path = option.get("heading_path")
                if heading_path and heading_path not in paths:
                    paths.append(heading_path)
        if relevant_here:
            for fact in case.get("key_answer_facts") or []:
                pair = (fact.get("fact_id", ""), fact.get("statement", ""))
                if pair not in facts:
                    facts.append(pair)
    return paths, facts


def evaluate_document(
    *,
    document_name: str,
    markdown_text: str,
    text_text: str,
    reference_text: str,
    expected_paths: list[str],
    golden_facts: list[tuple[str, str]],
    loader: DocumentLoader,
    chunker: KnowledgeChunker,
    source_path: Path | None = None,
) -> DocumentEvaluation:
    """对单个提取产物跑完全部五类检查。"""
    evaluation = DocumentEvaluation(
        document_name=document_name,
        reference_name=document_name.rsplit(".", 1)[0] + ".md",
    )

    reference_normalized = normalize(strip_table_separators(reference_text))
    markdown_normalized = normalize(markdown_text)
    text_normalized = normalize(text_text)

    # ---- 1. 文本保真度 -------------------------------------------------
    similarity = difflib.SequenceMatcher(
        None, reference_normalized, markdown_normalized, autojunk=False
    ).ratio()
    missing, added = diff_fragments(reference_normalized, markdown_normalized)

    # ---- 2. 段落召回 ---------------------------------------------------
    paragraphs = reference_paragraphs(reference_text)
    missing_paragraphs = [
        paragraph for paragraph in paragraphs if normalize(paragraph) not in markdown_normalized
    ]

    # ---- 3. 标题结构 ---------------------------------------------------
    extracted_paths = [p for p in heading_paths(markdown_text, loader) if p is not None]
    reference_paths = [p for p in heading_paths(reference_text, loader) if p is not None]
    golden_path_hits = {
        path: path in set(extracted_paths) for path in expected_paths
    }

    # ---- 4. 分块适配 ---------------------------------------------------
    loaded = loader.load(markdown_text.encode("utf-8"), DocumentSourceType.MARKDOWN)
    chunks = chunker.split(
        loaded,
        organization_id="eval-org",
        document_id="eval-doc",
        version_id="eval-version",
    )
    offsets_exact = all(
        chunk.content == loaded.text[chunk.start_offset : chunk.end_offset] for chunk in chunks
    )
    max_tokens = max(chunk.token_count for chunk in chunks)
    unnamed_chunks = sum(1 for chunk in chunks if chunk.heading_path is None)
    # 承载金标 heading_path 的 chunk 是否真的包含该事实所在段落的关键词。
    path_coverage = {
        path: sum(1 for chunk in chunks if chunk.heading_path == path)
        for path in expected_paths
    }
    reference_chunks = chunker.split(
        loader.load(reference_text.encode("utf-8"), DocumentSourceType.MARKDOWN),
        organization_id="eval-org",
        document_id="eval-doc",
        version_id="eval-version",
    )

    # 成本维度：同一份内容，提取结果比原生 Markdown 多花多少 token。
    # 这直接决定 embedding 单价与向量库体积，是 loader 选型的现实约束。
    encoding = tiktoken.get_encoding("cl100k_base")
    extracted_tokens = len(encoding.encode(markdown_text))
    reference_tokens = len(encoding.encode(reference_text))
    extracted_chunk_tokens = sum(chunk.token_count for chunk in chunks)
    reference_chunk_tokens = sum(chunk.token_count for chunk in reference_chunks)

    evaluation.markdown_view = {
        "raw_chars": len(markdown_text),
        "normalized_chars": len(markdown_normalized),
        "reference_normalized_chars": len(reference_normalized),
        "similarity": round(similarity, 4),
        "paragraph_total": len(paragraphs),
        "paragraph_missing": len(missing_paragraphs),
        "paragraph_recall": round(
            (len(paragraphs) - len(missing_paragraphs)) / max(1, len(paragraphs)), 4
        ),
        "missing_paragraph_samples": missing_paragraphs[:5],
        "missing_fragments": missing,
        "added_fragments": added,
        "hash_table_count": markdown_text.count("<table>"),
        "pipe_table_count": len(reference_tables(markdown_text)),
        "heading_paths": extracted_paths,
        "reference_heading_paths": reference_paths,
        "heading_path_recall": round(
            len(set(extracted_paths) & set(reference_paths)) / max(1, len(set(reference_paths))),
            4,
        ),
        "golden_heading_path_hits": golden_path_hits,
        "noise_hits": noise_hits(markdown_text),
        "chunk_count": len(chunks),
        "reference_chunk_count": len(reference_chunks),
        "chunk_offsets_exact": offsets_exact,
        "chunk_max_tokens": max_tokens,
        "chunk_token_limit": MAX_CHUNK_TOKENS,
        "chunk_within_token_limit": max_tokens <= MAX_CHUNK_TOKENS,
        "chunk_target_tokens": TARGET_CHUNK_TOKENS,
        "chunk_mean_tokens": round(
            sum(chunk.token_count for chunk in chunks) / len(chunks), 1
        ),
        "chunks_without_heading": unnamed_chunks,
        "golden_path_chunk_counts": path_coverage,
        "extracted_tokens": extracted_tokens,
        "reference_tokens": reference_tokens,
        "token_overhead_ratio": round(extracted_tokens / max(1, reference_tokens), 4),
        "chunk_total_tokens": extracted_chunk_tokens,
        "reference_chunk_total_tokens": reference_chunk_tokens,
        "reference_chunk_max_tokens": max(chunk.token_count for chunk in reference_chunks),
        "inserted_space_pairs": inserted_space_pairs(markdown_text, reference_text),
    }

    # ---- 纯文本视图对照：证明"只能用 markdown 视图" ----------------------
    text_paths = [p for p in heading_paths(text_text, loader) if p is not None]
    evaluation.text_view = {
        "raw_chars": len(text_text),
        "similarity": round(
            difflib.SequenceMatcher(
                None, reference_normalized, text_normalized, autojunk=False
            ).ratio(),
            4,
        ),
        "heading_paths": text_paths,
        "heading_path_count": len(text_paths),
        "noise_hits": noise_hits(text_text),
        "hard_wrap_count": len(hard_wrap_lines(text_text)),
        "hard_wrap_samples": hard_wrap_lines(text_text)[:5],
    }

    # ---- 现有 loader 对照：docx 走 python-docx，pdf 根本走不通 -------------
    if document_name.lower().endswith(".docx") and source_path is not None and source_path.exists():
        word_text = WordDocumentExtractor().extract(source_path.read_bytes())
        word_loaded = loader.load(source_path.read_bytes(), DocumentSourceType.WORD)
        word_chunks = chunker.split(
            word_loaded,
            organization_id="eval-org",
            document_id="eval-doc",
            version_id="eval-version",
        )
        word_normalized = normalize(word_text)
        evaluation.existing_loader_view = {
            "loader": "WordDocumentExtractor(python-docx)",
            "supported": True,
            "raw_chars": len(word_text),
            "similarity": round(
                difflib.SequenceMatcher(
                    None, reference_normalized, word_normalized, autojunk=False
                ).ratio(),
                4,
            ),
            "heading_path_count": sum(
                1 for s in word_loaded.sections if s.heading_path is not None
            ),
            "chunk_count": len(word_chunks),
            "chunks_without_heading": sum(
                1 for chunk in word_chunks if chunk.heading_path is None
            ),
            "chunk_max_tokens": max(chunk.token_count for chunk in word_chunks),
            "chunk_total_tokens": sum(chunk.token_count for chunk in word_chunks),
        }
    else:
        evaluation.existing_loader_view = {
            "loader": "无（现有 DocumentLoader 不支持该格式）",
            "supported": False,
        }

    # ---- 5. 关键事实 ---------------------------------------------------
    probes: list[FactProbe] = []
    for literal in CRITICAL_LITERALS:
        normalized_literal = normalize(literal)
        probes.append(
            FactProbe(
                label=literal,
                source="critical_literal",
                hit=normalized_literal in markdown_normalized,
                detail="" if normalized_literal in markdown_normalized else "关键数值在提取文本中缺失",
            )
        )
    for fact_id, statement in golden_facts:
        # 金标 statement 是自然语言复述，不保证逐字出现在原文。这里退一步：
        # 抽取 statement 中的数字/专有名词作为锚点，要求全部命中。
        anchors = re.findall(r"[A-Za-z0-9]+(?:[-:][A-Za-z0-9]+)*|\d+", statement)
        anchors = [anchor for anchor in anchors if len(anchor) >= 2]
        missed = [anchor for anchor in anchors if normalize(anchor) not in markdown_normalized]
        probes.append(
            FactProbe(
                label=f"{fact_id}: {statement}",
                source="golden_case",
                hit=not missed,
                detail="" if not missed else f"缺失锚点：{', '.join(missed)}",
            )
        )
    evaluation.fact_probes = probes
    return evaluation


def render_markdown_report(evaluations: list[DocumentEvaluation]) -> str:
    """把评估结果渲染成便于阅读的 Markdown 摘要。"""
    lines: list[str] = ["# LlamaParse 提取评估摘要（自动生成）", ""]
    lines.append("| 指标 | " + " | ".join(e.document_name for e in evaluations) + " |")
    lines.append("|---|" + "---|" * len(evaluations))

    def row(label: str, values: list[object]) -> None:
        lines.append(
            f"| {label} | " + " | ".join(str(value) for value in values) + " |"
        )

    row("归一化字符数", [e.markdown_view["normalized_chars"] for e in evaluations])
    row("基准归一化字符数", [e.markdown_view["reference_normalized_chars"] for e in evaluations])
    row("字符相似度", [e.markdown_view["similarity"] for e in evaluations])
    row("段落召回率", [e.markdown_view["paragraph_recall"] for e in evaluations])
    row("heading_path 召回率", [e.markdown_view["heading_path_recall"] for e in evaluations])
    row(
        "金标 heading_path 命中",
        [
            f"{sum(1 for v in e.markdown_view['golden_heading_path_hits'].values() if v)}"
            f"/{len(e.markdown_view['golden_heading_path_hits'])}"
            for e in evaluations
        ],
    )
    row(
        "关键事实探针命中",
        [f"{sum(1 for p in e.fact_probes if p.hit)}/{len(e.fact_probes)}" for e in evaluations],
    )
    row("分块数（提取 / 基准）", [
        f"{e.markdown_view['chunk_count']} / {e.markdown_view['reference_chunk_count']}"
        for e in evaluations
    ])
    row("分块偏移精确", [e.markdown_view["chunk_offsets_exact"] for e in evaluations])
    row(
        "分块最大 token（上限 " + str(MAX_CHUNK_TOKENS) + "）",
        [e.markdown_view["chunk_max_tokens"] for e in evaluations],
    )
    row("无标题分块数", [e.markdown_view["chunks_without_heading"] for e in evaluations])
    row("HTML 表格数", [e.markdown_view["hash_table_count"] for e in evaluations])
    row("页码/页脚噪声", [e.markdown_view["noise_hits"] or "无" for e in evaluations])
    row(
        "token 数（提取 / 基准）",
        [
            f"{e.markdown_view['extracted_tokens']} / {e.markdown_view['reference_tokens']} "
            f"(×{e.markdown_view['token_overhead_ratio']})"
            for e in evaluations
        ],
    )
    row(
        "被插入空格的字符对",
        [e.markdown_view["inserted_space_pairs"] or "无" for e in evaluations],
    )
    row("纯文本视图 heading 数", [e.text_view["heading_path_count"] for e in evaluations])
    row("纯文本视图硬换行行数", [e.text_view["hard_wrap_count"] for e in evaluations])
    row(
        "现有 loader 是否支持",
        ["是" if e.existing_loader_view.get("supported", True) else "否" for e in evaluations],
    )
    row(
        "现有 loader 相似度",
        [e.existing_loader_view.get("similarity", "—") for e in evaluations],
    )
    row(
        "现有 loader 有标题分块数",
        [
            e.existing_loader_view.get("chunk_count", 0)
            - e.existing_loader_view.get("chunks_without_heading", 0)
            for e in evaluations
        ],
    )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="评估 LlamaParse 提取结果的 RAG 适配性")
    parser.add_argument("--extract-dir", default=str(DEFAULT_EXTRACT_DIR))
    parser.add_argument("--reference-dir", default=str(DEFAULT_REFERENCE_DIR))
    parser.add_argument("--cases", default=str(DEFAULT_CASES_PATH))
    parser.add_argument(
        "--output", default=str(DEFAULT_EXTRACT_DIR / "eval-report.json")
    )
    args = parser.parse_args(argv)

    extract_dir = Path(args.extract_dir)
    reference_dir = Path(args.reference_dir)
    if not extract_dir.exists():
        print(f"提取产物目录不存在：{extract_dir}", file=sys.stderr)
        return 1

    expected_paths, golden_facts = load_golden_expectations(Path(args.cases))
    loader = DocumentLoader()
    chunker = KnowledgeChunker()

    evaluations: list[DocumentEvaluation] = []
    for markdown_path in sorted(extract_dir.glob("*/markdown.md")):
        document_name = markdown_path.parent.name
        # 产物目录名形如 "01-售后服务总则.docx"，去掉扩展名即基准 Markdown 名。
        reference_name = document_name.rsplit(".", 1)[0] + ".md"
        reference_path = reference_dir / reference_name
        if not reference_path.exists():
            print(f"[跳过] 找不到基准文档 {reference_path}", file=sys.stderr)
            continue
        text_path = markdown_path.parent / "text.txt"
        evaluations.append(
            evaluate_document(
                document_name=document_name,
                markdown_text=markdown_path.read_text(encoding="utf-8"),
                text_text=text_path.read_text(encoding="utf-8") if text_path.exists() else "",
                reference_text=reference_path.read_text(encoding="utf-8"),
                expected_paths=expected_paths,
                golden_facts=golden_facts,
                loader=loader,
                chunker=chunker,
                source_path=reference_dir / document_name,
            )
        )

    if not evaluations:
        print("没有可评估的产物", file=sys.stderr)
        return 1

    payload = {
        "expected_golden_heading_paths": expected_paths,
        "golden_fact_count": len(golden_facts),
        "documents": [
            {
                **{k: v for k, v in asdict(evaluation).items() if k != "fact_probes"},
                "fact_probe_hit_rate": round(evaluation.fact_hit_rate, 4),
                "fact_probes": [asdict(probe) for probe in evaluation.fact_probes],
            }
            for evaluation in evaluations
        ],
    }
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report_path = output_path.with_name("eval-report.md")
    report_path.write_text(render_markdown_report(evaluations), encoding="utf-8")

    for evaluation in evaluations:
        print(
            f"[{evaluation.document_name}] 相似度={evaluation.markdown_view['similarity']} "
            f"段落召回={evaluation.markdown_view['paragraph_recall']} "
            f"heading召回={evaluation.markdown_view['heading_path_recall']} "
            f"事实命中={sum(1 for p in evaluation.fact_probes if p.hit)}/"
            f"{len(evaluation.fact_probes)} "
            f"分块={evaluation.markdown_view['chunk_count']} "
            f"最大token={evaluation.markdown_view['chunk_max_tokens']}"
        )
    print(f"报告已写入：{output_path}")
    print(f"摘要已写入：{report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
