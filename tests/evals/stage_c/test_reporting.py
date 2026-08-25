"""Behavior tests for Stage C result normalization and aggregate reporting."""

from __future__ import annotations

import json

import pytest

from app.evals.stage_c.checkpoint import CheckpointResult, CheckpointState
from app.evals.stage_c.models import (
    EvidenceAlternative,
    EvidenceGroup,
    ExpectedBehavior,
    KeyAnswerFact,
    RetrievalStrategy,
    SecurityExpectations,
    StageCCase,
    StageCCategory,
    StageCScenario,
    StageCVariant,
    StrategyExpectation,
    TenantKey,
)
from app.evals.stage_c.reporting import compile_stage_c_report, normalize_result
from app.evals.stage_c.scoring import ChunkIdentity, IdentityIndex
from app.knowledge.base import (
    DocumentStatus,
    InsufficientEvidenceError,
    RetrievalEvent,
    SearchBudgetExceededError,
    VectorStoreUnavailableError,
)
from app.knowledge.results import (
    AdaptiveSearchResult,
    BaselineSearchResult,
    Citation,
    RetrievalSummary,
    RetrievalTrace,
)


HEADING = "退货与换货政策/1.1 退货时限"


def make_case(
    case_id: str = "report-case",
    *,
    category: StageCCategory = StageCCategory.SIMPLE_POLICY,
    strategy_expectation: StrategyExpectation | None = None,
    security_expectations: SecurityExpectations | None = None,
) -> StageCCase:
    return StageCCase(
        case_id=case_id,
        category=category,
        scenario=StageCScenario.MAIN_ACTIVE,
        tenant_key=TenantKey.ORG_A,
        question="退货期限是多少？",
        reference_answer="7 天",
        key_answer_facts=(KeyAnswerFact(fact_id="window", statement="7 天"),),
        required_evidence_groups=(
            EvidenceGroup(
                group_id="window",
                supports_fact_ids=("window",),
                any_of=(
                    EvidenceAlternative(
                        document_key="returns_exchange", heading_path=HEADING
                    ),
                ),
            ),
        ),
        should_have_answer=True,
        expected_behavior=ExpectedBehavior.ANSWER_GROUNDED,
        strategy_expectation=strategy_expectation,
        business_context=None,
        forbidden_tenant_keys=(TenantKey.ORG_B,),
        security_expectations=security_expectations,
        notes="",
    )


@pytest.fixture
def identity_index() -> IdentityIndex:
    return IdentityIndex(
        tuple(
            ChunkIdentity(
                chunk_id=f"chunk-{index}",
                tenant_key=TenantKey.ORG_A,
                document_key="returns_exchange" if index == 1 else "irrelevant",
                document_id=f"document-{index}",
                version_id=f"version-{index}",
                heading_path=HEADING if index == 1 else f"无关/{index}",
                document_status=DocumentStatus.ACTIVE,
                active_version_id=f"version-{index}",
            )
            for index in range(1, 7)
        )
    )


def citations(count: int) -> tuple[Citation, ...]:
    return tuple(
        Citation(
            citation_id=f"C{index}",
            document_id=f"document-{index}",
            version_id=f"version-{index}",
            chunk_id=f"chunk-{index}",
            title=f"sensitive title {index}",
            heading_path=HEADING if index == 1 else f"无关/{index}",
            content=f"sensitive body {index}",
        )
        for index in range(1, count + 1)
    )


def adaptive_result(
    *,
    strategy: str = "multi",
    returned: tuple[Citation, ...] = (),
    error=None,
) -> AdaptiveSearchResult:
    return AdaptiveSearchResult(
        ok=error is None,
        citations=returned,
        retrieval_summary=RetrievalSummary(
            strategy=strategy,
            round_count=99,
            evidence_status="sufficient" if error is None else "failed",
            latency_ms=99999,
        ),
        error=error,
        selected_chunks=(),
        retrieval_trace={"schema_version": 4, "queries": []},
    )


