"""端到端检索回归：证明 docx/pdf 入库与原生 md 入库在检索上等价。

任务 2 的验收口径（见 ``docs/knowledge-loader-remaining-tasks.md`` §2）
--------------------------------------------------------------------
用 ``evals/knowledge/stage_c/cases.jsonl`` 中 3 个 ``general_service`` 用例，
把同一份政策文档分别以 ``.md`` / ``.docx`` / ``.pdf`` 三种形式入库到**三个独立企业**，
跑同一批用例，比较：

1. ``required_evidence_groups`` 的命中数是否一致；
2. citation 的 ``heading_path`` 是否命中用例期望值
   （复用 ``app/evals/stage_c/scoring.py`` 的 ``heading_matches``，不自己写一套判据）。

为什么分企业入库
----------------
三份文件的**内容完全相同**，只是容器格式不同。若入到同一企业，检索会同时命中
三份等价文档，citation 的归属就分不清是哪种格式贡献的，比较也就失去意义。
分企业后每个企业内部只有一种格式，结果可逐条对照。

为什么按格式分别准备语料
------------------------
样本文档只有通用总则的 docx/pdf。用例还会引用 ``returns_exchange`` /
``logistics`` / ``vip`` 三份文档作为「命中任一即可」的备选项，因此这三份在
每个企业里都用原生 md 入库——它们不是本次的比较对象，只保证用例可判分。

为什么用 Baseline 检索
----------------------
Baseline 是确定性检索（embedding → Qdrant 混合召回 → 交叉重排 → SQLite 二次校验），
``AdaptiveKnowledgeSearchService`` 还要跑 Planner/Assessor 大模型调用，会引入与本
任务无关的随机性。本任务验证的是「入库格式是否影响检索」，因此用 Baseline 隔离变量。

为什么自己装配服务
------------------
``create_knowledge_services`` 会额外构造 LLM 客户端（需要 ``DEEPSEEK_API_KEY``），
而本脚本只用到入库 + Baseline 检索，两者都不碰大模型。这里按同样的真实适配器手动
装配，避免为一个不需要的依赖要求密钥。入库仍走异步链路的 worker，只是由脚本
在事件循环里同步跑完，保证入库格式与生产路径完全一致。

产物
----
``.artifacts/document-format-regression/report.json``：逐用例、逐格式的
heading_path、命中的证据组、以及三格式一致性结论。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 脚本引导：保证 ``python scripts/xxx.py`` 也能导入 app 包。
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import argparse
import asyncio
import json
import sqlite3
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Sequence
from uuid import uuid4

from app.core.config import get_knowledge_settings
from app.evals.stage_c.models import StageCCase, load_stage_c_cases
from app.evals.stage_c.scoring import heading_matches
from app.knowledge.base import DocumentSourceType, IngestionStatus
from app.knowledge.chunking import KnowledgeChunker
from app.knowledge.dashscope_embeddings import DashScopeEmbeddingClient
from app.knowledge.document_loader import DocumentLoader
from app.knowledge.ingestion import KnowledgeIngestionService
from app.knowledge.ingestion_worker import KnowledgeIngestionWorker
from app.knowledge.llamaparse_cache import LlamaParseCache
from app.knowledge.llamaparse_extractor import LlamaParseExtractor
from app.knowledge.qdrant_store import QdrantVectorStore
from app.knowledge.reranking import DashScopeQwenReranker
from app.knowledge.retrieval import BaselineKnowledgeSearchService, HybridRetriever
from app.knowledge.sqlite_store import SQLiteKnowledgeStore

DEFAULT_OUTPUT_DIR = _REPO_ROOT / ".artifacts" / "document-format-regression"
DEFAULT_CASES_PATH = _REPO_ROOT / "evals" / "knowledge" / "stage_c" / "cases.jsonl"
# 本次回归的目标文档：只有它同时存在 md / docx / pdf 三种形态。
GENERAL_SERVICE_KEY = "general_service"
GENERAL_SERVICE_STEM = "01-售后服务总则"
# 用例会引用这些文档作为「命中任一即可」的备选；它们只保证用例可判分，
# 不是本次比较对象，因此统一用原生 md 入库。
SUPPORTING_DOCUMENTS = (
    "02-退货与换货政策",
    "04-物流配送与异常处理",
    "08-VIP会员权益",
)
# 三种入库形态 -> 企业 id。企业 id 直接用作 organization_id。
FORMAT_ORGANIZATIONS = {
    "markdown": "fmt-md",
    "word": "fmt-docx",
    "pdf": "fmt-pdf",
}
# 形态 -> 目标文档的源文件后缀。
SOURCE_SUFFIX = {"markdown": ".md", "word": ".docx", "pdf": ".pdf"}
SOURCE_TYPE_BY_FORMAT = {
    "markdown": DocumentSourceType.MARKDOWN,
    "word": DocumentSourceType.WORD,
    "pdf": DocumentSourceType.PDF,
}
_TITLE = "售后服务总则"
_DOCUMENT_KEY_BY_TITLE = {
    "售后服务总则": "general_service",
    "退货与换货政策": "returns_exchange",
    "物流配送与异常处理": "logistics",
    "VIP会员权益": "vip",
}
_SUPPORTING_TITLE_BY_STEM = {
    "02-退货与换货政策": "退货与换货政策",
    "04-物流配送与异常处理": "物流配送与异常处理",
    "08-VIP会员权益": "VIP会员权益",
}
_REGRESSION_USER_ID = "format-regression"
# 入库等待上限：三份文档 + 两份 docx/pdf 外部解析，单份实测约 27 秒。
_INGEST_TIMEOUT_SECONDS = 1800.0


@dataclass
class RegressionServices:
    """回归脚本用到的三个真实适配器（不含任何大模型客户端）。"""

    store: SQLiteKnowledgeStore                      # 文档/版本/任务持久化
    ingestion: KnowledgeIngestionService             # 入库编排（异步入队）
    baseline: BaselineKnowledgeSearchService         # 确定性检索（本次判分对象）


@dataclass
class CaseOutcome:
    """一个用例在一种入库形态下的检索结果。"""

    case_id: str                                        # 用例 id
    citation_heading_paths: list[str] = field(default_factory=list)  # 返回的 heading_path
    group_hits: dict[str, bool] = field(default_factory=dict)        # 证据组 -> 是否命中

    @property
    def hit_count(self) -> int:
        """命中的证据组数量。"""
        return sum(1 for hit in self.group_hits.values() if hit)


def source_document_path(repo_root: Path, format_name: str) -> Path:
    """返回某种形态下目标文档的路径。"""
    return (
        repo_root
        / "docs"
        / "knowledge"
        / f"{GENERAL_SERVICE_STEM}{SOURCE_SUFFIX[format_name]}"
    )


def supporting_document_plan(repo_root: Path) -> list[tuple[str, Path]]:
    """返回需要用原生 md 入库的辅助文档 (标题, 路径)。"""
    plan: list[tuple[str, Path]] = []
    for stem in SUPPORTING_DOCUMENTS:
        path = repo_root / "docs" / "knowledge" / f"{stem}.md"
        if path.exists():
            plan.append((_SUPPORTING_TITLE_BY_STEM[stem], path))
    return plan


def select_general_service_cases(cases: Sequence[StageCCase]) -> list[StageCCase]:
    """挑出引用了 general_service 的证据用例。"""
    selected: list[StageCCase] = []
    for case in cases:
        if any(
            alternative.document_key == GENERAL_SERVICE_KEY
            for group in case.required_evidence_groups
            for alternative in group.any_of
        ):
            selected.append(case)
    return selected


def expected_general_service_headings(case: StageCCase) -> list[str]:
    """用例里针对 general_service 的全部期望 heading_path。"""
    return [
        alternative.heading_path
        for group in case.required_evidence_groups
        for alternative in group.any_of
        if alternative.document_key == GENERAL_SERVICE_KEY
    ]


def score_case(case: StageCCase, citations, document_keys: dict[str, str]) -> CaseOutcome:
    """按 stage_c 的判据逐组判定命中，不做任何自定义放宽。

    「命中一组」的定义与 ``app/evals/stage_c/scoring.py`` 完全一致：该组里
    存在某个 alternative，其 ``heading_path`` 被 citation 命中（``heading_matches``）。
    """
    headings = [
        citation.heading_path
        for citation in citations
        if citation.heading_path is not None
    ]
    group_hits: dict[str, bool] = {}
    for group in case.required_evidence_groups:
        # 所有证据组都按实际文档身份与标题路径判分，辅助文档不能默认命中。
        group_hits[group.group_id] = any(
            document_keys.get(citation.document_id) == alternative.document_key
            and heading_matches(citation.heading_path, alternative.heading_path)
            for citation in citations
            for alternative in group.any_of
        )
    return CaseOutcome(
        case_id=case.case_id,
        citation_heading_paths=headings,
        group_hits=group_hits,
    )


def heading_exactness(case: StageCCase, outcome: CaseOutcome) -> dict[str, bool]:
    """每个期望 heading_path 是否被 citation 命中。"""
    return {
        expected: any(
            heading_matches(heading, expected)
            for heading in outcome.citation_heading_paths
        )
        for expected in expected_general_service_headings(case)
    }


def score_search_result(case, result, document_keys):
    """基础设施失败或空证据必须终止回归，不能把三份空结果判成等价。"""
    if not result.ok:
        code = getattr(result.error, "code", "SEARCH_FAILED")
        raise RuntimeError(f"检索失败：{case.case_id} code={code}")
    if not result.citations:
        raise RuntimeError(f"检索没有返回证据：{case.case_id}")
    return score_case(case, result.citations, document_keys)


def build_services(*, database_path: Path, settings) -> RegressionServices:
    """按生产同样的真实适配器装配入库 + Baseline 检索（不涉及大模型）。"""
    store = SQLiteKnowledgeStore(database_path=database_path)

    # DOCX/PDF 走 LlamaParse；密钥缺失时装配照常，只有真正加载这类文档才失败。
    extractor = (
        LlamaParseExtractor(
            api_key=settings.llama_cloud_api_key,
            tier=settings.llama_cloud_tier,
            cache=(
                LlamaParseCache(settings.llama_cloud_cache_dir)
                if settings.llama_cloud_cache_dir
                else None
            ),
        )
        if settings.llama_cloud_api_key
        else None
    )
    embedding = DashScopeEmbeddingClient(
        api_key=settings.dashscope_api_key,
        base_url=settings.dashscope_base_url,
    )
    vector_store = QdrantVectorStore(
        client=_build_qdrant_client(settings.qdrant_url),
        collection_name=settings.qdrant_collection,
    )
    ingestion = KnowledgeIngestionService(
        store=store,
        loader=DocumentLoader(document_extractor=extractor),
        chunker=KnowledgeChunker(),
        embedding=embedding,
        vector_store=vector_store,
        embedding_model=settings.embedding_model,
        embedding_dimensions=settings.embedding_dimensions,
    )
    baseline = BaselineKnowledgeSearchService(
        store=store,
        embedding=embedding,
        vector_store=vector_store,
        retriever=HybridRetriever(
            store=store, embedding=embedding, vector_store=vector_store
        ),
        reranker=DashScopeQwenReranker(
            api_key=settings.dashscope_api_key,
            base_url=settings.rerank_base_url,
            model=settings.rerank_model,
            instruct=settings.rerank_instruct,
        ),
    )
    return RegressionServices(
        store=store, ingestion=ingestion, baseline=baseline
    )


def _build_qdrant_client(qdrant_url: str):
    """构造 Qdrant 客户端；localhost 显式绕过系统代理。

    与 ``create_knowledge_services`` 同样的处理：本机系统代理会让
    ``http://localhost:6333`` 的请求被代理劫持并报连接重置。
    """
    from urllib.parse import urlparse

    from qdrant_client import QdrantClient

    options = {"url": qdrant_url, "timeout": 30, "check_compatibility": False}
    if urlparse(qdrant_url).hostname in {"localhost", "127.0.0.1", "::1"}:
        options["trust_env"] = False
    return QdrantClient(**options)


def ensure_tenants(database_path: Path) -> None:
    """建好回归用的用户与三个企业（幂等）。"""
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT OR IGNORE INTO users(id, username, password_hash) VALUES (?, ?, ?)",
            (_REGRESSION_USER_ID, _REGRESSION_USER_ID, "not-used"),
        )
        for organization_id in FORMAT_ORGANIZATIONS.values():
            connection.execute(
                "INSERT OR IGNORE INTO organizations(id, name) VALUES (?, ?)",
                (organization_id, organization_id),
            )
            connection.execute(
                "INSERT OR IGNORE INTO memberships(organization_id, user_id, role) "
                "VALUES (?, ?, 'admin')",
                (organization_id, _REGRESSION_USER_ID),
            )


async def ingest_corpus(
    *,
    services: RegressionServices,
    organization_id: str,
    format_name: str,
    repo_root: Path,
    supporting: Sequence[tuple[str, Path]],
) -> None:
    """把一个企业的语料入库并**等到全部完成**。

    走的是生产同一条异步入库链路（queue_* + worker），因此这里验证的
    ``LOADER_VERSION`` / section 划分 / chunk 逻辑与线上完全一致。
    """
    store = services.store
    ingestion = services.ingestion
    worker = KnowledgeIngestionWorker(service=ingestion, store=store)
    ingestion.set_dispatcher(worker)

    plan: list[tuple[str, Path, DocumentSourceType]] = [
        (
            _TITLE,
            source_document_path(repo_root, format_name),
            SOURCE_TYPE_BY_FORMAT[format_name],
        )
    ]
    plan.extend(
        (title, path, DocumentSourceType.MARKDOWN) for title, path in supporting
    )
    existing_documents = {
        document.title: document
        for document in store.list_documents(organization_id=organization_id)
    }

    worker.bind_loop()
    try:
        for title, path, source_type in plan:
            content = path.read_bytes()
            existing = existing_documents.get(title)
            print(
                f"  [入队] {organization_id} / {title} "
                f"({source_type.value}, {len(content)} 字节)..."
            )
            queue = ingestion.queue_new_version if existing else ingestion.queue_new_document
            identity = {"document_id": existing.document_id} if existing else {"title": title}
            receipt = queue(
                organization_id=organization_id,
                uploaded_by_user_id=_REGRESSION_USER_ID,
                **identity,
                source_type=source_type,
                content=content,
            )
            await asyncio.wait_for(
                worker.drain(), timeout=_INGEST_TIMEOUT_SECONDS
            )
            job = store.get_latest_job_for_version(
                organization_id=organization_id,
                document_id="",
                version_id="",
                job_id=receipt.job_id,
            )
            status = job.status if job is not None else None
            if status is not IngestionStatus.SUCCEEDED:
                detail = job.error_code if job is not None else "unknown"
                raise RuntimeError(
                    f"入库未成功：{organization_id}/{title} "
                    f"status={status.value if status else 'missing'} code={detail}"
                )
            print(f"  [完成] {organization_id} / {title}")
    finally:
        await worker.stop()


async def run_regression(
    *,
    repo_root: Path,
    database_path: Path,
    cases_path: Path,
    formats: Sequence[str],
    collection_name: str | None = None,
) -> dict[str, object]:
    """跑完整回归并返回报告载荷。"""
    all_cases = load_stage_c_cases(cases_path)
    cases = select_general_service_cases(all_cases)
    if not cases:
        raise RuntimeError("cases.jsonl 里没有引用 general_service 的用例")
    print(f"[用例] 选中 {len(cases)} 个 general_service 用例")

    settings = get_knowledge_settings()
    if collection_name is not None:
        settings = replace(settings, qdrant_collection=collection_name)
    services = build_services(database_path=database_path, settings=settings)
    ensure_tenants(database_path)

    supporting = supporting_document_plan(repo_root)
    outcomes: dict[str, list[CaseOutcome]] = {}
    for format_name in formats:
        organization_id = FORMAT_ORGANIZATIONS[format_name]
        print(f"[语料] {format_name} -> {organization_id}")
        await ingest_corpus(
            services=services,
            organization_id=organization_id,
            format_name=format_name,
            repo_root=repo_root,
            supporting=supporting,
        )
        per_case: list[CaseOutcome] = []
        document_keys = {
            document.document_id: _DOCUMENT_KEY_BY_TITLE[document.title]
            for document in services.store.list_documents(organization_id=organization_id)
            if document.title in _DOCUMENT_KEY_BY_TITLE
        }
        for case in cases:
            print(f"  [检索] {case.case_id} ...")
            result = services.baseline.search(
                organization_id=organization_id,
                question=case.question,
            )
            per_case.append(score_search_result(case, result, document_keys))
        outcomes[format_name] = per_case

    return _compile_report(cases, outcomes, settings, formats)


def _compile_report(cases, outcomes, settings, formats) -> dict[str, object]:
    """把逐格式结果整理成可复核的报告，并给出三格式一致性结论。"""
    case_reports: list[dict[str, object]] = []
    for index, case in enumerate(cases):
        per_format: dict[str, object] = {}
        for format_name in formats:
            outcome = outcomes[format_name][index]
            per_format[format_name] = {
                "hit_count": outcome.hit_count,
                "group_hits": outcome.group_hits,
                "citation_heading_paths": outcome.citation_heading_paths,
                "expected_heading_matches": heading_exactness(case, outcome),
            }
        hit_counts = {
            name: per_format[name]["hit_count"] for name in formats
        }
        case_reports.append(
            {
                "case_id": case.case_id,
                "question": case.question,
                "expected_groups": [
                    group.group_id for group in case.required_evidence_groups
                ],
                "expected_general_service_headings": (
                    expected_general_service_headings(case)
                ),
                "per_format": per_format,
                "hit_counts_consistent": len(set(hit_counts.values())) == 1,
                "results_consistent": all(
                    per_format[name] == per_format[formats[0]] for name in formats
                ),
                "hit_counts": hit_counts,
            }
        )

    total_groups = sum(len(case.required_evidence_groups) for case in cases)
    return {
        "run": {
            "embedding_model": settings.embedding_model,
            "embedding_dimensions": settings.embedding_dimensions,
            "collection_name": settings.qdrant_collection,
            "rerank_model": settings.rerank_model,
            "organizations": {
                name: FORMAT_ORGANIZATIONS[name] for name in formats
            },
            "formats": list(formats),
        },
        "cases": case_reports,
        "summary": {
            "comparison_complete": set(formats) == set(FORMAT_ORGANIZATIONS),
            "all_results_consistent": all(
                report["results_consistent"] for report in case_reports
            ),
            "case_count": len(cases),
            "all_hit_counts_consistent": all(
                report["hit_counts_consistent"] for report in case_reports
            ),
            "all_expected_headings_matched": all(
                all(
                    all(per_format["expected_heading_matches"].values())
                    for per_format in report["per_format"].values()
                )
                for report in case_reports
            ),
            "per_format_group_hits": {
                name: sum(outcome.hit_count for outcome in outcomes[name])
                for name in formats
            },
            "total_groups": total_groups,
        },
    }


def render_markdown(report: dict[str, object]) -> str:
    """渲染便于人工复核的 Markdown 摘要。"""
    formats = report["run"]["formats"]
    lines = ["# docx / pdf / md 入库检索等价性回归（自动生成）", ""]
    summary = report["summary"]
    lines.append(f"- 已完成三格式比较：{'是' if summary['comparison_complete'] else '否（仅部分格式）'}")
    lines.append(f"- 证据组与引用路径逐项一致：{'是' if summary['all_results_consistent'] else '否'}")
    lines.append(f"- 用例数：{summary['case_count']}；证据组总数：{summary['total_groups']}")
    for format_name, hits in summary["per_format_group_hits"].items():
        lines.append(f"- {format_name} 命中证据组：{hits}")
    lines.append(
        f"- 三格式命中数一致：{'是' if summary['all_hit_counts_consistent'] else '否'}"
    )
    lines.append(
        f"- 全部期望 heading_path 被命中："
        f"{'是' if summary['all_expected_headings_matched'] else '否'}"
    )
    lines.append("")
    lines.append(
        "| 用例 | 期望 heading_path | "
        + " | ".join(formats)
        + " | 命中数一致 |"
    )
    lines.append("|---|" + "---|" * (len(formats) + 2))
    for case in report["cases"]:
        expected = "<br>".join(case["expected_general_service_headings"])
        cells = []
        for format_name in formats:
            per_format = case["per_format"][format_name]
            exact = per_format["expected_heading_matches"]
            exact_ok = all(exact.values()) if exact else True
            cells.append(
                f"{case['hit_counts'][format_name]}（精确{'✅' if exact_ok else '❌'}）"
            )
        lines.append(
            f"| {case['case_id']} | {expected} | "
            + " | ".join(cells)
            + f" | {'✅' if case['hit_counts_consistent'] else '❌'} |"
        )
    lines.append("")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="docx/pdf/md 三种入库形态的端到端检索等价性回归",
    )
    parser.add_argument("--repo-root", default=str(_REPO_ROOT))
    parser.add_argument("--database", default=str(DEFAULT_OUTPUT_DIR / "state.db"))
    parser.add_argument("--cases", default=str(DEFAULT_CASES_PATH))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_DIR / "report.json"))
    parser.add_argument(
        "--fresh", action="store_true",
        help="使用新数据库和独立向量集合重新入库；保留历史结果并复用解析缓存",
    )
    parser.add_argument(
        "--formats",
        nargs="+",
        choices=sorted(FORMAT_ORGANIZATIONS),
        default=sorted(FORMAT_ORGANIZATIONS),
        help="只跑指定形态（默认三种全跑；语料已入库时会跳过重复入库）",
    )
    args = parser.parse_args(argv)

    database = Path(args.database)
    output = Path(args.output)
    collection_name = None
    if args.fresh:
        run_id = uuid4().hex
        database = database.parent / run_id / database.name
        output = output.parent / run_id / output.name
        collection_name = f"supportpilot_format_{run_id}"
    database.parent.mkdir(parents=True, exist_ok=True)
    report = asyncio.run(
        run_regression(
            repo_root=Path(args.repo_root),
            database_path=database,
            cases_path=Path(args.cases),
            formats=args.formats,
            collection_name=collection_name,
        )
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    markdown_path = output.with_suffix(".md")
    markdown_path.write_text(render_markdown(report), encoding="utf-8")

    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"报告已写入：{output}")
    print(f"摘要已写入：{markdown_path}")
    return 0 if report["summary"]["all_results_consistent"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
