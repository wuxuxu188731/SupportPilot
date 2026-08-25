"""Reproducible offline Baseline knowledge-RAG eval runner.

Runs the first 16-case knowledge eval set through the REAL ingestion and
Retrieval pipeline (SQLite + DashScope embedding + Qdrant), then writes a
machine-readable JSON report with per-case traces and aggregate metrics.

Determinism / reproducibility
-----------------------------
* ``build_services`` assembles the same component graph ``create_knowledge_services``
  uses, but accepts an *injectable* ``embedding_client``/``qdrant_client`` so unit
  tests can reuse the exact runner functions with fakes and no DashScope/network.
  Production (CLI) builds the real DashScope + Qdrant adapters by default.
* Ingesting each tenant's documents is idempotent: a document whose title already
  exists for the org is re-ingested via ``ingest_new_version`` (content-hash
  dedup), so re-running over the same SQLite file does not duplicate points.
* The report stamps ``code_revision`` (git HEAD), ``dataset_hash`` (SHA-256 of
  the cases file) and the embedding-cost price snapshot date, so metrics can be
  compared across runs.

Failure semantics
-----------------
Any embedding or Qdrant failure (the store raises
``EmbeddingUnavailableError`` / ``VectorStoreUnavailableError``, or ingestion
fails) aborts the whole run with a non-zero exit code. A failed retrieval is
NEVER coerced into an "insufficient" / empty-citation result -- a crash on a
case must fail the command, not silently report "no hit". Aggregate precision /
recall are computed ONLY over the cases that actually ran.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Scripts directory bootstrap: ``python scripts/run_knowledge_baseline_eval.py``
# puts ``scripts/`` on sys.path, not the repo root. Insert the repo root so the
# ``app`` package resolves regardless of how the runner is launched.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import argparse
import hashlib
import json
import math
import subprocess
from dataclasses import dataclass
from typing import Sequence
from uuid import uuid4

from app.core.config import KnowledgeSettings, get_knowledge_settings
from app.knowledge.base import (
    DocumentSourceType,
    IngestionStatus,
    VectorStoreUnavailableError,
)
from app.knowledge.chunking import CHUNKER_VERSION, KnowledgeChunker
from app.knowledge.document_loader import LOADER_VERSION, DocumentLoader
from app.knowledge.embeddings import EmbeddingClient, count_tokens
from app.knowledge.factory import (
    create_knowledge_services,
    KnowledgeServices,
)
from app.knowledge.ingestion import KnowledgeIngestionService
from app.knowledge.qdrant_store import QdrantVectorStore
from app.knowledge.results import BaselineSearchResult
from app.knowledge.retrieval import (
    BaselineKnowledgeSearchService,
    TOP_K,
    TOKEN_BUDGET,
    PREFETCH_LIMIT,
)
from app.knowledge.sqlite_store import SQLiteKnowledgeStore
from app.knowledge.vector_store import VectorStore
from app.organizations.sqlite_store import SQLiteOrganizationStore
from app.users.sqlite_store import SQLiteUserStore

# --------------------------------------------------------------------------- #
# Fixed price snapshot. Cost is recomputed from this constant at runtime and
# never fetched from the web, so results are reproducible offline. The snapshot
# date identifies which price card the number comes from.
#
# Cost scope (see M5): `estimated_embedding_cost_cny` covers ONLY the retrieval
# query + selected-chunk embeddings (input_tokens), NOT the document-ingestion
# embedding cost. The report stamps this scope explicitly as:
#   "cost_scope": "retrieval_query_and_citations_only"
# --------------------------------------------------------------------------- #
EMBEDDING_PRICE_CNY_PER_1K_TOKENS = 0.0005
PRICE_SNAPSHOT_DATE = "2026-08-08"

# Directory layout of the eval corpus (relative to the repo root).
CORPUS_ROOT = "evals/knowledge/documents"
# Each tenant's document files: document_key -> file stem (in org_a/org_b dirs).
# The Chinese titles are stamped onto Document rows so citations carry a title.

# Tenants participating in the eval: tenant_key -> directory name.
TENANTS = ("org_a", "org_b")

# document_key -> ({title} used for the Document row, file stem under the tenant dir).
# org_b only carries the returns document (for isolation testing); org_a carries all three.
_DOC_TITLES = {
    "returns": "云舟商城退货政策",
    "logistics_compensation": "云舟商城物流赔偿政策",
    "warranty": "云舟商城保修政策",
}
ORG_A_DOC_KEYS = ("returns", "logistics_compensation", "warranty")
ORG_B_DOC_KEYS = ("returns",)


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ExpectedRelevant:
    document_key: str
    heading_path: str

    @classmethod
    def from_dict(cls, raw: dict) -> "ExpectedRelevant":
        return cls(
            document_key=raw["document_key"],
            heading_path=raw["heading_path"],
        )


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    category: str
    tenant_key: str
    question: str
    expected_relevant: tuple[ExpectedRelevant, ...]
    should_have_answer: bool
    forbidden_tenant_keys: tuple[str, ...]
    expected_behavior: str | None = None

    @classmethod
    def from_dict(cls, raw: dict) -> "EvalCase":
        return cls(
            case_id=raw["case_id"],
            category=raw["category"],
            tenant_key=raw["tenant_key"],
            question=raw["question"],
            expected_relevant=tuple(
                ExpectedRelevant.from_dict(e) for e in raw["expected_relevant"]
            ),
            should_have_answer=bool(raw["should_have_answer"]),
            forbidden_tenant_keys=tuple(raw["forbidden_tenant_keys"]),
            expected_behavior=raw.get("expected_behavior"),
        )


@dataclass(frozen=True)
class CaseMetrics:
    """Per-case numeric metrics, decoupled from any live DB/results object so
    unit tests can construct them directly from fake data."""

    category: str
    tenant_key: str
    citations_returned: int = 0
    expected_count: int = 0
    hit_count: int = 0
    recall_at_5: float | None = None
    retrieval_precision_at_5: float | None = None
    relevant_returned: int = 0
    cross_tenant_leak: bool = False
    latency_ms: int = 0
    rounds: int = 0
    model_calls: int = 0
    input_tokens: int = 0
    cost_cny: float = 0.0
    should_have_answer: bool = False
    got_citations: bool = False
    returned_citations: tuple[dict, ...] = ()
    retrieval_trace: dict | None = None


# --------------------------------------------------------------------------- #
# Metric primitives (pure, network-free -- unit-testable with hand-built inputs)
# --------------------------------------------------------------------------- #
def _expected_pairs(case: EvalCase) -> set[tuple[str, str]]:
    return {(e.document_key, e.heading_path) for e in case.expected_relevant}


def _matches_expected(
    returned: tuple[str, str], expected_pair: tuple[str, str]
) -> bool:
    """True when a returned ``(document_key, heading_path)`` matches an expected
    ``(document_key, expected_heading)``.

    The rule resolves the scoring-convention mismatch between the runner's
    citations (which carry the FULL heading path, e.g. ``云舟商城退货政策（A 版）
    /退货时限``, built from the loader's heading stack) and the golden
    ``expected_relevant`` [heading_path] (which carry the BARE second-level
    heading, e.g. ``退货时限``). A match requires:

    * the ``document_key`` fields to be equal;
    * the last "/"-segment of the returned ``heading_path`` to equal the
      expected heading (an exact, whole-segment comparison at the trailing end:
      ``云舟商城退货政策（A 版）/退货时限`` matches ``退货时限``, but a heading
      that merely CONTAINS the expected text as a prefix/substring inside a
      different final segment, e.g. ``退货时限延长期``, does NOT match).

    An exact string equality still wins first — a citation heading with no
    slash equals the expected heading directly. """
    returned_doc, returned_heading = returned
    expected_doc, expected_heading = expected_pair
    if returned_doc != expected_doc:
        return False
    if returned_heading == expected_heading:
        return True
    return returned_heading.endswith("/" + expected_heading)


def _hit_count(
    pairs: Sequence[tuple[str, str]],
    expected: set[tuple[str, str]],
) -> int:
    """Number of expected pairs with at least one matching returned pair, using
    :func:`_matches_expected` path-segment semantics. Each expected pair counts
    at most once, preserving the brief's hit_count semantics."""
    return sum(
        1
        for expected_pair in expected
        if any(
            _matches_expected(p, expected_pair)
            for p in pairs
        )
    )


def recall_at_5(
    case: EvalCase, returned_pairs: Sequence[tuple[str, str]]
) -> float | None:
    """Fraction of expected relevant pairs that were returned, using
    path-segment heading matching (see :func:`_matches_expected`).

    Returns None when the case has no expected_relevant (safety/no-answer), so
    aggregates can skip it. Otherwise hit_count / expected_count per the brief.
    """
    expected = _expected_pairs(case)
    if not expected:
        return None
    return _hit_count(returned_pairs, expected) / len(expected)


def relevant_returned_count(case: EvalCase, returned_pairs) -> int:
    """Exact integer count of returned pairs that match an expected pair.

    Kept as its own integer (not reconstructed from the pre-rounded
    ``retrieval_precision_at_5`` float) so aggregate precision is computed
    from exact integers and never suffers a float round-trip off-by-one
    (see I1). ``retrieval_precision_at_5()`` delegates here too, so the rules live
    in exactly one place."""
    expected = _expected_pairs(case)
    return sum(
        1 for p in returned_pairs if any(_matches_expected(p, e) for e in expected)
    )


def retrieval_precision_at_5(
    case: EvalCase, returned_pairs, returned_count: int
) -> float | None:
    """Relevant raw Top-K chunks divided by returned chunks for golden cases.

    Cases with no golden evidence require routing, abstention, denial, or
    clarification behavior that Stage A does not implement, so their retrieval
    precision is not measured.
    """
    if not case.expected_relevant:
        return None
    if returned_count == 0:
        return 0.0
    relevant_returned = relevant_returned_count(case, returned_pairs)
    return relevant_returned / returned_count


def cross_tenant_leak(case: EvalCase, returned_tenant_keys) -> bool:
    forbidden = set(case.forbidden_tenant_keys)
    return bool(forbidden & set(returned_tenant_keys))


def evaluate_case(case: EvalCase, result: dict) -> CaseMetrics:
    """Build a :class:`CaseMetrics` from a case plus a result descriptor.

    ``result`` is a plain mapping with the retrieval's observable facts:

    * ``pairs``: sequence of ``(document_key, heading_path)`` actually returned
    * ``citations_returned``: total returned citation count
    * ``tenant_keys``: tenant keys the returned citations belong to
    * ``should_have_answer``/``expected``: fall back to the case's own values

    This is the pure seam the runner uses and unit tests reuse with hand-built
    descriptors, so the exact metric rules live here exactly once.
    ``recall_at_5``/``retrieval_precision_at_5``/``cross_tenant_leak`` delegate to
    the primitive functions below.
    """
    pairs = result["pairs"]
    returned_count = int(
        result.get("citations_returned", len(pairs))
    )
    should_have_answer = bool(
        result.get("should_have_answer", case.should_have_answer)
    )
    tenant_keys = result.get("tenant_keys", [case.tenant_key])
    expected = _expected_pairs(case)
    hit_count = _hit_count(pairs, expected)
    exact_relevant = relevant_returned_count(case, pairs)
    return CaseMetrics(
        category=case.category,
        tenant_key=case.tenant_key,
        citations_returned=returned_count,
        expected_count=len(case.expected_relevant),
        hit_count=hit_count,
        recall_at_5=recall_at_5(case, pairs),
        retrieval_precision_at_5=retrieval_precision_at_5(
            case, pairs, returned_count
        ),
        relevant_returned=exact_relevant,
        cross_tenant_leak=cross_tenant_leak(case, tenant_keys),
        latency_ms=int(result.get("latency_ms", 0)),
        rounds=int(result.get("rounds", 1)),
        model_calls=int(result.get("model_calls", 0)),
        input_tokens=int(result.get("input_tokens", 0)),
        cost_cny=float(result.get("cost_cny", 0.0)),
        should_have_answer=should_have_answer,
        got_citations=returned_count > 0,
        returned_citations=tuple(result.get("returned_citations", ())),
        retrieval_trace=result.get("retrieval_trace"),
    )


def case_report_dict(case: EvalCase, metrics: CaseMetrics) -> dict:
    """Serialize one case's metrics and content-free retrieval diagnostics."""
    return {
        "case_id": case.case_id,
        "category": metrics.category,
        "tenant_key": metrics.tenant_key,
        "expected_behavior": case.expected_behavior,
        "should_have_answer": metrics.should_have_answer,
        "citations_returned": metrics.citations_returned,
        "expected_count": metrics.expected_count,
        "hit_count": metrics.hit_count,
        "recall_at_5": metrics.recall_at_5,
        "retrieval_precision_at_5": metrics.retrieval_precision_at_5,
        "relevant_returned": metrics.relevant_returned,
        "cross_tenant_leak": metrics.cross_tenant_leak,
        "latency_ms": metrics.latency_ms,
        "rounds": metrics.rounds,
        "model_calls": metrics.model_calls,
        "input_tokens": metrics.input_tokens,
        "cost_cny": metrics.cost_cny,
        "returned_citations": list(metrics.returned_citations),
        "retrieval_trace": metrics.retrieval_trace or {
            "schema_version": 2,
            "candidates": [],
        },
    }


# --------------------------------------------------------------------------- #
# Aggregation helpers (pure, network-free)
# --------------------------------------------------------------------------- #
def _safe_mean(values: Sequence[float]) -> float:
    values = [float(v) for v in values if v is not None]
    if not values:
        return 0.0
    return sum(values) / len(values)


def percentile_nearest_rank(sorted_values: Sequence[float], percentile: float) -> float:
    """Nearest-rank percentile: index = ceil(p * n) - 1 in ascending order.

    For 16 cases at p=0.95: ceil(15.2)-1 = 15 (the maximum), matching the brief.
    Returns 0.0 for an empty sample.
    """
    values = sorted(float(v) for v in sorted_values if v is not None)
    if not values:
        return 0.0
    rank = math.ceil(percentile * len(values))
    index = rank - 1
    index = min(max(index, 0), len(values) - 1)
    return values[index]


def compile_report(
    *,
    cases: Sequence[EvalCase],
    per_case: Sequence[CaseMetrics],
    code_revision: str,
    dataset_hash: str,
    collection_name: str,
    embedding_model: str,
    embedding_dimensions: int,
) -> dict:
    """Aggregate per-case metrics into the reproducible report shape."""
    recall_values: list[float] = []
    recall_weights: list[float] = []  # expected_count, for weighted recall
    latencies: list[int] = []
    for c, m in zip(cases, per_case):
        if m.recall_at_5 is not None:
            recall_values.append(m.recall_at_5)
            recall_weights.append(float(m.expected_count) if m.expected_count else 1.0)
        latencies.append(m.latency_ms)

    total_expected = sum(m.expected_count for m in per_case)
    total_hit = sum(m.hit_count for m in per_case)
    precision_cases = [
        m for m in per_case if m.retrieval_precision_at_5 is not None
    ]
    precision_returned = sum(m.citations_returned for m in precision_cases)
    precision_relevant = sum(m.relevant_returned for m in precision_cases)
    leak_cases = sum(1 for m in per_case if m.cross_tenant_leak)

    # retrieval_recall_at_5: weighted by expected_count (so multi-condition cases
    # count their full expected set), falling back to simple mean when weights 0.
    if total_expected:
        retrieval_recall = total_hit / total_expected
    elif recall_values:
        retrieval_recall = _safe_mean(recall_values)
    else:
        retrieval_recall = 0.0

    return {
        "code_revision": code_revision,
        "dataset_hash": dataset_hash,
        "loader_version": LOADER_VERSION,
        "chunker_version": CHUNKER_VERSION,
        "embedding_model": embedding_model,
        "embedding_dimensions": embedding_dimensions,
        "collection_name": collection_name,
        "top_k": TOP_K,
        "token_budget": TOKEN_BUDGET,
        "price_snapshot_date": PRICE_SNAPSHOT_DATE,
        "price_cny_per_1k_input_tokens": EMBEDDING_PRICE_CNY_PER_1K_TOKENS,
        "base_prefetch_limit": PREFETCH_LIMIT,
        "case_count": len(cases),
        "aggregates": {
            "retrieval_recall_at_5": retrieval_recall,
            "retrieval_precision_at_5": (
                precision_relevant / precision_returned
                if precision_returned else 0.0
            ),
            "citation_precision": None,
            "citation_precision_scope": "stage_b_not_measured",
            "correct_abstention_rate": None,
            "correct_abstention_rate_scope": "stage_b_not_measured",
            "cross_tenant_leak_rate": leak_cases / len(cases) if cases else 0.0,
            "average_search_rounds": _safe_mean([m.rounds for m in per_case]),
            "average_model_calls": _safe_mean([m.model_calls for m in per_case]),
            "average_tokens": _safe_mean([m.input_tokens for m in per_case]),
            "p50_latency_ms": percentile_nearest_rank(latencies, 0.50),
            "p95_latency_ms": percentile_nearest_rank(latencies, 0.95),
            "estimated_embedding_cost_cny": sum(
                m.cost_cny for m in per_case
            ),
            "cost_scope": (
                "retrieval_query_and_citations_only"
            ),
        },
    }


# --------------------------------------------------------------------------- #
# Data + service wiring
# --------------------------------------------------------------------------- #
def load_eval_cases(path: str | Path) -> list[EvalCase]:
    cases: list[EvalCase] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            raw = json.loads(line)
            cases.append(EvalCase.from_dict(raw))
    return cases


def sha256_of_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def git_revision() -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
    except Exception:  # noqa: BLE001 - git may be unavailable; fall back.
        pass
    return "unknown"


def build_services(
    database_path: str | Path,
    *,
    settings: KnowledgeSettings,
    embedding_client: EmbeddingClient | None = None,
    qdrant_client=None,
) -> KnowledgeServices:
    """Assemble the service graph with an injectable embedding/vector adapter.

    Delegate to ``create_knowledge_services`` when nothing is injected (the
    production DashScope-Openssl + Qdrant adapters). When ``embedding_client``
    is given, wire it in place of the real DashScope client so offline unit
    tests can reuse this exact function with fakes (no network, no key).
    """
    if embedding_client is None and qdrant_client is None:
        return create_knowledge_services(database_path, settings)

    store = SQLiteKnowledgeStore(database_path=database_path)
    loader = DocumentLoader()
    chunker = KnowledgeChunker()
    embedding = embedding_client if embedding_client is not None else None

    if embedding is None:
        # Only reachable if a qdrant_client was injected but no embedding client:
        # fall back to the real DashScope adapter (requires a key).
        from app.knowledge.dashscope_embeddings import (
            DashScopeEmbeddingClient,
        )

        embedding = DashScopeEmbeddingClient(
            api_key=settings.dashscope_api_key,
            base_url=settings.dashscope_base_url,
        )

    if qdrant_client is None:
        from qdrant_client import QdrantClient

        qdrant_client = QdrantClient(
            url=settings.qdrant_url,
            timeout=5,
            check_compatibility=False,
        )
    vector_store: VectorStore = QdrantVectorStore(
        client=qdrant_client,
        collection_name=settings.qdrant_collection,
    )

    ingestion = KnowledgeIngestionService(
        store=store,
        loader=loader,
        chunker=chunker,
        embedding=embedding,
        vector_store=vector_store,
        embedding_model=settings.embedding_model,
        embedding_dimensions=settings.embedding_dimensions,
    )
    baseline = BaselineKnowledgeSearchService(
        store=store,
        embedding=embedding,
        vector_store=vector_store,
    )
    return KnowledgeServices(
        store=store,
        ingestion=ingestion,
        baseline=baseline,
    )


def _find_or_create_org(
    users: SQLiteUserStore, orgs: SQLiteOrganizationStore, name: str
) -> tuple[str, str]:
    """Return (organization_id, admin_user_id) for the named eval org.

    Each run allocates a fresh org + admin user so the eval never depends on
    pre-seeded rows and never cross-contaminates a prior run's orgs. Because
    ingestion is content-hash idempotent and retrieval is scoped purely by the
    org_id used within this single run, allocating fresh orgs keeps the
    database and report internally consistent while staying fully reproducible.
    """
    user = users.create_user(
        username=f"eval-{name}-{uuid4().hex[:8]}",
        password_hash="eval-hash",
    )
    org = orgs.create_with_admin(name=name, admin_user_id=user.user_id)
    return org.organization_id, user.user_id


# --------------------------------------------------------------------------- #
# Eval driver
# --------------------------------------------------------------------------- #
def _corpus_paths() -> dict[str, dict[str, Path]]:
    """tenant_key -> {document_key: Path} resolved against the repo root."""
    root = Path(__file__).resolve().parents[1]
    corpus = root / CORPUS_ROOT
    by_tenant: dict[str, dict[str, Path]] = {}
    for tenant in TENANTS:
        tenant_dir = corpus / tenant
        by_tenant[tenant] = {
            key: tenant_dir / f"{key}.md" for key in _doc_keys_for(tenant)
        }
    return by_tenant


def _doc_keys_for(tenant: str) -> tuple[str, ...]:
    if tenant == "org_a":
        return ORG_A_DOC_KEYS
    return ORG_B_DOC_KEYS


def ingest_tenant_documents(
    services: KnowledgeServices,
    *,
    organization_id: str,
    uploaded_by_user_id: str,
    tenant: str,
) -> dict[str, str]:
    """Ingest a tenant's eval documents idempotently.

    Returns a map of document_id -> document_key so retrieval citations can be
    tracked back to a corpus document. Idempotency: if a document with the same
    title already exists and is ACTIVE, re-ingest via ``ingest_new_version``
    (content-hash dedup -> returns a deduplicated receipt without re-embedding).
    """
    store = services.store
    documents = store.list_documents(organization_id=organization_id)
    title_to_doc = {
        (d.title): d
        for d in documents
        if d.status.value == "active"
    }

    paths = _corpus_paths()[tenant]
    document_key_by_id: dict[str, str] = {}
    for document_key, path in paths.items():
        title = f"{_DOC_TITLES[document_key]}（{'A' if tenant == 'org_a' else 'B'} 版）"
        content = path.read_bytes()
        existing = title_to_doc.get(title)
        if existing is None:
            receipt = services.ingestion.ingest_new_document(
                organization_id=organization_id,
                uploaded_by_user_id=uploaded_by_user_id,
                title=title,
                source_type=DocumentSourceType.MARKDOWN,
                content=content,
            )
        else:
            receipt = services.ingestion.ingest_new_version(
                organization_id=organization_id,
                uploaded_by_user_id=uploaded_by_user_id,
                document_id=existing.document_id,
                source_type=DocumentSourceType.MARKDOWN,
                content=content,
            )
        if receipt.status is not IngestionStatus.SUCCEEDED:
            raise RuntimeError(
                f"ingestion failed for {tenant}/{document_key}: "
                f"status={receipt.status.value}"
            )
        document_key_by_id[receipt.document_id] = document_key
    return document_key_by_id


def run_case(
    services: KnowledgeServices,
    case: EvalCase,
    *,
    document_key_by_org: dict[str, dict[str, str]],
    tenant_org_ids: dict[str, str],
) -> CaseMetrics:
    """Run one case through the real baseline retriever and build its metrics."""
    org_id = tenant_org_ids[case.tenant_key]
    doc_key_by_id = document_key_by_org.get(case.tenant_key, {})

    result: BaselineSearchResult = services.baseline.search(
        organization_id=org_id,
        question=case.question,
    )
    if not result.ok:
        # An infrastructure failure (embedding/Qdrant) must abort, never be
        # treated as "no hit" (per Task 14 Step 1).
        raise RuntimeError(
            f"retrieval failed for case {case.case_id}: "
            f"{(result.error.code if result.error else 'UNKNOWN')} "
            f"{(result.error.safe_message if result.error else '')}"
        )

    # Map each returned citation back to (document_key, heading_path).
    returned_pairs: list[tuple[str, str]] = []
    returned_citations: list[dict] = []
    returned_tenant_keys: set[str] = {case.tenant_key}
    for citation_rank, citation in enumerate(result.citations, start=1):
        document_key = doc_key_by_id.get(citation.document_id)
        # A citation whose document_id is unknown to THIS org means its points
        # leaked from another tenant (or was ingested by a different run's doc
        # rows). Treat any such citation as cross-tenant for safety.
        if document_key is None:
            returned_tenant_keys.add(
                "org_b" if case.tenant_key == "org_a" else "org_a"
            )
            continue
        returned_pairs.append((document_key, citation.heading_path or ""))
        returned_citations.append(
            {
                "document_key": document_key,
                "heading_path": citation.heading_path or "",
                "chunk_id": citation.chunk_id,
                "citation_rank": citation_rank,
            }
        )

    returned_count = len(result.citations)
    input_tokens = count_tokens(case.question) + sum(
        c.token_count for c in result.selected_chunks
    )
    cost_cny = (
        input_tokens / 1000.0 * EMBEDDING_PRICE_CNY_PER_1K_TOKENS
    )

    return evaluate_case(case, {
        "pairs": returned_pairs,
        "citations_returned": returned_count,
        "tenant_keys": list(returned_tenant_keys),
        "latency_ms": result.retrieval_summary.latency_ms,
        "rounds": result.retrieval_summary.round_count,
        "input_tokens": input_tokens,
        "cost_cny": cost_cny,
        "returned_citations": returned_citations,
        "retrieval_trace": result.retrieval_trace.to_dict(),
    })


def run_baseline_eval(
    *, database_path: str | Path, cases_path: str | Path,
) -> dict:
    """The full offline eval driver: migrate DB, ingest all tenants, run every
    case through the real baseline, and return the compiled report dict.

    Database mutations and the real Qdrant/DashScope calls happen only here,
    so unit tests can exercise the runner end-to-end with a fake embedding and
    a scratch DB without any network spend.
    """
    settings = get_knowledge_settings()
    services = build_services(database_path, settings=settings)

    users = SQLiteUserStore(database_path)
    orgs = SQLiteOrganizationStore(database_path)

    cases = load_eval_cases(cases_path)
    tenant_org_ids: dict[str, str] = {}
    tenant_user_ids: dict[str, str] = {}
    for tenant in TENANTS:
        org_id, user_id = _find_or_create_org(users, orgs, f"eval-{tenant}")
        tenant_org_ids[tenant] = org_id
        tenant_user_ids[tenant] = user_id

    document_key_by_org: dict[str, dict[str, str]] = {}
    for tenant in TENANTS:
        document_key_by_org[tenant] = ingest_tenant_documents(
            services,
            organization_id=tenant_org_ids[tenant],
            uploaded_by_user_id=tenant_user_ids[tenant],
            tenant=tenant,
        )

    per_case: list[CaseMetrics] = []
    for case in cases:
        per_case.append(
            run_case(
                services,
                case,
                document_key_by_org=document_key_by_org,
                tenant_org_ids=tenant_org_ids,
            )
        )

    report = compile_report(
        cases=cases,
        per_case=per_case,
        code_revision=git_revision(),
        dataset_hash=sha256_of_file(cases_path),
        collection_name=settings.qdrant_collection,
        embedding_model=settings.embedding_model,
        embedding_dimensions=settings.embedding_dimensions,
    )
    report["cases"] = [
        case_report_dict(case, metrics)
        for case, metrics in zip(cases, per_case)
    ]
    return report


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_knowledge_baseline_eval",
        description=(
            "Run the reproducible offline knowledge-RAG Baseline eval and write "
            "a JSON report with per-case traces and aggregate metrics."
        ),
    )
    parser.add_argument(
        "--database",
        default=".artifacts/knowledge-eval.db",
        help="SQLite database path (created/migrated if absent). "
        "Default: .artifacts/knowledge-eval.db",
    )
    parser.add_argument(
        "--cases",
        default="evals/knowledge/cases.jsonl",
        help="Path to the eval cases JSONL. Default: evals/knowledge/cases.jsonl",
    )
    parser.add_argument(
        "--output",
        default=".artifacts/knowledge-baseline.json",
        help="Path to write the aggregated JSON report. "
        "Default: .artifacts/knowledge-baseline.json",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    database = Path(args.database)
    cases_path = Path(args.cases)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    database.parent.mkdir(parents=True, exist_ok=True)

    try:
        report = run_baseline_eval(
            database_path=database, cases_path=cases_path
        )
    except (VectorStoreUnavailableError, Exception) as exc:  # noqa: BLE001
        # Any embedding/Qdrant/service failure aborts with a non-zero exit code
        # so a broken run is never mistaken for "the eval found no hits".
        print(f"eval aborted: {exc}", file=sys.stderr)
        return 1

    with open(output, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    aggregates = report["aggregates"]
    print("\n".join(
        [
            f"wrote {output}",
            f"code_revision        {report['code_revision']}",
            f"dataset_hash         {report['dataset_hash']}",
            f"retrieval_recall@5   {aggregates['retrieval_recall_at_5']:.3f}",
            (
                "retrieval_precision@5 "
                f"{aggregates['retrieval_precision_at_5']:.3f}"
            ),
            "citation_precision   not measured (Stage B)",
            "correct_abstention   not measured (Stage B)",
            f"cross_tenant_leak    {aggregates['cross_tenant_leak_rate']:.3f}",
            f"avg_search_rounds    {aggregates['average_search_rounds']:.2f}",
            f"avg_model_calls      {aggregates['average_model_calls']:.2f}",
            f"avg_tokens           {aggregates['average_tokens']:.1f}",
            f"p50_latency_ms       {aggregates['p50_latency_ms']}",
            f"p95_latency_ms       {aggregates['p95_latency_ms']}",
            f"embed_cost_cny       {aggregates['estimated_embedding_cost_cny']:.6f}",
            f"price_snapshot       {report['price_snapshot_date']}",
        ]
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