def baseline_result(*, returned: tuple[Citation, ...] = (), error=None):
    return BaselineSearchResult(
        ok=error is None,
        citations=returned,
        retrieval_summary=RetrievalSummary(
            strategy="baseline",
            round_count=99,
            evidence_status="sufficient" if returned else "insufficient",
            latency_ms=99999,
        ),
        error=error,
        selected_chunks=(),
        retrieval_trace=RetrievalTrace.empty(),
    )


def event(
    *,
    adaptive: bool,
    rounds: int,
    model_calls: int,
    tokens: int,
    query_rounds: tuple[int, ...],
    candidates: list[dict[str, object]] | None = None,
) -> RetrievalEvent:
    if adaptive:
        queries = [
            {
                "round": round_number,
                "query_index": query_index,
                "query_digest": f"sha256:{round_number}-{query_index}",
            }
            for round_number, count in enumerate(query_rounds, start=1)
            for query_index in range(1, count + 1)
        ]
        trace = {
            "schema_version": 4,
            "queries": queries,
            "candidates": candidates or [],
        }
        strategy = "multi"
    else:
        trace = {"schema_version": 2, "candidates": candidates or []}
        strategy = "baseline"
    return RetrievalEvent(
        event_id="event-1",
        organization_id="trusted-org-a",
        conversation_id="stage-c:case:variant:attempt-1",
        strategy=strategy,
        original_query="must never be serialized",
        planned_queries_json='["must never be serialized"]',
        round_count=rounds,
        candidate_json=json.dumps(trace),
        selected_chunk_ids_json="[]",
        outcome="sufficient",
        latency_ms=123,
        model_calls=model_calls,
        estimated_tokens=tokens,
        created_at="2026-08-21T00:00:00Z",
    )


def test_adaptive_normalization_uses_persisted_event_counts_and_top_five(
    identity_index: IdentityIndex,
) -> None:
    """Using the result summary or sixth citation would break fair comparison."""
    case = make_case(
        strategy_expectation=StrategyExpectation(
            preferred=RetrievalStrategy.MULTI,
            allowed=(RetrievalStrategy.SINGLE, RetrievalStrategy.MULTI),
        )
    )

    normalized = normalize_result(
        case=case,
        variant=StageCVariant.ADAPTIVE,
        result=adaptive_result(returned=citations(6)),
        event=event(
            adaptive=True,
            rounds=2,
            model_calls=3,
            tokens=321,
            query_rounds=(3, 2),
        ),
        identity_index=identity_index,
        attempt=1,
    )

    assert normalized.round_count == 2
    assert normalized.query_count_by_round == (3, 2)
    assert normalized.model_calls == 3
    assert normalized.tokens == 321
    assert normalized.latency_ms == 123
    assert normalized.evaluated_citation_count == 5
    assert normalized.full_citation_count == 6
    assert normalized.metrics is not None
    assert normalized.metrics.returned_full_count == 6
    assert normalized.strategy_allowed is True
    assert normalized.strategy_preferred is True
    payload = normalized.to_payload()
    assert "title" not in json.dumps(payload)
    assert "content" not in json.dumps(payload)
    assert "must never be serialized" not in json.dumps(payload)


def test_none_strategy_is_preserved_and_scored_against_expectations(
    identity_index: IdentityIndex,
) -> None:
    """Dropping a legal NONE strategy would remove a completed case from strategy metrics."""
    case = make_case(
        strategy_expectation=StrategyExpectation(
            preferred=RetrievalStrategy.SINGLE,
            allowed=(RetrievalStrategy.SINGLE, RetrievalStrategy.MULTI),
        )
    )
    persisted_event = event(
        adaptive=True,
        rounds=0,
        model_calls=1,
        tokens=12,
        query_rounds=(),
    )
    object.__setattr__(persisted_event, "strategy", "none")

    normalized = normalize_result(
        case=case,
        variant=StageCVariant.ADAPTIVE,
        result=adaptive_result(strategy="none"),
        event=persisted_event,
        identity_index=identity_index,
        attempt=1,
    )

    assert normalized.status == "completed"
    assert normalized.strategy == "none"
    assert normalized.strategy_allowed is False
    assert normalized.strategy_preferred is False
    assert checkpoint_result(normalized).payload["strategy"] == "none"

    no_expectation = normalize_result(
        case=make_case("none-without-expectation"),
        variant=StageCVariant.ADAPTIVE,
        result=adaptive_result(strategy="none"),
        event=persisted_event,
        identity_index=identity_index,
        attempt=1,
    )
    assert no_expectation.strategy == "none"
    assert no_expectation.strategy_allowed is None
    assert no_expectation.strategy_preferred is None


