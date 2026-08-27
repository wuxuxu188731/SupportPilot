"""Baseline fixed Top-K retrieval tests.

Uses recording fakes over every dependency boundary (store, embedding, vector
store) so the service is exercised with zero network I/O. The fake vector store
returns configurable candidates and records the exact search call the service
builds; the fake store performs the second-pass ``resolve_active_citations``
validation exactly like the real SQLite store (only active, same-org, current-
version chunks survive).
"""

import json
from types import SimpleNamespace

import pytest

from app.knowledge.base import (
    ChunkWithDocumentTitle,
    EmbeddingUnavailableError,
    RerankUnavailableError,
    VectorStoreUnavailableError,
)
from app.knowledge.embeddings import EmbeddingVector
from app.knowledge.retrieval import BaselineKnowledgeSearchService, HybridRetriever


def _chunk_ref(
    *,
    chunk_id,
    document_id="doc-a",
    version_id="version-a",
    ordinal=0,
    title="退货政策",
    heading_path=None,
    content="policy content",
    token_count=10,
    organization_id="org-a",
):
    return ChunkWithDocumentTitle(
        chunk_id=chunk_id,
        organization_id=organization_id,
        document_id=document_id,
        version_id=version_id,
        ordinal=ordinal,
        heading_path=heading_path,
        content=content,
        token_count=token_count,
        document_title=title,
    )


class _Store:
    """Fake KnowledgeStore that only knows ALREADY-validated active chunks.

    ``active`` maps chunk_id -> ChunkWithDocumentTitle for chunks that are
    genuinely active + same-org + current-version (the real store mirrors this
    via the status/active_version_id/four-level-id conditions). So anything the
    fake Qdrant returns that is an old version or a foreign org simply has no
    entry here and is filtered out by ``resolve_active_citations``.
    """

    def __init__(self, events):
        self.events = events
        self._active_version_ids = {"org-a": ["version-a"]}
        self.active: dict[str, ChunkWithDocumentTitle] = {}

    def list_active_version_ids(self, *, organization_id):
        return self._active_version_ids.get(organization_id, [])

    def resolve_active_citations(self, *, organization_id, candidate_ids):
        # Returns in the store's own insertion order, NOT candidate order, to
        # prove the service rebuilds citation order from candidate scores via
        # candidate_by_id and never trusts the DB default order.
        wanted = set(candidate_ids)
        resolved = []
        for ref in self.active.values():
            if ref.chunk_id in wanted and ref.organization_id == organization_id:
                resolved.append(ref)
        return resolved

    def record_retrieval_event(self, *, organization_id, event):
        self.events.append(event)
        return event


class _Embedding:
    def __init__(self):
        self.query_calls = []
        self.query_timeouts = []

    def embed_query(self, text, *, timeout_seconds=5):
        self.query_calls.append(text)
        self.query_timeouts.append(timeout_seconds)
        # token_count grows with each query so tests can detect extra calls.
        return EmbeddingVector(
            dense=(0.1,) * 3,
            sparse=(),
            token_count=3 * len(self.query_calls),
        )


class _Vector:
    def __init__(self):
        self.search_calls = []
        self.candidates = []
        self.error = None

    def search(self, **kwargs):
        if self.error is not None:
            raise self.error
        self.search_calls.append(kwargs)
        return self.candidates


@pytest.fixture
def retrieval_scope():
    events = []
    store = _Store(events)
    store.active["chunk-a0"] = _chunk_ref(
        chunk_id="chunk-a0", ordinal=0, token_count=20,
        content="七天无理由退货", heading_path="/退货政策",
    )
    store.active["chunk-a1"] = _chunk_ref(
        chunk_id="chunk-a1", ordinal=1, token_count=20,
        content="退货期限为收货后七天内", heading_path="/退货政策/时限",
    )
    store.active["chunk-a2"] = _chunk_ref(
        chunk_id="chunk-a2", ordinal=2, token_count=20,
        content="需保留吊牌和购买凭证", heading_path="/退货政策/凭证",
    )
    store.active["chunk-a5"] = _chunk_ref(
        chunk_id="chunk-a5", ordinal=5, token_count=50,
        content="跨文档的独立高分片段", heading_path="/其他",
        document_id="doc-b",
    )
    embedding = _Embedding()
    vector = _Vector()
    service = BaselineKnowledgeSearchService(
        store=store,
        embedding=embedding,
        vector_store=vector,
    )
    return SimpleNamespace(
        service=service,
        store=store,
        embedding=embedding,
        vector=vector,
    )


