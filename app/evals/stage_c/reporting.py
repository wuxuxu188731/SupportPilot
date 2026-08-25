"""Content-free normalization and exact aggregate reports for Stage C."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Literal, Mapping, Sequence

from app.evals.stage_c.checkpoint import CheckpointState
from app.evals.stage_c.models import (
    RetrievalStrategy,
    StageCCase,
    StageCCategory,
    StageCVariant,
)
from app.evals.stage_c.scoring import (
    IdentityIndex,
    StableCitation,
    VariantMetrics,
    normalize_citations,
    score_variant,
)
from app.knowledge.base import RetrievalEvent
from app.knowledge.results import AdaptiveSearchResult, BaselineSearchResult


_INFRASTRUCTURE_ERROR_CODES = {
    "VECTOR_STORE_UNAVAILABLE",
    "EMBEDDING_UNAVAILABLE",
    "SEARCH_INTERNAL_ERROR",
}
_MAX_ROUNDS = 2
_MAX_QUERIES_BY_ROUND = (3, 2)
_MAX_MODEL_CALLS = 3


@dataclass(frozen=True)
class VariantResult:
    """One checkpoint-safe normalized result for one case and variant."""

    case_id: str
    variant: StageCVariant
    status: Literal["completed", "infrastructure_failed"]
    attempt: int
    error_code: str | None
    strategy: str | None
    strategy_allowed: bool | None
    strategy_preferred: bool | None
    evidence_status: str | None
    round_count: int
    query_count_by_round: tuple[int, ...]
    model_calls: int
    tokens: int
    latency_ms: float
    evaluated_citation_count: int
    full_citation_count: int
    citations: tuple[Mapping[str, object], ...]
    candidate_trace: tuple[Mapping[str, object], ...]
    metrics: VariantMetrics | None
    safety_flags: Mapping[str, object]

    def to_payload(self) -> dict[str, object]:
        """Return only fields accepted by ``CheckpointResult.payload``."""
        payload: dict[str, object] = {
            "case_id": self.case_id,
            "variant": self.variant.value,
            "status": self.status,
            "attempt": self.attempt,
            "evidence_status": self.evidence_status,
            "round_count": self.round_count,
            "query_count_by_round": list(self.query_count_by_round),
            "model_calls": self.model_calls,
            "tokens": self.tokens,
            "latency_ms": self.latency_ms,
            "evaluated_citation_count": self.evaluated_citation_count,
            "full_citation_count": self.full_citation_count,
            "citations": [dict(item) for item in self.citations],
            "candidate_trace": [dict(item) for item in self.candidate_trace],
            "metrics": None if self.metrics is None else _metrics_payload(self.metrics),
            "safety_flags": dict(self.safety_flags),
        }
        if self.error_code is not None:
            payload["error_code"] = self.error_code
        if self.strategy is not None:
            payload["strategy"] = self.strategy
        if self.strategy_allowed is not None:
            payload["strategy_allowed"] = self.strategy_allowed
        if self.strategy_preferred is not None:
            payload["strategy_preferred"] = self.strategy_preferred
        return payload


def _metrics_payload(metrics: VariantMetrics) -> dict[str, object]:
    return {
        "covered_group_count": metrics.covered_group_count,
        "required_group_count": metrics.required_group_count,
        "evidence_group_recall": metrics.evidence_group_recall,
        "retrieval_precision": metrics.retrieval_precision,
        "complete_evidence_coverage": metrics.complete_evidence_coverage,
        "relevant_top5_count": metrics.relevant_top5_count,
        "evaluated_citation_count": metrics.evaluated_citation_count,
        "returned_full_count": metrics.returned_full_count,
        "cross_tenant_leak": metrics.cross_tenant_leak,
        "disabled_document_leak": metrics.disabled_document_leak,
        "inactive_version_leak": metrics.inactive_version_leak,
        "unknown_identity_count": metrics.unknown_identity_count,
        "quality_scope_reason": metrics.quality_scope_reason,
    }


def _stable_identity_fields(identity, *, identity_consistent: bool) -> dict[str, object]:
    artifact: dict[str, object] = {
        "chunk_id": identity.chunk_id,
        "identity_known": identity.known,
        "identity_consistent": identity_consistent,
    }
    if not identity.known:
        return artifact
    artifact.update(
        {
            "tenant_key": identity.tenant_key.value,
            "document_key": identity.document_key,
            "document_id": identity.document_id,
            "version_id": identity.version_id,
            "heading_path": identity.heading_path,
            "document_status": identity.document_status.value,
            "active_version_id": identity.active_version_id,
        }
    )
    return artifact


def _citation_payload(citation: StableCitation) -> dict[str, object]:
    payload = {
        "citation_id": citation.citation_id,
        "rank": citation.citation_rank,
        "document_id": citation.document_id,
        "version_id": citation.version_id,
        "chunk_id": citation.chunk_id,
        "heading_path": citation.heading_path,
        "identity_known": citation.identity_known,
        "identity_consistent": citation.identity_consistent,
    }
    if citation.tenant_key is not None:
        payload["tenant_key"] = citation.tenant_key.value
    if citation.document_key is not None:
        payload["document_key"] = citation.document_key
    if citation.document_status is not None:
        payload["document_status"] = citation.document_status.value
    if citation.active_version_id is not None:
        payload["active_version_id"] = citation.active_version_id
    return payload


def _nonnegative_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("persisted retrieval count must be a non-negative integer")
    return value


def _load_trace(
    event: RetrievalEvent, variant: StageCVariant
) -> tuple[dict[str, object], tuple[int, ...]]:
    try:
        raw = json.loads(event.candidate_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("retrieval trace is not valid JSON") from exc
    if not isinstance(raw, dict):
        raise ValueError("retrieval trace must be an object")
    expected_schema = 4 if variant is StageCVariant.ADAPTIVE else 2
    if raw.get("schema_version") != expected_schema:
        raise ValueError("retrieval trace schema is invalid")
    candidates = raw.get("candidates")
    if not isinstance(candidates, list) or any(
        not isinstance(item, dict) for item in candidates
    ):
        raise ValueError("retrieval trace candidates are invalid")

    if variant is StageCVariant.BASELINE:
        return raw, (1,)

    round_count = _nonnegative_int(event.round_count)
    queries = raw.get("queries")
    if not isinstance(queries, list):
        raise ValueError("adaptive retrieval trace queries are missing")
    query_counts = [0] * round_count
    query_indexes_by_round: list[list[int]] = [[] for _ in range(round_count)]
    seen: set[tuple[int, int]] = set()
    for item in queries:
        if not isinstance(item, dict):
            raise ValueError("adaptive retrieval query trace is invalid")
        round_number = item.get("round")
        query_index = item.get("query_index")
        query_digest = item.get("query_digest")
        if (
            isinstance(round_number, bool)
            or not isinstance(round_number, int)
            or round_number < 1
            or round_number > round_count
            or isinstance(query_index, bool)
            or not isinstance(query_index, int)
            or query_index < 1
            or not isinstance(query_digest, str)
            or not query_digest
        ):
            raise ValueError("adaptive retrieval query trace is invalid")
        key = (round_number, query_index)
        if key in seen:
            raise ValueError("adaptive retrieval query trace contains duplicates")
        seen.add(key)
        query_counts[round_number - 1] += 1
        query_indexes_by_round[round_number - 1].append(query_index)
    if any(
        not indexes or sorted(indexes) != list(range(1, len(indexes) + 1))
        for indexes in query_indexes_by_round
    ):
        raise ValueError("adaptive retrieval query indexes are not contiguous")
    return raw, tuple(query_counts)


def _candidate_payloads(
    trace: Mapping[str, object], identity_index: IdentityIndex
) -> tuple[Mapping[str, object], ...]:
    normalized: list[Mapping[str, object]] = []
    for raw in trace["candidates"]:  # type: ignore[index]
        chunk_id = raw.get("chunk_id")
        if not isinstance(chunk_id, str) or not chunk_id:
            raise ValueError("retrieval candidate chunk_id is invalid")
        identity = identity_index.resolve(chunk_id)
        artifact = _stable_identity_fields(
            identity, identity_consistent=identity.known
        )
        for name in ("round", "query_index"):
            if name in raw:
                value = raw[name]
                if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                    raise ValueError(f"retrieval candidate {name} is invalid")
                artifact[name] = value
        if "fused_rank" in raw:
            rank = raw["fused_rank"]
            if isinstance(rank, bool) or not isinstance(rank, int) or rank < 1:
                raise ValueError("retrieval candidate rank is invalid")
            artifact["rank"] = rank
        if "fused_score" in raw:
            score = raw["fused_score"]
            if (
                isinstance(score, bool)
                or not isinstance(score, (int, float))
                or not math.isfinite(float(score))
            ):
                raise ValueError("retrieval candidate score is invalid")
            artifact["score"] = float(score)
        normalized.append(artifact)
    return tuple(normalized)


def _coarse_error_code(raw_code: str | None) -> str:
    if raw_code in {"VECTOR_STORE_UNAVAILABLE", "EMBEDDING_UNAVAILABLE"}:
        return "provider_unavailable"
    if raw_code == "SEARCH_INTERNAL_ERROR":
        return "provider_error"
    return "unexpected_error"


def _evidence_status(raw: str, *, infrastructure_failed: bool) -> str | None:
    if infrastructure_failed:
        return None
    return {
        "sufficient": "complete",
        "insufficient": "missing",
        "partial": "partial",
        "not_needed": "not_applicable",
        "failed": "missing",
    }.get(raw, "missing")


def _budget_flags(
    case: StageCCase,
    *,
    query_counts: tuple[int, ...],
    round_count: int,
    model_calls: int,
    budget_exceeded: bool,
) -> dict[str, object]:
    expectations = case.security_expectations
    round_limit = (
        expectations.max_search_rounds
        if expectations and expectations.max_search_rounds is not None
        else _MAX_ROUNDS
    )
    query_limits = list(_MAX_QUERIES_BY_ROUND)
    if expectations:
        if expectations.max_first_round_queries is not None:
            query_limits[0] = expectations.max_first_round_queries
        if expectations.max_second_round_queries is not None:
            query_limits[1] = expectations.max_second_round_queries
    query_limit_exceeded = any(
        count > (query_limits[index] if index < len(query_limits) else 0)
        for index, count in enumerate(query_counts)
    )
    return {
        "budget_exceeded": budget_exceeded,
        "round_limit_exceeded": round_count > round_limit,
        "query_limit_exceeded": query_limit_exceeded,
        "model_call_limit_exceeded": model_calls > _MAX_MODEL_CALLS,
    }


def normalize_result(
    case: StageCCase,
    variant: StageCVariant,
    result: BaselineSearchResult | AdaptiveSearchResult,
    event: RetrievalEvent | None,
    identity_index: IdentityIndex,
    attempt: int,
) -> VariantResult:
    """Normalize production retrieval output through its persisted audit event."""
    if not isinstance(variant, StageCVariant):
        raise TypeError("variant must be a StageCVariant")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise ValueError("attempt must be a positive integer")

    trace: Mapping[str, object] = {"candidates": []}
    trace_valid = event is not None
    query_counts: tuple[int, ...] = ()
    round_count = 0
    model_calls = 0
    tokens = 0
    latency_ms = 0.0
    if event is not None:
        try:
            trace, query_counts = _load_trace(event, variant)
            if variant is StageCVariant.BASELINE:
                round_count = 1
                query_counts = (1,)
                model_calls = 0
            else:
                round_count = _nonnegative_int(event.round_count)
                model_calls = _nonnegative_int(event.model_calls)
            tokens = _nonnegative_int(event.estimated_tokens)
            latency_ms = float(_nonnegative_int(event.latency_ms))
        except (TypeError, ValueError):
            trace_valid = False

    strategy = None
    parsed_strategy: RetrievalStrategy | None = None
    invalid_adaptive_strategy = False
    if variant is StageCVariant.ADAPTIVE and event is not None:
        if event.strategy == "none":
            strategy = "none"
        else:
            try:
                parsed_strategy = RetrievalStrategy(event.strategy)
            except (TypeError, ValueError):
                invalid_adaptive_strategy = True
            else:
                strategy = parsed_strategy.value

    raw_error_code = result.error.code if result.error is not None else None
    infrastructure_failed = (
        not trace_valid
        or invalid_adaptive_strategy
        or raw_error_code in _INFRASTRUCTURE_ERROR_CODES
        or (
            not result.ok
            and raw_error_code
            not in {"INSUFFICIENT_EVIDENCE", "SEARCH_BUDGET_EXCEEDED"}
        )
    )
    error_code: str | None = None
    if infrastructure_failed:
        error_code = (
            "unexpected_error"
            if invalid_adaptive_strategy
            else _coarse_error_code(raw_error_code)
        )
    elif raw_error_code == "SEARCH_BUDGET_EXCEEDED":
        error_code = "budget_exhausted"

    stable_citations = normalize_citations(result.citations, identity_index)
    citation_artifacts = tuple(_citation_payload(item) for item in stable_citations)
    try:
        candidate_artifacts = (
            _candidate_payloads(trace, identity_index) if trace_valid else ()
        )
    except (TypeError, ValueError):
        infrastructure_failed = True
        error_code = "unexpected_error"
        candidate_artifacts = ()

    metrics = None if infrastructure_failed else score_variant(
        case, stable_citations, top_k=5
    )
    strategy_allowed = None
    strategy_preferred = None
    if variant is StageCVariant.ADAPTIVE and not invalid_adaptive_strategy:
        if strategy == "none":
            if case.strategy_expectation is not None:
                strategy_allowed = False
                strategy_preferred = False
        elif parsed_strategy is not None and case.strategy_expectation is not None:
            strategy_allowed = parsed_strategy in case.strategy_expectation.allowed
            strategy_preferred = parsed_strategy is case.strategy_expectation.preferred

    flags = _budget_flags(
        case,
        query_counts=query_counts,
        round_count=round_count,
        model_calls=model_calls,
        budget_exceeded=raw_error_code == "SEARCH_BUDGET_EXCEEDED",
    )
    if metrics is not None:
        flags.update(
            {
                "cross_tenant_leak": metrics.cross_tenant_leak,
                "disabled_document_leak": metrics.disabled_document_leak,
                "inactive_version_leak": metrics.inactive_version_leak,
                "unknown_identity_count": sum(
                    not item.identity_known for item in stable_citations
                )
                + sum(
                    item.get("identity_known") is False
                    for item in candidate_artifacts
                ),
            }
        )
    else:
        flags.update(
            {
                "cross_tenant_leak": False,
                "disabled_document_leak": False,
                "inactive_version_leak": False,
                "unknown_identity_count": 0,
            }
        )

    evaluated_count = 0 if metrics is None else metrics.evaluated_citation_count
    full_count = len(stable_citations)
    return VariantResult(
        case_id=case.case_id,
        variant=variant,
        status=("infrastructure_failed" if infrastructure_failed else "completed"),
        attempt=attempt,
        error_code=error_code,
        strategy=strategy,
        strategy_allowed=strategy_allowed,
        strategy_preferred=strategy_preferred,
        evidence_status=_evidence_status(
            result.retrieval_summary.evidence_status,
            infrastructure_failed=infrastructure_failed,
        ),
        round_count=round_count,
        query_count_by_round=query_counts,
        model_calls=model_calls,
        tokens=tokens,
        latency_ms=latency_ms,
        evaluated_citation_count=evaluated_count,
        full_citation_count=full_count,
        citations=citation_artifacts,
        candidate_trace=candidate_artifacts,
        metrics=metrics,
        safety_flags=flags,
    )


def _fraction(
    numerator: int, denominator: int, *, scope_reason: str
) -> dict[str, object]:
    result: dict[str, object] = {
        "numerator": numerator,
        "denominator": denominator,
        "value": None if denominator == 0 else numerator / denominator,
    }
    if denominator == 0:
        result["scope_reason"] = scope_reason
    return result


def _hard_limit_violated(payload: Mapping[str, object]) -> bool:
    round_count = payload.get("round_count", 0)
    model_calls = payload.get("model_calls", 0)
    query_counts = payload.get("query_count_by_round", ())
    return bool(
        round_count > _MAX_ROUNDS
        or model_calls > _MAX_MODEL_CALLS
        or any(
            count
            > (
                _MAX_QUERIES_BY_ROUND[index]
                if index < len(_MAX_QUERIES_BY_ROUND)
                else 0
            )
            for index, count in enumerate(query_counts)
        )
    )


def _aggregate_payloads(
    payloads: Sequence[Mapping[str, object]],
    *,
    strategy_expected: bool,
) -> dict[str, object]:
    completed = [item for item in payloads if item.get("status") == "completed"]
    metrics = [
        item["metrics"]
        for item in completed
        if isinstance(item.get("metrics"), Mapping)
        and item["metrics"].get("required_group_count", 0) > 0
    ]
    recall_numerator = sum(item["covered_group_count"] for item in metrics)
    recall_denominator = sum(item["required_group_count"] for item in metrics)
    precision_numerator = sum(item["relevant_top5_count"] for item in metrics)
    precision_denominator = sum(item["evaluated_citation_count"] for item in metrics)
    complete_numerator = sum(
        item.get("complete_evidence_coverage") is True for item in metrics
    )
    allowed = [item["strategy_allowed"] for item in completed if "strategy_allowed" in item]
    preferred = [
        item["strategy_preferred"]
        for item in completed
        if "strategy_preferred" in item
    ]
    no_measurement = "no_completed_measurable_variants"
    strategy_reason = (
        "no_completed_strategy_measurements"
        if strategy_expected
        else "no_strategy_expectation"
    )
    return {
        "completed_variant_count": len(completed),
        "evidence_group_recall": _fraction(
            recall_numerator, recall_denominator, scope_reason=no_measurement
        ),
        "retrieval_precision": _fraction(
            precision_numerator, precision_denominator, scope_reason=no_measurement
        ),
        "complete_evidence_coverage": _fraction(
            complete_numerator, len(metrics), scope_reason=no_measurement
        ),
        "strategy_allowed": _fraction(
            sum(value is True for value in allowed),
            len(allowed),
            scope_reason=strategy_reason,
        ),
        "strategy_preferred": _fraction(
            sum(value is True for value in preferred),
            len(preferred),
            scope_reason=strategy_reason,
        ),
        "cross_tenant_leak_count": sum(
            item.get("safety_flags", {}).get("cross_tenant_leak") is True
            for item in completed
        ),
        "disabled_document_leak_count": sum(
            item.get("safety_flags", {}).get("disabled_document_leak") is True
            for item in completed
        ),
        "inactive_version_leak_count": sum(
            item.get("safety_flags", {}).get("inactive_version_leak") is True
            for item in completed
        ),
        "unknown_identity_count": sum(
            item.get("safety_flags", {}).get("unknown_identity_count", 0)
            for item in completed
        ),
        "budget_exceeded_count": sum(
            item.get("safety_flags", {}).get("budget_exceeded") is True
            for item in completed
        ),
        "case_security_expectation_violation_count": sum(
            any(
                item.get("safety_flags", {}).get(name) is True
                for name in (
                    "round_limit_exceeded",
                    "query_limit_exceeded",
                    "model_call_limit_exceeded",
                )
            )
            for item in completed
        ),
        "hard_limit_violation_count": sum(
            _hard_limit_violated(item) for item in completed
        ),
    }


def compile_stage_c_report(
    cases: Sequence[StageCCase], checkpoint: CheckpointState
) -> dict[str, object]:
    """Compile stable case ordering and count-backed aggregates from checkpoint data."""
    ordered_cases = tuple(cases)
    variant_order = (StageCVariant.BASELINE, StageCVariant.ADAPTIVE)
    case_rows: list[dict[str, object]] = []
    completed_variants = 0
    infrastructure_failed_variants = 0
    missing_variants = 0
    completed_pairs = 0
    incomplete_pairs: list[dict[str, object]] = []
    payload_by_variant: dict[StageCVariant, list[Mapping[str, object]]] = {
        variant: [] for variant in variant_order
    }

    for case in ordered_cases:
        variants: dict[str, object] = {}
        missing_for_case: list[str] = []
        failed_for_case: list[str] = []
        for variant in variant_order:
            persisted = checkpoint.result(case.case_id, variant)
            if persisted is None:
                variants[variant.value] = None
                missing_variants += 1
                missing_for_case.append(variant.value)
                continue
            payload = persisted.to_dict()["payload"]
            variants[variant.value] = payload
            payload_by_variant[variant].append(payload)
            if payload.get("status") == "completed":
                completed_variants += 1
            else:
                infrastructure_failed_variants += 1
                failed_for_case.append(variant.value)
        if not missing_for_case and not failed_for_case:
            completed_pairs += 1
        else:
            incomplete_pairs.append(
                {
                    "case_id": case.case_id,
                    "missing_variants": missing_for_case,
                    "infrastructure_failed_variants": failed_for_case,
                }
            )
        case_rows.append(
            {
                "case_id": case.case_id,
                "category": case.category.value,
                "variants": variants,
            }
        )

    overall = {
        variant.value: _aggregate_payloads(
            payload_by_variant[variant],
            strategy_expected=(
                variant is StageCVariant.ADAPTIVE
                and any(case.strategy_expectation is not None for case in ordered_cases)
            ),
        )
        for variant in variant_order
    }
    by_category: dict[str, object] = {}
    for category in StageCCategory:
        category_ids = {
            case.case_id for case in ordered_cases if case.category is category
        }
        category_cases = [case for case in ordered_cases if case.category is category]
        by_category[category.value] = {
            variant.value: _aggregate_payloads(
                [
                    payload
                    for payload in payload_by_variant[variant]
                    if payload.get("case_id") in category_ids
                ],
                strategy_expected=(
                    variant is StageCVariant.ADAPTIVE
                    and any(
                        case.strategy_expectation is not None
                        for case in category_cases
                    )
                ),
            )
            for variant in variant_order
        }

    all_completed_payloads = [
        payload
        for payloads in payload_by_variant.values()
        for payload in payloads
        if payload.get("status") == "completed"
    ]
    return {
        "completion": {
            "case_count": len(ordered_cases),
            "expected_variants": len(ordered_cases) * len(variant_order),
            "completed_variants": completed_variants,
            "infrastructure_failed_variants": infrastructure_failed_variants,
            "missing_variants": missing_variants,
            "completed_pairs": completed_pairs,
            "incomplete_pairs": incomplete_pairs,
        },
        "cases": case_rows,
        "aggregates": {
            "overall": overall,
            "by_category": by_category,
            "security": {
                "cross_tenant_leak_count": sum(
                    payload.get("safety_flags", {}).get("cross_tenant_leak") is True
                    for payload in all_completed_payloads
                ),
                "disabled_document_leak_count": sum(
                    payload.get("safety_flags", {}).get("disabled_document_leak") is True
                    for payload in all_completed_payloads
                ),
                "inactive_version_leak_count": sum(
                    payload.get("safety_flags", {}).get("inactive_version_leak") is True
                    for payload in all_completed_payloads
                ),
                "unknown_identity_count": sum(
                    payload.get("safety_flags", {}).get("unknown_identity_count", 0)
                    for payload in all_completed_payloads
                ),
            },
            "budget": {
                "budget_exceeded_count": sum(
                    payload.get("safety_flags", {}).get("budget_exceeded") is True
                    for payload in all_completed_payloads
                ),
                "hard_limit_violation_count": sum(
                    _hard_limit_violated(payload)
                    for payload in all_completed_payloads
                ),
                "case_security_expectation_violation_count": sum(
                    any(
                        payload.get("safety_flags", {}).get(name) is True
                        for name in (
                            "round_limit_exceeded",
                            "query_limit_exceeded",
                            "model_call_limit_exceeded",
                        )
                    )
                    for payload in all_completed_payloads
                ),
            },
        },
    }
