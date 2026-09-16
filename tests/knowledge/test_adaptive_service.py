import pytest
import json
from types import SimpleNamespace

from app.knowledge import evidence as evidence_module
from app.knowledge import planning as planning_module
from app.knowledge.base import (
    ChunkWithDocumentTitle,
    EmbeddingUnavailableError,
    SearchBudgetExceededError,
    SearchInternalError,
    VectorStoreUnavailableError,
)
from app.knowledge.evidence import (
    AssessmentDecision,
    EvidenceAssessment,
    EvidenceStatus,
)
from app.knowledge.planning import (
    PlanDecision,
    SearchPlan,
    SearchReasonCode,
    SearchStrategy,
)
from app.knowledge.results import QueryRetrievalResult, ScoredChunk
from app.knowledge.service import (
    AdaptiveKnowledgeSearchService,
    SearchBudget,
    query_digest,
)


def test_budget_rejects_third_round_and_fourth_model_call():
    budget = SearchBudget.start(timeout_seconds=15, clock=lambda: 0.0)
    budget.consume_planner_call()
    budget.start_round(query_count=3)
    budget.consume_assessor_call()
    budget.start_round(query_count=2)
    budget.consume_assessor_call()

    with pytest.raises(SearchBudgetExceededError):
        budget.start_round(query_count=1)
    with pytest.raises(SearchBudgetExceededError):
        budget.consume_assessor_call()


def test_budget_rejects_query_caps_and_elapsed_deadline():
    budget = SearchBudget.start(timeout_seconds=15, clock=lambda: 0.0)
    with pytest.raises(SearchBudgetExceededError):
        budget.start_round(query_count=4)

    now = iter([0.0, 16.0])
    expired = SearchBudget.start(timeout_seconds=15, clock=lambda: next(now))
    with pytest.raises(SearchBudgetExceededError):
        expired.ensure_time()


def test_external_timeout_floors_remaining_time_and_never_returns_zero():
    now = iter([0.0, 11.2])
    budget = SearchBudget.start(timeout_seconds=15, clock=lambda: next(now))
    assert budget.external_timeout_seconds() == 3

    nearly_expired = iter([0.0, 14.2])
    budget = SearchBudget.start(
        timeout_seconds=15, clock=lambda: next(nearly_expired)
    )
    with pytest.raises(SearchBudgetExceededError):
        budget.external_timeout_seconds()


def _plan(strategy, queries, reason):
    return SearchPlan(
        strategy=strategy, queries=queries, reason_code=reason
    )