def _candidate(chunk_id, *, score, version_id="version-a", ordinal=0,
               document_id="doc-a"):
    from app.knowledge.vector_store import VectorCandidate

    return VectorCandidate(
        chunk_id=chunk_id,
        document_id=document_id,
        version_id=version_id,
        ordinal=ordinal,
        score=score,
    )


def test_hybrid_retriever_returns_ranked_active_chunks_without_event(
    retrieval_scope,
):
    retrieval_scope.vector.candidates = [
        _candidate("chunk-a2", score=0.91, ordinal=2),
        _candidate("chunk-old", score=0.90, version_id="old"),
        _candidate("chunk-a0", score=0.80, ordinal=0),
    ]
    retriever = HybridRetriever(
        store=retrieval_scope.store,
        embedding=retrieval_scope.embedding,
        vector_store=retrieval_scope.vector,
    )
    timeout_values = iter([4, 3])

    result = retriever.retrieve(
        organization_id="org-a",
        query="return conditions",
        timeout_provider=lambda: next(timeout_values),
    )

    assert [item.chunk.chunk_id for item in result.ranked_chunks] == [
        "chunk-a2",
        "chunk-a0",
    ]
    assert [item.fused_score for item in result.ranked_chunks] == [0.91, 0.80]
    assert result.query_tokens == 3
    assert retrieval_scope.embedding.query_timeouts == [4]
    assert retrieval_scope.vector.search_calls[0]["timeout_seconds"] == 3
    assert retrieval_scope.store.events == []


def test_baseline_using_shared_retriever_still_records_exactly_one_event(
    retrieval_scope,
):
    retrieval_scope.vector.candidates = [
        _candidate("chunk-a0", score=0.9, ordinal=0)
    ]

    result = retrieval_scope.service.search(
        organization_id="org-a", question="returns"
    )

    assert [item.chunk_id for item in result.citations] == ["chunk-a0"]
    assert len(retrieval_scope.store.events) == 1
    assert retrieval_scope.store.events[0].strategy == "baseline"
    assert retrieval_scope.embedding.query_timeouts == [5]
    assert retrieval_scope.vector.search_calls[0]["timeout_seconds"] == 5


# ------------------------------------------------------------------ Step 1


def test_baseline_uses_one_query_and_fixed_top_k(retrieval_scope):
    retrieval_scope.vector.candidates = [
        _candidate("chunk-a0", score=0.9, ordinal=0),
        _candidate("chunk-a1", score=0.8, ordinal=1),
        _candidate("chunk-a2", score=0.7, ordinal=2),
        _candidate("chunk-a5", score=0.6, ordinal=5, document_id="doc-b"),
    ]
    result = retrieval_scope.service.search(
        organization_id="org-a",
        question="退货期限是多久？",
    )

    assert retrieval_scope.embedding.query_calls == ["退货期限是多久？"]
    (call,) = retrieval_scope.vector.search_calls
    assert call["organization_id"] == "org-a"
    assert call["active_version_ids"] == ["version-a"]
    assert call["prefetch_limit"] == 8
    assert call["result_limit"] == 8
    # The embed_query response is passed straight through as the query vector.
    assert call["query_embedding"].token_count == 3
    assert len(result.citations) <= 5
    assert sum(item.token_count for item in result.selected_chunks) <= 3000
    assert [citation.citation_id for citation in result.citations] == [
        f"C{i}" for i in range(1, len(result.citations) + 1)
    ]
    assert result.retrieval_summary.strategy == "baseline"
    assert result.retrieval_summary.round_count == 1
    assert result.retrieval_summary.evidence_status == "sufficient"


# 保护行为：Baseline 必须按 rerank 分数而非融合召回分数生成最终引用顺序。
def test_baseline_uses_rerank_order_before_budget_selection(retrieval_scope):
    from app.knowledge.reranking import RerankedChunk

    class _ReverseReranker:
        def __init__(self):
            self.calls = []

        def rerank(self, *, query, chunks, timeout_seconds):
            self.calls.append((query, chunks, timeout_seconds))
            return (
                RerankedChunk(chunk=chunks[1], rerank_score=0.95),
                RerankedChunk(chunk=chunks[0], rerank_score=0.10),
            )

    reranker = _ReverseReranker()
    retrieval_scope.service = BaselineKnowledgeSearchService(
        store=retrieval_scope.store,
        embedding=retrieval_scope.embedding,
        vector_store=retrieval_scope.vector,
        reranker=reranker,
    )
    retrieval_scope.vector.candidates = [
        _candidate("chunk-a0", score=0.99, ordinal=0),
        _candidate("chunk-a2", score=0.40, ordinal=2),
    ]

    result = retrieval_scope.service.search(
        organization_id="org-a", question="退货期限"
    )

    assert [item.chunk_id for item in result.citations] == [
        "chunk-a2",
        "chunk-a0",
    ]
    assert reranker.calls[0][0] == "退货期限"
    assert reranker.calls[0][2] == 5


