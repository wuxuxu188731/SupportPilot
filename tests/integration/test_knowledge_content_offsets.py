"""知识库正文偏移的端到端集成测试（离线跑，无网络、无 Qdrant）。

覆盖的是本次「正文查看 + 引用跳转」功能的**完整闭环**：

    上传/入库（真实 KnowledgeIngestionService + 真实 SQLite）
      → 切块（真实 KnowledgeChunker，写 start_offset/end_offset）
      → 检索（真实 BaselineKnowledgeSearchService，只把向量库换成内存实现）
      → 结构化引用（带偏移）
      → HTTP 读取正文（真实路由 + 真实 store）
      → 断言 text[start:end] == citation.content

与 `test_knowledge_pipeline.py` 的区别：那个文件面向真实 Qdrant（不可达即 skip），
本文件用内存向量库，因此在任何环境下都会真正执行——偏移契约是纯数据问题，
不该因为"本机没起 Qdrant"而失去覆盖。中文文档、同步与异步入库两条路径
都在覆盖范围内。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.knowledge_router import create_knowledge_router
from app.application.organization_service import TenantContext
from app.knowledge.base import (
    DocumentSourceType,
    DocumentStatus,
    IngestionStatus,
    VectorStoreUnavailableError,
)
from app.knowledge.chunking import KnowledgeChunker
from app.knowledge.document_loader import DocumentLoader
from app.knowledge.embeddings import EmbeddingVector, SparseValue
from app.knowledge.ingestion import KnowledgeIngestionService
from app.knowledge.retrieval import BaselineKnowledgeSearchService
from app.knowledge.sqlite_store import SQLiteKnowledgeStore
from app.knowledge.vector_store import VectorCandidate, VectorPoint
from app.organizations.base import MembershipRole
from app.organizations.sqlite_store import SQLiteOrganizationStore
from app.users.sqlite_store import SQLiteUserStore

# 中文正文：标题层级 + 多段落，确保多字节字符下的偏移计算被真实覆盖。
DOCUMENT_TITLE = "退货与换货政策"
DOCUMENT_TEXT = (
    "# 退货与换货政策\n"
    "\n"
    "本政策适用于云舟商城全部自营商品。\n"
    "\n"
    "## 1. 无理由退货\n"
    "\n"
    "签收后 7 日内可申请无理由退货，商品需保持完好。\n"
    "\n"
    "## 2. 换货流程\n"
    "\n"
    "换货申请审核通过后 3 个工作日内寄出新品。\n"
)


@dataclass(frozen=True)
class OrgContext:
    """测试用的企业上下文（企业 id + 上传者用户 id）。"""

    organization_id: str
    user_id: str


class FakeEmbedding:
    """确定性 1024 维稠密+稀疏向量：同文本同向量，无任何外部调用。"""

    def embed_documents(self, texts):
        return [self._vector(text) for text in texts]

    def embed_query(self, text, *, timeout_seconds=5):
        return self._vector(text)

    @staticmethod
    def _vector(text: str) -> EmbeddingVector:
        raw = hashlib.sha256(text.encode("utf-8")).digest()
        bias = (int.from_bytes(raw[0:8], "little") / (2**64) % 0.4) + 0.5
        index = (int.from_bytes(raw[8:12], "little") % 5000) + 100
        return EmbeddingVector(
            dense=tuple(bias for _ in range(1024)),
            sparse=(SparseValue(index=index, value=1.0),),
            token_count=len(text),
        )


class InMemoryVectorStore:
    """内存向量库：实现 VectorStore 协议，语义与 Qdrant 实现一致（含租户过滤）。

    ``search`` 模拟 Qdrant 的两条硬约束：只返回同一个企业的点，且只返回
    ``active_version_ids`` 里的版本——这样"停用文档/旧版本不再被检索"的行为
    在离线环境下也会被真实执行，而不是靠假 store 假装过滤掉了。
    """

    def __init__(self) -> None:
        self.points: dict[str, VectorPoint] = {}
        self.ensure_collections = 0
        self.fail = False

    def ensure_collection(self) -> None:
        self.ensure_collections += 1

    def upsert(self, *, points) -> None:
        if self.fail:
            raise VectorStoreUnavailableError(reason="fake vector store is down")
        for point in points:
            self.points[point.chunk_id] = point

    def validate_point_identities(self, *, expected) -> None:
        for identity in expected:
            stored = self.points.get(identity.chunk_id)
            if stored is None:
                raise AssertionError(f"missing vector point {identity.chunk_id}")
            if (
                stored.organization_id != identity.organization_id
                or stored.document_id != identity.document_id
                or stored.version_id != identity.version_id
                or stored.ordinal != identity.ordinal
            ):
                raise AssertionError(f"point identity mismatch {identity.chunk_id}")

    def search(
        self,
        *,
        organization_id: str,
        active_version_ids,
        query_embedding: EmbeddingVector,
        prefetch_limit: int,
        result_limit: int,
        timeout_seconds: int = 5,
    ) -> list[VectorCandidate]:
        active = set(active_version_ids)
        scored: list[tuple[float, VectorPoint]] = []
        for point in self.points.values():
            if point.organization_id != organization_id:
                continue
            if point.version_id not in active:
                continue
            scored.append(
                (_cosine(query_embedding.dense, point.embedding.dense), point)
            )
        scored.sort(key=lambda item: item[0], reverse=True)
        return [
            VectorCandidate(
                chunk_id=point.chunk_id,
                document_id=point.document_id,
                version_id=point.version_id,
                ordinal=point.ordinal,
                score=score,
            )
            for score, point in scored[:result_limit]
        ]


def _cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    """余弦相似度；测试向量非零，无需处理零向量。"""
    dot = sum(a * b for a, b in zip(left, right))
    norm_left = sum(a * a for a in left) ** 0.5
    norm_right = sum(b * b for b in right) ** 0.5
    return dot / (norm_left * norm_right)


@dataclass
class Harness:
    """一套装好的离线知识库装置：真实 store + 真实服务 + 内存向量库 + 真实路由。"""

    store: SQLiteKnowledgeStore
    ingestion: KnowledgeIngestionService
    baseline: BaselineKnowledgeSearchService
    vectors: InMemoryVectorStore
    context_a: OrgContext
    context_b: OrgContext
    current_context: dict[str, OrgContext]
    client: TestClient

    def authenticated_as(self, context: OrgContext) -> None:
        """切换当前请求的租户上下文（模拟不同企业的成员登录）。"""
        self.current_context["context"] = context


def _org_contexts(database_path) -> tuple[OrgContext, OrgContext]:
    """建两个真实企业（含真实成员关系），用于验证跨租户隔离。"""
    users = SQLiteUserStore(database_path)
    organizations = SQLiteOrganizationStore(database_path)
    contexts: list[OrgContext] = []
    for name in ("alpha", "beta"):
        user = users.create_user(username=f"{name}-admin", password_hash="hash")
        organization = organizations.create_with_admin(
            name=name, admin_user_id=user.user_id
        )
        contexts.append(
            OrgContext(
                organization_id=organization.organization_id,
                user_id=user.user_id,
            )
        )
    return contexts[0], contexts[1]


@pytest.fixture
def harness(tmp_path) -> Harness:
    database_path = tmp_path / "knowledge.db"
    store = SQLiteKnowledgeStore(database_path)
    context_a, context_b = _org_contexts(database_path)

    embedding = FakeEmbedding()
    vectors = InMemoryVectorStore()
    ingestion = KnowledgeIngestionService(
        store=store,
        loader=DocumentLoader(),
        chunker=KnowledgeChunker(),
        embedding=embedding,
        vector_store=vectors,
    )
    baseline = BaselineKnowledgeSearchService(
        store=store, embedding=embedding, vector_store=vectors
    )

    # 路由的租户上下文可切换：同一个 app 内模拟不同企业的成员调用。
    current_context: dict[str, OrgContext] = {"context": context_a}

    def get_current_tenant() -> TenantContext:
        context = current_context["context"]
        return TenantContext(
            user_id=context.user_id,
            organization_id=context.organization_id,
            role=MembershipRole.ADMIN,
        )

    app = FastAPI()
    app.include_router(
        create_knowledge_router(
            ingestion_service=ingestion,
            knowledge_store=store,
            get_current_tenant=get_current_tenant,
        )
    )
    return Harness(
        store=store,
        ingestion=ingestion,
        baseline=baseline,
        vectors=vectors,
        context_a=context_a,
        context_b=context_b,
        current_context=current_context,
        client=TestClient(app),
    )


def _ingest_sync(harness: Harness, context: OrgContext):
    """同步入库路径：直接跑完整管线并返回回执。"""
    receipt = harness.ingestion.ingest_new_document(
        organization_id=context.organization_id,
        uploaded_by_user_id=context.user_id,
        title=DOCUMENT_TITLE,
        source_type=DocumentSourceType.MARKDOWN,
        content=DOCUMENT_TEXT.encode("utf-8"),
    )
    assert receipt.status is IngestionStatus.SUCCEEDED
    return receipt


def _content_url(document_id: str, version_id: str) -> str:
    return f"/knowledge/documents/{document_id}/versions/{version_id}/content/"


def _assert_citation_slices_match(harness: Harness, document_id: str, version_id: str):
    """检索一轮，并逐条断言引用内容能在正文里被精确切出来。

    返回 (正文, 引用列表)，供调用方继续做其它断言。
    """
    result = harness.baseline.search(
        organization_id=harness.context_a.organization_id,
        question="退货 换货 政策",
    )
    assert result.ok, "检索未跑通"
    assert result.citations, "没有召回到任何引用"

    harness.authenticated_as(harness.context_a)
    response = harness.client.get(_content_url(document_id, version_id))
    assert response.status_code == 200
    body = response.json()
    text = body["text"]

    # 偏移自检的基准：字符数必须与正文实际长度一致（不是 utf-8 字节数）。
    assert body["text_length"] == len(text)
    assert body["text_length"] == len(DOCUMENT_TEXT)

    for citation in result.citations:
        assert citation.document_id == document_id
        assert citation.version_id == version_id
        # 核心契约：引用正文就是正文的 [start_offset, end_offset) 切片。
        assert citation.start_offset is not None
        assert citation.end_offset is not None
        assert 0 <= citation.start_offset < citation.end_offset <= len(text)
        assert text[citation.start_offset : citation.end_offset] == citation.content
        # 引用正文必须真的出现在正文里（多字节字符切成半个字会在这里暴露）。
        assert citation.content in text

    return text, result.citations


def test_sync_ingestion_citation_offsets_slice_the_stored_markdown(harness):
    # 保护行为（同步入库）：从上传到"用引用偏移在正文里切出原文"的完整闭环，
    # 在中文文档上逐条成立。
    receipt = _ingest_sync(harness, harness.context_a)

    text, citations = _assert_citation_slices_match(
        harness, receipt.document_id, receipt.version_id
    )

    # 目录与正文同源：每个目录项的偏移都落在对应标题行首。
    harness.authenticated_as(harness.context_a)
    body = harness.client.get(
        _content_url(receipt.document_id, receipt.version_id)
    ).json()
    assert body["outline"], "中文 Markdown 应解析出目录"
    for item in body["outline"]:
        assert text[item["char_offset"] :].startswith("#" * item["level"])
    # 每个引用的 heading_path 都能在目录里找到（前端"降级跳到章节"的依据）。
    outline_paths = {item["heading_path"] for item in body["outline"]}
    for citation in citations:
        if citation.heading_path is not None:
            assert citation.heading_path in outline_paths


def test_async_ingestion_citation_offsets_slice_the_stored_markdown(harness):
    # 保护行为（异步入库）：上传只登记入队、解析与切块在 worker 里完成，
    # 偏移仍然相对最终落库的 raw_text —— 这条链路是 HTTP 上传的真实路径。
    queue_receipt = harness.ingestion.queue_new_document(
        organization_id=harness.context_a.organization_id,
        uploaded_by_user_id=harness.context_a.user_id,
        title=DOCUMENT_TITLE,
        source_type=DocumentSourceType.MARKDOWN,
        content=DOCUMENT_TEXT.encode("utf-8"),
    )
    assert queue_receipt.status is IngestionStatus.QUEUED
    # 入库前队列里没有向量点，说明确实还没解析（避免测试假装是异步）。
    assert harness.vectors.points == {}

    executed = harness.ingestion.run_job(
        organization_id=harness.context_a.organization_id,
        job_id=queue_receipt.job_id,
    )
    assert executed is not None
    assert executed.status is IngestionStatus.SUCCEEDED

    _assert_citation_slices_match(
        harness, queue_receipt.document_id, queue_receipt.version_id
    )


def test_http_upload_then_citation_jump_round_trip(harness):
    # 保护行为：走真实 HTTP 上传（异步入库 + 任务轮询到终态），再走真实 HTTP
    # 读正文，最后用检索到的引用偏移在正文里切出原文——接口对外行为层面的闭环。
    harness.authenticated_as(harness.context_a)
    upload = harness.client.post(
        "/knowledge/documents/",
        data={"title": DOCUMENT_TITLE},
        files={
            "file": (
                "退货与换货政策.md",
                DOCUMENT_TEXT.encode("utf-8"),
                "text/markdown",
            )
        },
    )
    assert upload.status_code == 201
    receipt = upload.json()
    assert receipt["status"] == "queued"

    # 上传接口不等待解析：这里手动推进任务（生产由进程内 worker 执行）。
    harness.ingestion.run_job(
        organization_id=harness.context_a.organization_id,
        job_id=receipt["job_id"],
    )

    job = harness.client.get(f"/knowledge/ingestion-jobs/{receipt['job_id']}/").json()
    assert job["status"] == "succeeded"

    _assert_citation_slices_match(
        harness, receipt["document_id"], receipt["version_id"]
    )


def test_content_endpoint_keeps_tenants_isolated_after_real_ingestion(harness):
    # 边界情况（跨租户）：A 企业真实入库后，B 企业成员即使拿到完整的
    # document_id + version_id，也读不到正文，且响应里不含 A 的任何内容。
    receipt = _ingest_sync(harness, harness.context_a)

    harness.authenticated_as(harness.context_b)
    response = harness.client.get(
        _content_url(receipt.document_id, receipt.version_id)
    )

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "DOCUMENT_NOT_FOUND"
    assert DOCUMENT_TITLE not in response.text
    assert "无理由退货" not in response.text


def test_disabled_document_content_still_readable_but_not_retrieved(harness):
    # 保护行为：停用后不再被检索（引用消失），但正文仍可读——
    # 历史引用可能正指向这份已停用文档。
    receipt = _ingest_sync(harness, harness.context_a)
    harness.store.set_document_status(
        organization_id=harness.context_a.organization_id,
        document_id=receipt.document_id,
        status=DocumentStatus.DISABLED,
    )

    result = harness.baseline.search(
        organization_id=harness.context_a.organization_id,
        question="退货 换货 政策",
    )
    assert result.ok
    assert result.citations == []

    harness.authenticated_as(harness.context_a)
    response = harness.client.get(
        _content_url(receipt.document_id, receipt.version_id)
    )
    assert response.status_code == 200
    assert response.json()["status"] == "disabled"
    assert response.json()["text"] == DOCUMENT_TEXT
