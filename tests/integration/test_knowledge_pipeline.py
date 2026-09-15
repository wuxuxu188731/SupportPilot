"""End-to-end knowledge pipeline integration tests against REAL Qdrant.

Unlike the unit tests (which use fake vector stores / fake chunkers), every
test here assembles the REAL pipeline over a REAL Qdrant collection:

    SQLiteKnowledgeStore + DocumentLoader + KnowledgeChunker
    + FakeEmbeddingClient                    (no external model spend)
    + QdrantVectorStore -> real Qdrant at QDRANT_URL

and drives it through the real ``KnowledgeIngestionService`` (ingest) and the
real ``BaselineKnowledgeSearchService`` (retrieve), so the whole
chunk->embed->upsert->activate->tenant/version-filtered-RRF->SQLite-validate
path runs for real. Tests skip when Qdrant is unreachable. Every test uses a
UNIQUE ``supportpilot_test_<uuid>`` collection deleted in ``finally``; the
production collection name is never touched.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from uuid import uuid4

import pytest
from qdrant_client import QdrantClient

from app.knowledge.base import (
    DocumentNotFoundError,
    DocumentSourceType,
    DocumentStatus,
    EmbeddingUnavailableError,
    IngestionStatus,
)
from app.knowledge.chunking import KnowledgeChunker
from app.knowledge.document_loader import DocumentLoader
from app.knowledge.embeddings import EmbeddingVector, SparseValue
from app.knowledge.ingestion import KnowledgeIngestionService
from app.knowledge.qdrant_store import QdrantVectorStore
from app.knowledge.retrieval import BaselineKnowledgeSearchService
from app.knowledge.sqlite_store import SQLiteKnowledgeStore
from app.organizations.sqlite_store import SQLiteOrganizationStore
from app.users.sqlite_store import SQLiteUserStore

PRODUCTION_COLLECTION = "supportpilot_knowledge_te4_1024_v1"


@dataclass(frozen=True)
class OrgContext:
    organization_id: str
    user_id: str


class FakeEmbeddingClient:
    """Deterministic 1024-dim dense+sparse embedding; can be switched to fail.

    ``fail=True`` raises ``EmbeddingUnavailableError`` from ``embed_documents``
    and ``embed_query`` so tests can exercise the ingestion-failure path
    without spending any external model quota.
    """

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.embed_document_calls = 0

    def _maybe_fail(self) -> None:
        if self.fail:
            raise EmbeddingUnavailableError(reason="fake embedding provider down")

    def embed_documents(self, texts):
        self._maybe_fail()
        self.embed_document_calls += 1
        return [self._vector(text) for text in texts]

    def embed_query(self, text, *, timeout_seconds=5):
        """按正式查询接口接收超时预算，返回确定性测试向量。"""
        self._maybe_fail()
        return self._vector(text)

    @staticmethod
    def _vector(text: str) -> EmbeddingVector:
        raw = hashlib.sha256(text.encode("utf-8")).digest()
        # A small, deterministic per-content dense bias so identical content
        # maps to an identical vector (cosine 1.0) and distinct content is still
        # a valid 1024-dim vector for COSINE search over real Qdrant.
        bias = (int.from_bytes(raw[0:8], "little") / (2**64) % 0.4) + 0.5
        dense = tuple(bias for _ in range(1024))
        idx = (int.from_bytes(raw[8:12], "little") % 5000) + 100
        sparse = (
            SparseValue(index=idx, value=1.0),
            SparseValue(index=idx + 1 if idx + 1 < 10000 else idx - 1, value=0.5),
        )
        return EmbeddingVector(dense=dense, sparse=sparse, token_count=4)


def _test_qdrant_url() -> str:
    import os

    return os.getenv("QDRANT_URL", "http://localhost:6333").rstrip("/")


@pytest.fixture
def real_client():
    client = QdrantClient(
        url=_test_qdrant_url(),
        timeout=10,
        check_compatibility=False,
    )
    try:
        client.get_collections()
    except Exception:  # noqa: BLE001 - any transport error => unavailable
        pytest.skip(f"Qdrant not reachable at {_test_qdrant_url()}")
    return client


@pytest.fixture
def unique_collection(real_client):
    name = f"supportpilot_test_{uuid4().hex}"
    yield name
    try:
        real_client.delete_collection(collection_name=name)
    except Exception:  # noqa: BLE001 - best-effort cleanup
        pass


@dataclass
class Pipeline:
    store: SQLiteKnowledgeStore
    ingestion: KnowledgeIngestionService
    baseline: BaselineKnowledgeSearchService
    embedding: FakeEmbeddingClient
    context_a: OrgContext
    context_b: OrgContext


def _org_contexts(database_path) -> tuple[OrgContext, OrgContext]:
    users = SQLiteUserStore(database_path)
    orgs = SQLiteOrganizationStore(database_path)
    contexts = []
    for name in ("alpha", "beta"):
        user = users.create_user(username=f"{name}-admin", password_hash="hash")
        org = orgs.create_with_admin(name=name, admin_user_id=user.user_id)
        contexts.append(
            OrgContext(
                organization_id=org.organization_id,
                user_id=user.user_id,
            )
        )
    return contexts[0], contexts[1]


@pytest.fixture
def pipeline(tmp_path, real_client, unique_collection):
    store = SQLiteKnowledgeStore(tmp_path / "knowledge.db")
    context_a, context_b = _org_contexts(tmp_path / "knowledge.db")

    loader = DocumentLoader()
    chunker = KnowledgeChunker()
    embedding = FakeEmbeddingClient()
    vector_store = QdrantVectorStore(
        client=real_client, collection_name=unique_collection
    )
    ingestion = KnowledgeIngestionService(
        store=store,
        loader=loader,
        chunker=chunker,
        embedding=embedding,
        vector_store=vector_store,
        embedding_model="text-embedding-v4",
        embedding_dimensions=1024,
    )
    baseline = BaselineKnowledgeSearchService(
        store=store, embedding=embedding, vector_store=vector_store
    )
    return Pipeline(
        store=store,
        ingestion=ingestion,
        baseline=baseline,
        embedding=embedding,
        context_a=context_a,
        context_b=context_b,
    )


def _ingest(pipe: Pipeline, ctx: OrgContext, *, title: str, body: str):
    receipt = pipe.ingestion.ingest_new_document(
        organization_id=ctx.organization_id,
        uploaded_by_user_id=ctx.user_id,
        title=title,
        source_type=DocumentSourceType.MARKDOWN,
        content=f"# {title}\n{body}".encode("utf-8"),
    )
    assert receipt.status is IngestionStatus.SUCCEEDED
    return receipt


@pytest.mark.integration
def test_same_title_conflicting_policies_are_tenant_isolated(pipeline):
    """Org A and org B upload equal-title, conflicting policies; a query from A
    returns only A citations and a query from B only B citations."""
    body_a = "退货：A公司允许收货后 90 天内无理由退货，全额退款。"
    body_b = "退货：B公司不允许任何退货，仅支持换货。"
    _ingest(pipeline, pipeline.context_a, title="退货政策", body=body_a)
    _ingest(pipeline, pipeline.context_b, title="退货政策", body=body_b)

    res_a = pipeline.baseline.search(
        organization_id=pipeline.context_a.organization_id,
        question="退货政策",
    )
    res_b = pipeline.baseline.search(
        organization_id=pipeline.context_b.organization_id,
        question="退货政策",
    )

    assert res_a.ok
    assert res_a.citations
    assert all(
        c.content == body_a or body_a in c.content for c in res_a.citations
    ), "A result leaks B's content"

    assert res_b.ok
    assert res_b.citations
    assert all(
        c.content == body_b or body_b in c.content for c in res_b.citations
    ), "B result leaks A's content"


@pytest.mark.integration
def test_new_version_activation_cites_only_v2(pipeline):
    """A uploads v1 then activates v2; afterwards only v2 chunks are cited."""
    v1 = _ingest(pipeline, pipeline.context_a, title="手册", body="旧版本第 1 章内容。")
    # v2 is a NEW version of the SAME document (distinct content -> new hash).
    v2 = pipeline.ingestion.ingest_new_version(
        organization_id=pipeline.context_a.organization_id,
        uploaded_by_user_id=pipeline.context_a.user_id,
        document_id=v1.document_id,
        source_type=DocumentSourceType.MARKDOWN,
        content="# 手册\n新版本第 1 章完全不同的内容。".encode("utf-8"),
    )
    assert v2.status is IngestionStatus.SUCCEEDED

    res = pipeline.baseline.search(
        organization_id=pipeline.context_a.organization_id,
        question="手册 第一章",
    )
    assert res.ok
    assert res.citations
    assert all(c.version_id == v2.version_id for c in res.citations), (
        "citation references the inactive v1"
    )


@pytest.mark.integration
def test_disabling_document_yields_empty_insufficient(pipeline):
    _ingest(pipeline, pipeline.context_a, title="政策", body="被停用的政策内容。")
    doc = pipeline.store.list_documents(
        organization_id=pipeline.context_a.organization_id
    )[0]
    pipeline.store.set_document_status(
        organization_id=pipeline.context_a.organization_id,
        document_id=doc.document_id,
        status=DocumentStatus.DISABLED,
    )

    res = pipeline.baseline.search(
        organization_id=pipeline.context_a.organization_id,
        question="政策",
    )
    assert res.ok  # retrieval ran; it is a healthy-but-empty result
    assert res.citations == []
    assert res.retrieval_summary.evidence_status == "insufficient"


@pytest.mark.integration
def test_embedding_failure_marks_job_failed_and_keeps_old_version(pipeline):
    """Fake embedding fails mid-ingestion: v2's job is FAILED with the stable
    code, the document stays active on v1, and a query still retrieves v1."""
    v1 = _ingest(pipeline, pipeline.context_a, title="手册", body="第一版内容。")

    pipeline.embedding.fail = True
    with pytest.raises(EmbeddingUnavailableError):
        pipeline.ingestion.ingest_new_version(
            organization_id=pipeline.context_a.organization_id,
            uploaded_by_user_id=pipeline.context_a.user_id,
            document_id=v1.document_id,
            source_type=DocumentSourceType.MARKDOWN,
            content="# 手册\n第二版的内容，但本次断供导致失败。".encode("utf-8"),
        )
    pipeline.embedding.fail = False

    # Document stays ACTIVE on v1; v1's job is still SUCCEEDED.
    doc = pipeline.store.get_document(
        organization_id=pipeline.context_a.organization_id,
        document_id=v1.document_id,
    )
    assert doc.status is DocumentStatus.ACTIVE
    assert doc.active_version_id == v1.version_id

    # v2's job is FAILED with the stable code.
    v2_version = pipeline.store.get_version_by_hash(
        organization_id=pipeline.context_a.organization_id,
        document_id=v1.document_id,
        content_hash=(
            "sha256:"
            + hashlib.sha256(
                "# 手册\n第二版的内容，但本次断供导致失败。".encode("utf-8")
            ).hexdigest()
        ),
    )
    v2_job = pipeline.store.get_latest_job_for_version(
        organization_id=pipeline.context_a.organization_id,
        document_id=v1.document_id,
        version_id=v2_version.version_id,
    )
    assert v2_job.status is IngestionStatus.FAILED
    assert v2_job.error_code == "EMBEDDING_UNAVAILABLE"

    # v1 citations are still retrievable end-to-end (through real Qdrant).
    res = pipeline.baseline.search(
        organization_id=pipeline.context_a.organization_id,
        question="手册 第一版",
    )
    assert res.ok
    assert res.citations
    assert all(c.version_id == v1.version_id for c in res.citations)


@pytest.mark.integration
def test_pipeline_never_touches_production_collection(pipeline, unique_collection):
    assert unique_collection.startswith("supportpilot_test_")
    assert unique_collection != PRODUCTION_COLLECTION