# 边界情况：rerank 基础设施失败必须明确失败，不能静默退回融合分数顺序。
def test_baseline_rerank_failure_is_not_converted_to_unreranked_result(
    retrieval_scope,
):
    class _FailingReranker:
        def rerank(self, *, query, chunks, timeout_seconds):
            del query, chunks, timeout_seconds
            raise RerankUnavailableError(reason="provider down")

    retrieval_scope.service = BaselineKnowledgeSearchService(
        store=retrieval_scope.store,
        embedding=retrieval_scope.embedding,
        vector_store=retrieval_scope.vector,
        reranker=_FailingReranker(),
    )
    retrieval_scope.vector.candidates = [
        _candidate("chunk-a0", score=0.9, ordinal=0)
    ]

    result = retrieval_scope.service.search(
        organization_id="org-a", question="退货期限"
    )

    assert result.ok is False
    assert result.citations == []
    assert result.error.code == "RERANK_UNAVAILABLE"
    assert result.retrieval_summary.evidence_status == "failed"
    assert retrieval_scope.store.events[-1].outcome == "failed"


# ------------------------------------------------------------------ Step 2


def test_sqlite_second_pass_filters_old_version_and_cross_org(
    retrieval_scope,
):
    """Old-version and foreign-org candidates are invisible to the fake store's
    ``resolve_active_citations``, so the final citations exclude them; only the
    genuinely active same-org current-version chunks survive, in RRF (score)
    order."""
    # Candidates: active chunks, an old-version chunk the store does not know,
    # and a foreign-org chunk the store does not have. a5 (doc-b) is a
    # legitimately active chunk of ANOTHER document -- it should survive.
    retrieval_scope.vector.candidates = [
        _candidate("chunk-a2", score=0.95, ordinal=2),  # active (ordinal 2)
        _candidate("chunk-old", score=0.9, ordinal=0,  # stale version id
                   version_id="version-old"),
        _candidate("chunk-a0", score=0.88, ordinal=0),   # active (ordinal 0)
        _candidate("foreign-1", score=0.8, ordinal=0,  # cross-org, not in store
                   document_id="foreign-doc"),
        _candidate("chunk-a5", score=0.7, ordinal=5,   # active, other doc
                   document_id="doc-b"),
    ]

    result = retrieval_scope.service.search(
        organization_id="org-a",
        question="退货政策",
    )

    ids = [c.chunk_id for c in result.citations]
    assert "chunk-old" not in ids
    assert "foreign-1" not in ids
    # Order reflects RRF score descending: a2 (0.95), a0 (0.88), a5 (0.7).
    assert ids == ["chunk-a2", "chunk-a0", "chunk-a5"]
    # Citations carry the document title from the store, not from Qdrant.
    assert [c.title for c in result.citations] == [
        "退货政策", "退货政策", "退货政策"
    ]


def test_adjacent_ordinals_with_distinct_headings_are_all_kept(
    retrieval_scope,
):
    """Ordinal adjacency is document order, not evidence duplication."""
    retrieval_scope.vector.candidates = [
        _candidate("chunk-a0", score=0.9, ordinal=0),
        _candidate("chunk-a1", score=0.8, ordinal=1),
        _candidate("chunk-a2", score=0.7, ordinal=2),
        _candidate("chunk-a5", score=0.6, ordinal=5, document_id="doc-b"),
    ]

    result = retrieval_scope.service.search(
        organization_id="org-a",
        question="退货",
    )

    assert [c.chunk_id for c in result.citations] == [
        "chunk-a0",
        "chunk-a1",
        "chunk-a2",
        "chunk-a5",
    ]


def test_adjacent_chunks_from_different_documents_are_both_kept(
    retrieval_scope,
):
    """Two chunks from DIFFERENT documents that share a version_id and differ by
    one ordinal must BOTH be kept. Document-scoping prevents cross-document
    collisions: without it, the shared version + adjacent ordinals would make a
    valid hit look like a contiguous same-document fragment and drop it."""
    # doc-b chunk-b1 (ordinal 1) and doc-a chunk-a2 (ordinal 2) share version-a
    # and differ by exactly one ordinal; their real documents differ, so the
    # dedupe must NOT collapse them.
    retrieval_scope.store.active["chunk-b1"] = _chunk_ref(
        chunk_id="chunk-b1",
        document_id="doc-b",
        ordinal=1,
        token_count=10,
        content="另一个独立文档的高分片段",
    )
    retrieval_scope.vector.candidates = [
        _candidate("chunk-b1", score=0.9, ordinal=1, document_id="doc-b"),
        _candidate("chunk-a2", score=0.8, ordinal=2),
    ]
    # Both live in the same active version and their ordinals are adjacent
    # (|1-2| == 1), yet they come from different documents -- the original
    # (document-unaware) dedupe would have dropped chunk-a2 as "adjacent" to
    # chunk-b1. Both must appear.
    result = retrieval_scope.service.search(
        organization_id="org-a",
        question="退货",
    )

    ids = [c.chunk_id for c in result.citations]
    assert ids == ["chunk-b1", "chunk-a2"]