class _Planner:
    def __init__(self):
        self.next_plan = _plan(
            SearchStrategy.NONE, (), SearchReasonCode.BUSINESS_ONLY
        )
        self.calls = []
        self.error = None

    def plan(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return PlanDecision(self.next_plan, 1, 7, False)


class _Retriever:
    def __init__(self):
        self.calls = []
        self.results = {}
        self.error = None

    def retrieve(self, **kwargs):
        self.calls.append(SimpleNamespace(**kwargs))
        if self.error:
            raise self.error
        query = kwargs["query"]
        chunks = self.results.get(query, ())
        return QueryRetrievalResult(
            query=query,
            ranked_chunks=tuple(chunks),
            raw_candidates=(),
            query_tokens=2,
        )


class _Assessor:
    def __init__(self):
        self.calls = []
        self.decisions = []
        self.error = None

    def assess(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.decisions.pop(0)


class _Store:
    def __init__(self):
        self.events = []

    def record_retrieval_event(self, *, organization_id, event):
        self.events.append(event)
        return event


def _chunk(chunk_id, *, tokens=10):
    content = f"trusted content {chunk_id}"
    return ChunkWithDocumentTitle(
        chunk_id=chunk_id,
        organization_id="org-a",
        document_id="doc-1",
        version_id="version-1",
        ordinal=0,
        heading_path="/returns",
        content=content,
        token_count=tokens,
        document_title="Returns",
        # 区间与 content 自洽：内容取自 raw_text[0:len(content)]。
        start_offset=0,
        end_offset=len(content),
    )


def _scored(chunk_id, score, *, tokens=10):
    return ScoredChunk(_chunk(chunk_id, tokens=tokens), score)


def _assessment(status, *, followups=(), missing=()):
    return AssessmentDecision(
        EvidenceAssessment(
            status=status,
            covered_aspects=(1,) if status is EvidenceStatus.SUFFICIENT else (),
            missing_aspects=missing,
            follow_up_queries=followups,
        ),
        model_calls=1,
        estimated_tokens=5,
        degraded=False,
    )


@pytest.fixture
def adaptive_scope():
    store, planner, retriever, assessor = (
        _Store(), _Planner(), _Retriever(), _Assessor()
    )
    service = AdaptiveKnowledgeSearchService(
        store=store,
        retriever=retriever,
        planner=planner,
        assessor=assessor,
        timeout_seconds=15,
    )
    return SimpleNamespace(
        store=store,
        planner=planner,
        retriever=retriever,
        assessor=assessor,
        service=service,
    )


def test_none_records_zero_rounds_and_never_retrieves(adaptive_scope):
    result = adaptive_scope.service.search(
        organization_id="org-a", question="order ORD-1"
    )
    assert result.ok is True
    assert result.retrieval_summary.strategy == "none"
    assert result.retrieval_summary.round_count == 0
    assert result.public_dict()["data"]["result_code"] == "SEARCH_NOT_NEEDED"
    assert adaptive_scope.retriever.calls == []
    assert adaptive_scope.assessor.calls == []
    assert len(adaptive_scope.store.events) == 1


# 保护行为：查询侧 embedding 的上限是 QUERY_TIMEOUT_SECONDS（10 秒），且仍受本次检索
# 剩余预算约束。旧的 5 秒上限在 DashScope 抖动时会直接吃光预算、重试不足 1 秒，
# 把整条检索判死为 EMBEDDING_UNAVAILABLE（2026-09-16 由 5 秒调整为 10 秒）。
def test_query_embedding_timeout_caps_at_ten_seconds(adaptive_scope):
    adaptive_scope.planner.next_plan = _plan(
        SearchStrategy.SINGLE,
        ("return window",),
        SearchReasonCode.SIMPLE_POLICY,
    )
    adaptive_scope.retriever.results["return window"] = (
        _scored("c1", 0.9),
    )
    adaptive_scope.assessor.decisions = [
        _assessment(EvidenceStatus.SUFFICIENT)
    ]

    adaptive_scope.service.search(organization_id="org-a", question="returns")

    # 检索器拿到的是可调用的 provider（不是固定值），剩余预算充足时取上限 10 秒
    timeout_provider = adaptive_scope.retriever.calls[0].timeout_provider
    assert timeout_provider() == 10


def test_single_runs_one_query_and_returns_grounded_citation(adaptive_scope):
    adaptive_scope.planner.next_plan = _plan(
        SearchStrategy.SINGLE,
        ("return window",),
        SearchReasonCode.SIMPLE_POLICY,
    )
    adaptive_scope.retriever.results["return window"] = (
        _scored("c1", 0.9),
    )
    adaptive_scope.assessor.decisions = [
        _assessment(EvidenceStatus.SUFFICIENT)
    ]
    result = adaptive_scope.service.search(
        organization_id="org-a", question="how long"
    )
    assert [call.query for call in adaptive_scope.retriever.calls] == [
        "return window"
    ]
    assert [item.citation_id for item in result.citations] == ["C1"]
    # 保护行为：Agentic Search 的引用同样要带块在版本正文中的精确区间，
    # 否则 Baseline 能跳转而 Adaptive 不能跳转（两条链路行为不一致）。
    assert result.citations[0].start_offset == 0
    assert result.citations[0].end_offset == len("trusted content c1")
    assert result.retrieval_summary.round_count == 1
    assert result.retrieval_summary.strategy == "single"
    assert len(adaptive_scope.store.events) == 1


# 保护行为：Agentic Search 应按 rerank 分数选择证据，而不是沿用 embedding 融合分数顺序。
def test_single_uses_rerank_order_for_evidence_selection(adaptive_scope):
    from app.knowledge.reranking import RerankedChunk

    class _ReverseReranker:
        def rerank(self, *, query, chunks, timeout_seconds):
            del query, timeout_seconds
            return (
                RerankedChunk(chunk=chunks[1], rerank_score=0.95),
                RerankedChunk(chunk=chunks[0], rerank_score=0.10),
            )

    adaptive_scope.service._reranker = _ReverseReranker()
    adaptive_scope.planner.next_plan = _plan(
        SearchStrategy.SINGLE,
        ("return answer",),
        SearchReasonCode.SIMPLE_POLICY,
    )
    adaptive_scope.retriever.results["return answer"] = (
        _scored("embedding-first", 0.99),
        _scored("rerank-first", 0.40),
    )
    adaptive_scope.assessor.decisions = [
        _assessment(EvidenceStatus.SUFFICIENT)
    ]

    result = adaptive_scope.service.search(
        organization_id="org-a", question="how long"
    )

    assert [item.chunk_id for item in result.citations] == [
        "rerank-first",
        "embedding-first",
    ]
    assert [item["rerank_score"] for item in result.retrieval_trace["candidates"]] == [
        0.95,
        0.10,
    ]


def test_multi_runs_two_rounds_and_deduplicates_global_evidence(adaptive_scope):
    adaptive_scope.planner.next_plan = _plan(
        SearchStrategy.MULTI,
        ("packaging", "window", "exceptions"),
        SearchReasonCode.MULTI_CONDITION,
    )
    adaptive_scope.retriever.results.update(
        {
            "packaging": (_scored("c1", 0.8),),
            "window": (_scored("c2", 0.7),),
            "exceptions": (_scored("c1", 0.9),),
            "fees": (_scored("c3", 0.85),),
            "damaged": (_scored("c4", 0.75),),
        }
    )
    adaptive_scope.assessor.decisions = [
        _assessment(
            EvidenceStatus.INSUFFICIENT,
            followups=("fees", "damaged"),
            missing=("fees",),
        ),
        _assessment(EvidenceStatus.SUFFICIENT),
    ]
    result = adaptive_scope.service.search(
        organization_id="org-a", question="return requirements"
    )
    assert [call.query for call in adaptive_scope.retriever.calls] == [
        "packaging", "window", "exceptions", "fees", "damaged"
    ]
    assert result.retrieval_summary.round_count == 2
    assert [item.chunk_id for item in result.citations] == [
        "c1", "c3", "c4", "c2"
    ]
    assert len(adaptive_scope.store.events) == 1


def test_multi_without_new_followups_stops_insufficient_but_keeps_partial_citations(adaptive_scope):
    adaptive_scope.planner.next_plan = _plan(
        SearchStrategy.MULTI,
        ("packaging", "window"),
        SearchReasonCode.MULTI_CONDITION,
    )
    adaptive_scope.retriever.results["packaging"] = (_scored("c1", 0.8),)
    adaptive_scope.retriever.results["window"] = (_scored("c2", 0.7),)
    adaptive_scope.assessor.decisions = [
        _assessment(
            EvidenceStatus.INSUFFICIENT,
            followups=("PACKAGING",),
            missing=("exceptions",),
        )
    ]
    result = adaptive_scope.service.search(
        organization_id="org-a", question="return requirements"
    )
    assert result.ok is False
    assert result.error.code == "INSUFFICIENT_EVIDENCE"
    # 保护行为：Assessor 判定不足时不再丢弃已召回的 evidence，
    # 而是作为 partial citations 返回，避免正确候选在最终阶段被全部清空。
    assert [item.chunk_id for item in result.citations] == ["c1", "c2"]
    assert result.retrieval_summary.round_count == 1


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (VectorStoreUnavailableError(reason="down"), "VECTOR_STORE_UNAVAILABLE"),
        (EmbeddingUnavailableError(reason="down"), "EMBEDDING_UNAVAILABLE"),
        (SearchBudgetExceededError(), "SEARCH_BUDGET_EXCEEDED"),
    ],
)
def test_retrieval_failures_are_mapped_without_assessment(
    adaptive_scope, error, code
):
    adaptive_scope.planner.next_plan = _plan(
        SearchStrategy.SINGLE, ("returns",), SearchReasonCode.SIMPLE_POLICY
    )
    adaptive_scope.retriever.error = error
    result = adaptive_scope.service.search(
        organization_id="org-a", question="returns"
    )
    assert result.error.code == code
    assert adaptive_scope.assessor.calls == []
    assert len(adaptive_scope.store.events) == 1