def test_unknown_adaptive_strategy_is_infrastructure_failed_and_unmeasured(
    identity_index: IdentityIndex,
) -> None:
    """An invented persisted strategy must not enter completed denominators."""
    case = make_case(
        strategy_expectation=StrategyExpectation(
            preferred=RetrievalStrategy.SINGLE,
            allowed=(RetrievalStrategy.SINGLE, RetrievalStrategy.MULTI),
        )
    )
    persisted_event = event(
        adaptive=True,
        rounds=1,
        model_calls=1,
        tokens=12,
        query_rounds=(1,),
    )
    object.__setattr__(persisted_event, "strategy", "invented")

    normalized = normalize_result(
        case=case,
        variant=StageCVariant.ADAPTIVE,
        result=adaptive_result(returned=citations(1)),
        event=persisted_event,
        identity_index=identity_index,
        attempt=1,
    )

    assert normalized.status == "infrastructure_failed"
    assert normalized.error_code == "unexpected_error"
    assert normalized.metrics is None
    assert normalized.strategy is None
    report = compile_stage_c_report(
        (case,), checkpoint_state((checkpoint_result(normalized),))
    )
    adaptive = report["aggregates"]["overall"]["adaptive"]
    assert adaptive["strategy_allowed"]["denominator"] == 0
    assert adaptive["strategy_preferred"]["denominator"] == 0


@pytest.mark.parametrize(
    "query_indexes_by_round",
    [
        ((1,), ()),
        ((1, 3),),
    ],
)
def test_adaptive_trace_rejects_empty_rounds_and_non_contiguous_query_indexes(
    identity_index: IdentityIndex,
    query_indexes_by_round: tuple[tuple[int, ...], ...],
) -> None:
    """A declared executed round must contain exactly query indexes 1 through count."""
    persisted_event = event(
        adaptive=True,
        rounds=len(query_indexes_by_round),
        model_calls=1,
        tokens=12,
        query_rounds=tuple(len(indexes) for indexes in query_indexes_by_round),
    )
    trace = json.loads(persisted_event.candidate_json)
    trace["queries"] = [
        {
            "round": round_number,
            "query_index": query_index,
            "query_digest": f"sha256:{round_number}-{query_index}",
        }
        for round_number, indexes in enumerate(query_indexes_by_round, start=1)
        for query_index in indexes
    ]
    object.__setattr__(persisted_event, "candidate_json", json.dumps(trace))

    normalized = normalize_result(
        case=make_case(),
        variant=StageCVariant.ADAPTIVE,
        result=adaptive_result(returned=citations(1)),
        event=persisted_event,
        identity_index=identity_index,
        attempt=1,
    )

    assert normalized.status == "infrastructure_failed"
    assert normalized.metrics is None