def test_rank_unique_collapses_duplicate_resolved_chunk_ids():
    """A duplicated SQLite resolution row cannot duplicate final evidence."""
    ref = _chunk_ref(chunk_id="chunk-a0", ordinal=0)
    candidate = _candidate("chunk-a0", score=0.9, ordinal=0)

    ranked = BaselineKnowledgeSearchService._rank_unique(
        [ref, ref],
        {candidate.chunk_id: candidate},
    )

    assert ranked == [ref]


def test_rebuilds_order_from_candidate_scores_not_db_default_order(
    retrieval_scope,
):
    """The fake store returns resolved chunks in its natural (insertion) order,
    which differs from the candidate score order. The service must rebuild the
    final order from candidate scores via candidate_by_id, so citations follow
    RRF ordering and never trust the DB default order."""
    # Distinct ordinals (0, 5, 10) so no adjacency-dedupe interferes with the
    # order assertion. Store insertion order is b0, b1, b2; candidate scores
    # order them b2 > b0 > b1.
    retrieval_scope.store.active.clear()
    retrieval_scope.store.active["b0"] = _chunk_ref(
        chunk_id="b0", ordinal=0, token_count=10, content="fragment zero",
    )
    retrieval_scope.store.active["b1"] = _chunk_ref(
        chunk_id="b1", ordinal=5, token_count=10, content="fragment one",
    )
    retrieval_scope.store.active["b2"] = _chunk_ref(
        chunk_id="b2", ordinal=10, token_count=10, content="fragment two",
    )
    retrieval_scope.vector.candidates = [
        _candidate("b2", score=0.9, ordinal=10),
        _candidate("b1", score=0.6, ordinal=5),
        _candidate("b0", score=0.8, ordinal=0),
    ]

    result = retrieval_scope.service.search(
        organization_id="org-a",
        question="退货",
    )

    # Score order (0.9, 0.8, 0.6) must win over insert order (b0, b1, b2).
    assert [c.chunk_id for c in result.citations] == ["b2", "b0", "b1"]


# ------------------------------------------------------------------ Step 3


def test_no_hits_are_insufficient_not_infrastructure_failure(retrieval_scope):
    retrieval_scope.vector.candidates = []
    result = retrieval_scope.service.search(
        organization_id="org-a",
        question="没有答案的问题",
    )
    assert result.ok is True
    assert result.citations == []
    assert result.selected_chunks == []
    assert result.retrieval_summary.evidence_status == "insufficient"
    assert retrieval_scope.store.events[-1].outcome == "insufficient"


def test_empty_active_versions_skip_embedding_and_vector(retrieval_scope):
    """No active versions -> insufficient WITHOUT calling embedding or Qdrant."""
    retrieval_scope.store._active_version_ids = {"org-a": []}
    result = retrieval_scope.service.search(
        organization_id="org-a",
        question="没有激活版本",
    )
    assert result.ok is True
    assert result.retrieval_summary.evidence_status == "insufficient"
    assert retrieval_scope.embedding.query_calls == []
    assert retrieval_scope.vector.search_calls == []
    assert retrieval_scope.store.events[-1].outcome == "insufficient"


def test_qdrant_failure_is_not_converted_to_empty_result(retrieval_scope):
    retrieval_scope.vector.error = VectorStoreUnavailableError(reason="down")
    result = retrieval_scope.service.search(
        organization_id="org-a",
        question="退货期限",
    )
    assert result.ok is False
    assert result.error.code == "VECTOR_STORE_UNAVAILABLE"
    assert result.retrieval_summary.evidence_status == "failed"
    assert retrieval_scope.store.events[-1].outcome == "failed"