def test_event_hashes_queries_and_contains_content_free_trace(adaptive_scope):
    adaptive_scope.planner.next_plan = _plan(
        SearchStrategy.SINGLE, ("returns",), SearchReasonCode.SIMPLE_POLICY
    )
    adaptive_scope.retriever.results["returns"] = (_scored("c1", 0.9),)
    adaptive_scope.assessor.decisions = [
        _assessment(EvidenceStatus.SUFFICIENT)
    ]
    adaptive_scope.service.search(
        organization_id="org-a",
        question="ORD-GOLDEN-001 13800000000 user@example.com",
    )
    event = adaptive_scope.store.events[0]
    combined = " ".join(
        value for value in vars(event).values() if isinstance(value, str)
    )
    assert event.original_query.startswith("sha256:")
    assert "ORD-GOLDEN-001" not in combined
    assert "13800000000" not in combined
    assert "user@example.com" not in combined
    assert "trusted content" not in combined
    assert '"schema_version": 4' in event.candidate_json
    assert event.model_calls == 2
    assert event.strategy == "single"


def test_trace_counts_every_query_even_when_no_candidates(adaptive_scope):
    adaptive_scope.planner.next_plan = _plan(
        SearchStrategy.MULTI,
        ("q1", "q2"),
        SearchReasonCode.MULTI_CONDITION,
    )
    adaptive_scope.assessor.decisions = [
        _assessment(
            EvidenceStatus.INSUFFICIENT,
            missing=("no_evidence",),
        )
    ]

    result = adaptive_scope.service.search(
        organization_id="org-a",
        question="complex private question",
    )

    assert result.retrieval_trace["queries"] == [
        {"round": 1, "query_index": 1, "query_digest": query_digest("q1")},
        {"round": 1, "query_index": 2, "query_digest": query_digest("q2")},
    ]
    assert result.retrieval_trace["planner_prompt_version"] == (
        planning_module.PLANNER_PROMPT_VERSION
    )
    assert result.retrieval_trace["assessor_prompt_version"] == (
        evidence_module.ASSESSOR_PROMPT_VERSION
    )
    serialized = json.dumps(result.retrieval_trace, ensure_ascii=False)
    for forbidden in (
        '"q1"',
        '"q2"',
        "complex private question",
        "org-a",
        "reasoning",
    ):
        assert forbidden not in serialized