def test_vector_failure_and_damaged_or_missing_event_are_infrastructure_failed(
    identity_index: IdentityIndex,
) -> None:
    """Provider failure or missing audit evidence must never score as an empty hit."""
    case = make_case()
    failed = normalize_result(
        case=case,
        variant=StageCVariant.BASELINE,
        result=baseline_result(error=VectorStoreUnavailableError(reason="503 body")),
        event=event(
            adaptive=False,
            rounds=1,
            model_calls=0,
            tokens=0,
            query_rounds=(1,),
        ),
        identity_index=identity_index,
        attempt=1,
    )
    missing = normalize_result(
        case=case,
        variant=StageCVariant.BASELINE,
        result=baseline_result(returned=citations(1)),
        event=None,
        identity_index=identity_index,
        attempt=1,
    )
    damaged_event = event(
        adaptive=True,
        rounds=1,
        model_calls=1,
        tokens=1,
        query_rounds=(1,),
    )
    object.__setattr__(damaged_event, "candidate_json", "not-json")
    damaged = normalize_result(
        case=case,
        variant=StageCVariant.ADAPTIVE,
        result=adaptive_result(returned=citations(1)),
        event=damaged_event,
        identity_index=identity_index,
        attempt=1,
    )

    assert failed.status == "infrastructure_failed"
    assert failed.error_code == "provider_unavailable"
    assert failed.metrics is None
    assert failed.round_count == 1
    assert failed.query_count_by_round == (1,)
    assert failed.model_calls == 0
    assert missing.status == "infrastructure_failed"
    assert missing.metrics is None
    assert damaged.status == "infrastructure_failed"
    assert damaged.metrics is None


def test_insufficient_evidence_is_a_completed_scored_outcome(
    identity_index: IdentityIndex,
) -> None:
    """A healthy abstention is a retrieval outcome, not infrastructure loss."""
    normalized = normalize_result(
        case=make_case(),
        variant=StageCVariant.ADAPTIVE,
        result=adaptive_result(
            error=InsufficientEvidenceError(missing_aspects=("window",))
        ),
        event=event(
            adaptive=True,
            rounds=1,
            model_calls=2,
            tokens=50,
            query_rounds=(1,),
        ),
        identity_index=identity_index,
        attempt=1,
    )

    assert normalized.status == "completed"
    assert normalized.error_code is None
    assert normalized.evidence_status == "missing"
    assert normalized.metrics is not None
    assert normalized.metrics.evidence_group_recall == 0.0


def test_budget_exceeded_is_completed_and_evaluates_case_limits(
    identity_index: IdentityIndex,
) -> None:
    """Bounded termination remains measurable and exposes the violated guardrail."""
    case = make_case(
        security_expectations=SecurityExpectations(
            max_search_rounds=1,
            max_first_round_queries=2,
            max_second_round_queries=1,
        )
    )
    normalized = normalize_result(
        case=case,
        variant=StageCVariant.ADAPTIVE,
        result=adaptive_result(error=SearchBudgetExceededError()),
        event=event(
            adaptive=True,
            rounds=2,
            model_calls=3,
            tokens=80,
            query_rounds=(3, 1),
        ),
        identity_index=identity_index,
        attempt=2,
    )

    assert normalized.status == "completed"
    assert normalized.error_code == "budget_exhausted"
    assert normalized.metrics is not None
    assert normalized.safety_flags["budget_exceeded"] is True
    assert normalized.safety_flags["round_limit_exceeded"] is True
    assert normalized.safety_flags["query_limit_exceeded"] is True
    assert normalized.safety_flags["model_call_limit_exceeded"] is False
    report = compile_stage_c_report(
        (case,), checkpoint_state((checkpoint_result(normalized),))
    )
    assert report["aggregates"]["budget"] == {
        "budget_exceeded_count": 1,
        "hard_limit_violation_count": 0,
        "case_security_expectation_violation_count": 1,
    }