def test_embedding_failure_maps_to_embedding_unavailable(retrieval_scope):
    class _BoomEmbedding(_Embedding):
        def embed_query(self, text, *, timeout_seconds=5):
            super().embed_query(text, timeout_seconds=timeout_seconds)
            raise EmbeddingUnavailableError(reason="provider down")

    retrieval_scope.service = BaselineKnowledgeSearchService(
        store=retrieval_scope.store,
        embedding=_BoomEmbedding(),
        vector_store=retrieval_scope.vector,
    )
    result = retrieval_scope.service.search(
        organization_id="org-a",
        question="退货",
    )
    assert result.ok is False
    assert result.error.code == "EMBEDDING_UNAVAILABLE"
    assert result.retrieval_summary.evidence_status == "failed"
    assert retrieval_scope.store.events[-1].outcome == "failed"


def test_event_records_only_ids_and_scores_not_content(retrieval_scope):
    # chunk-a0 and chunk-a2 are NOT ordinal-adjacent, so both survive dedupe.
    retrieval_scope.vector.candidates = [
        _candidate("chunk-a0", score=0.9, ordinal=0),
        _candidate("chunk-a2", score=0.8, ordinal=2),
    ]
    retrieval_scope.service.search(organization_id="org-a", question="退货")

    event = retrieval_scope.store.events[-1]
    assert event.strategy == "baseline"
    assert event.round_count == 1
    assert event.model_calls == 0
    # planned_queries_json records the fixed baseline plan: the original query.
    assert json.loads(event.planned_queries_json) == ["退货"]
    trace = json.loads(event.candidate_json)
    assert trace == {
        "schema_version": 2,
        "candidates": [
            {
                "chunk_id": "chunk-a0",
                "fused_rank": 1,
                "fused_score": 0.9,
                "resolution_status": "selected",
                "selection_reason": "selected",
            },
            {
                "chunk_id": "chunk-a2",
                "fused_rank": 2,
                "fused_score": 0.8,
                "resolution_status": "selected",
                "selection_reason": "selected",
            },
        ],
    }
    # selected_chunk_ids_json holds only ids (final order), no body text.
    assert json.loads(event.selected_chunk_ids_json) == ["chunk-a0", "chunk-a2"]
    serialized = json.dumps(trace) + json.dumps(event.selected_chunk_ids_json)
    assert "七天无理由退货" not in serialized
    assert "退货政策" not in serialized

    # estimated_tokens = query-embedding token_count + selected chunk tokens.
    assert event.estimated_tokens == 3 + 20 + 20
    assert event.original_query == "退货"


def test_trace_explains_budget_filter_top_k_and_duplicate_decisions(
    retrieval_scope,
):
    for chunk_id, ordinal, tokens in [
        ("oversized", 10, 3001),
        ("extra", 11, 10),
        ("overflow", 12, 10),
    ]:
        retrieval_scope.store.active[chunk_id] = _chunk_ref(
            chunk_id=chunk_id,
            ordinal=ordinal,
            token_count=tokens,
            heading_path=f"/诊断/{chunk_id}",
        )
    retrieval_scope.vector.candidates = [
        _candidate("chunk-a0", score=0.99, ordinal=0),
        _candidate("oversized", score=0.95, ordinal=10),
        _candidate("chunk-a1", score=0.9, ordinal=1),
        _candidate("chunk-a2", score=0.8, ordinal=2),
        _candidate("chunk-a5", score=0.7, ordinal=5, document_id="doc-b"),
        _candidate("extra", score=0.6, ordinal=11),
        _candidate("overflow", score=0.5, ordinal=12),
        _candidate("filtered", score=0.4, ordinal=13),
        _candidate("chunk-a0", score=0.3, ordinal=0),
    ]

    result = retrieval_scope.service.search(
        organization_id="org-a",
        question="诊断候选选择",
    )

    assert [c.chunk_id for c in result.citations] == [
        "chunk-a0", "chunk-a1", "chunk-a2", "chunk-a5", "extra"
    ]
    decisions = [
        (
            item.chunk_id,
            item.resolution_status,
            item.selection_reason,
        )
        for item in result.retrieval_trace.candidates
    ]
    assert decisions == [
        ("chunk-a0", "selected", "selected"),
        ("oversized", "active", "token_budget_exceeded"),
        ("chunk-a1", "selected", "selected"),
        ("chunk-a2", "selected", "selected"),
        ("chunk-a5", "selected", "selected"),
        ("extra", "selected", "selected"),
        ("overflow", "active", "top_k_exceeded"),
        (
            "filtered",
            "filtered_inactive_or_invalid",
            "filtered_inactive_or_invalid",
        ),
        ("chunk-a0", "active", "duplicate_chunk_id"),
    ]
    serialized = json.dumps(result.retrieval_trace.to_dict(), ensure_ascii=False)
    assert "policy content" not in serialized
    assert "诊断候选选择" not in serialized