def test_planner_and_assessor_provider_failures_are_internal(adaptive_scope):
    internal_reason = "ProviderBalanceError|status=402|code=insufficient_balance"
    adaptive_scope.planner.error = SearchInternalError(
        internal_reason=internal_reason
    )
    planner_result = adaptive_scope.service.search(
        organization_id="org-a", question="returns"
    )
    assert planner_result.error.code == "SEARCH_INTERNAL_ERROR"
    assert adaptive_scope.store.events[-1].strategy == "unplanned"
    assert json.loads(
        adaptive_scope.store.events[-1].candidate_json
    )["failure_stage"] == "planner"
    assert internal_reason not in json.dumps(planner_result.public_dict())
    assert internal_reason not in adaptive_scope.store.events[-1].candidate_json

    adaptive_scope.planner.error = None
    adaptive_scope.planner.next_plan = _plan(
        SearchStrategy.SINGLE, ("returns",), SearchReasonCode.SIMPLE_POLICY
    )
    adaptive_scope.retriever.results["returns"] = (_scored("c1", 0.9),)
    adaptive_scope.assessor.error = SearchInternalError()
    assessor_result = adaptive_scope.service.search(
        organization_id="org-a", question="returns"
    )
    assert assessor_result.error.code == "SEARCH_INTERNAL_ERROR"
    assert json.loads(
        adaptive_scope.store.events[-1].candidate_json
    )["failure_stage"] == "assessor"


def test_deadline_is_checked_after_external_call_before_next_stage():
    class Clock:
        value = 0.0

        def __call__(self):
            return self.value

    clock = Clock()
    store, planner, assessor = _Store(), _Planner(), _Assessor()
    planner.next_plan = _plan(
        SearchStrategy.SINGLE, ("returns",), SearchReasonCode.SIMPLE_POLICY
    )

    class ExpiringRetriever(_Retriever):
        def retrieve(self, **kwargs):
            result = super().retrieve(**kwargs)
            clock.value = 15.2
            return result

    retriever = ExpiringRetriever()
    retriever.results["returns"] = (_scored("c1", 0.9),)
    service = AdaptiveKnowledgeSearchService(
        store=store,
        retriever=retriever,
        planner=planner,
        assessor=assessor,
        timeout_seconds=15,
        clock=clock,
    )

    result = service.search(organization_id="org-a", question="returns")

    assert result.error.code == "SEARCH_BUDGET_EXCEEDED"
    assert assessor.calls == []
    assert json.loads(store.events[0].candidate_json)["failure_stage"] == "budget"


def test_expired_before_planner_does_not_count_or_start_model_call():
    class Clock:
        calls = 0

        def __call__(self):
            self.calls += 1
            return 0.0 if self.calls <= 2 else 14.5

    clock = Clock()
    store, planner = _Store(), _Planner()
    service = AdaptiveKnowledgeSearchService(
        store=store,
        retriever=_Retriever(),
        planner=planner,
        assessor=_Assessor(),
        timeout_seconds=15,
        clock=clock,
    )

    result = service.search(organization_id="org-a", question="returns")

    assert result.error.code == "SEARCH_BUDGET_EXCEEDED"
    assert planner.calls == []
    assert store.events[0].model_calls == 0