def test_normalization_keeps_unknown_trace_candidates_and_scores_trusted_leaks(
    identity_index: IdentityIndex,
) -> None:
    """Dropping unknown candidate IDs or trusting forged citations hides safety faults."""
    cross_tenant = ChunkIdentity(
        chunk_id="cross-tenant",
        tenant_key=TenantKey.ORG_B,
        document_key="returns_exchange",
        document_id="trusted-cross-document",
        version_id="cross-version",
        heading_path=HEADING,
        document_status=DocumentStatus.ACTIVE,
        active_version_id="cross-version",
    )
    disabled = ChunkIdentity(
        chunk_id="disabled",
        tenant_key=TenantKey.ORG_A,
        document_key="returns_exchange",
        document_id="disabled-document",
        version_id="disabled-version",
        heading_path=HEADING,
        document_status=DocumentStatus.DISABLED,
        active_version_id="disabled-version",
    )
    inactive = ChunkIdentity(
        chunk_id="inactive",
        tenant_key=TenantKey.ORG_A,
        document_key="returns_exchange",
        document_id="inactive-document",
        version_id="old-version",
        heading_path=HEADING,
        document_status=DocumentStatus.ACTIVE,
        active_version_id="current-version",
    )
    index = IdentityIndex(
        identity_index.identities + (cross_tenant, disabled, inactive)
    )
    returned = (
        Citation(
            citation_id="C1",
            document_id="forged-document",
            version_id="cross-version",
            chunk_id="cross-tenant",
            title="secret title",
            heading_path=HEADING,
            content="secret body",
        ),
        Citation(
            citation_id="C2",
            document_id="disabled-document",
            version_id="disabled-version",
            chunk_id="disabled",
            title="secret title",
            heading_path=HEADING,
            content="secret body",
        ),
        Citation(
            citation_id="C3",
            document_id="inactive-document",
            version_id="old-version",
            chunk_id="inactive",
            title="secret title",
            heading_path=HEADING,
            content="secret body",
        ),
    )
    normalized = normalize_result(
        case=make_case(),
        variant=StageCVariant.ADAPTIVE,
        result=adaptive_result(returned=returned),
        event=event(
            adaptive=True,
            rounds=1,
            model_calls=2,
            tokens=20,
            query_rounds=(1,),
            candidates=[
                {
                    "round": 1,
                    "query_index": 1,
                    "chunk_id": "unknown-candidate",
                    "fused_rank": 1,
                    "fused_score": 0.9,
                }
            ],
        ),
        identity_index=index,
        attempt=1,
    )

    assert normalized.metrics is not None
    assert normalized.metrics.cross_tenant_leak is True
    assert normalized.metrics.disabled_document_leak is True
    assert normalized.metrics.inactive_version_leak is True
    assert normalized.candidate_trace[0]["chunk_id"] == "unknown-candidate"
    assert normalized.candidate_trace[0]["identity_known"] is False


def test_full_citation_and_candidate_unknown_counts_reach_report_aggregates(
    identity_index: IdentityIndex,
) -> None:
    """Citation and candidate unknowns remain distinct safety observations."""
    unknown_sixth = Citation(
        citation_id="C6",
        document_id="unknown-document",
        version_id="unknown-version",
        chunk_id="unknown-citation",
        title="must not persist",
        heading_path="unknown/heading",
        content="must not persist",
    )
    case = make_case()
    normalized = normalize_result(
        case=case,
        variant=StageCVariant.ADAPTIVE,
        result=adaptive_result(returned=(*citations(5), unknown_sixth)),
        event=event(
            adaptive=True,
            rounds=1,
            model_calls=2,
            tokens=20,
            query_rounds=(1,),
            candidates=[
                {
                    "round": 1,
                    "query_index": 1,
                    "chunk_id": "unknown-candidate",
                    "fused_rank": 1,
                    "fused_score": 0.9,
                }
            ],
        ),
        identity_index=identity_index,
        attempt=1,
    )

    assert normalized.metrics is not None
    assert normalized.metrics.evaluated_citation_count == 5
    assert normalized.metrics.relevant_top5_count == 1
    assert normalized.metrics.unknown_identity_count == 1
    assert normalized.safety_flags["unknown_identity_count"] == 2
    report = compile_stage_c_report(
        (case,), checkpoint_state((checkpoint_result(normalized),))
    )
    assert report["aggregates"]["overall"]["adaptive"]["unknown_identity_count"] == 2
    assert report["aggregates"]["security"]["unknown_identity_count"] == 2


def checkpoint_result(normalized) -> CheckpointResult:
    return CheckpointResult(
        case_id=normalized.case_id,
        variant=normalized.variant,
        status=normalized.status,
        attempt=normalized.attempt,
        payload=normalized.to_payload(),
    )


def checkpoint_state(results: tuple[CheckpointResult, ...]) -> CheckpointState:
    from tests.evals.stage_c.test_checkpoint import metadata

    run_metadata = metadata("report")
    return CheckpointState(
        run_id="run-report",
        fingerprint=run_metadata.fingerprint,
        metadata=run_metadata,
        results={item.key: item for item in results},
    )


def test_compile_report_uses_persisted_payloads_and_exact_category_denominators(
    identity_index: IdentityIndex,
) -> None:
    """Averages and failed-result denominators would distort category comparisons."""
    first = make_case("first", category=StageCCategory.SIMPLE_POLICY)
    second = make_case("second", category=StageCCategory.SIMPLE_POLICY)
    first_baseline = normalize_result(
        case=first,
        variant=StageCVariant.BASELINE,
        result=baseline_result(returned=citations(1)),
        event=event(
            adaptive=False,
            rounds=1,
            model_calls=0,
            tokens=10,
            query_rounds=(1,),
        ),
        identity_index=identity_index,
        attempt=1,
    )
    first_adaptive = normalize_result(
        case=first,
        variant=StageCVariant.ADAPTIVE,
        result=adaptive_result(returned=citations(2)),
        event=event(
            adaptive=True,
            rounds=1,
            model_calls=2,
            tokens=20,
            query_rounds=(1,),
        ),
        identity_index=identity_index,
        attempt=1,
    )
    second_baseline = normalize_result(
        case=second,
        variant=StageCVariant.BASELINE,
        result=baseline_result(error=VectorStoreUnavailableError(reason="down")),
        event=event(
            adaptive=False,
            rounds=1,
            model_calls=0,
            tokens=0,
            query_rounds=(1,),
        ),
        identity_index=identity_index,
        attempt=1,
    )
    report = compile_stage_c_report(
        (first, second),
        checkpoint_state(
            tuple(
                checkpoint_result(item)
                for item in (first_baseline, first_adaptive, second_baseline)
            )
        ),
    )

    simple = report["aggregates"]["by_category"]["simple_policy"]
    assert simple["baseline"]["evidence_group_recall"] == {
        "numerator": 1,
        "denominator": 1,
        "value": 1.0,
    }
    assert simple["adaptive"]["retrieval_precision"] == {
        "numerator": 1,
        "denominator": 2,
        "value": 0.5,
    }
    assert report["completion"] == {
        "case_count": 2,
        "expected_variants": 4,
        "completed_variants": 2,
        "infrastructure_failed_variants": 1,
        "missing_variants": 1,
        "completed_pairs": 1,
        "incomplete_pairs": [
            {
                "case_id": "second",
                "missing_variants": ["adaptive"],
                "infrastructure_failed_variants": ["baseline"],
            }
        ],
    }
    assert [item["case_id"] for item in report["cases"]] == ["first", "second"]
    assert list(report["cases"][0]["variants"]) == ["baseline", "adaptive"]


def test_compile_report_marks_unmeasurable_quality_and_strategy_scope() -> None:
    """Absent persisted measurements must be null with an explicit reason, not zero."""
    case = make_case(strategy_expectation=None)
    report = compile_stage_c_report((case,), checkpoint_state(()))
    baseline = report["aggregates"]["overall"]["baseline"]

    assert baseline["evidence_group_recall"] == {
        "numerator": 0,
        "denominator": 0,
        "value": None,
        "scope_reason": "no_completed_measurable_variants",
    }
    assert baseline["strategy_allowed"] == {
        "numerator": 0,
        "denominator": 0,
        "value": None,
        "scope_reason": "no_strategy_expectation",
    }
