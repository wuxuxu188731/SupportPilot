"""Deterministic server-owned budgets for adaptive knowledge search."""

from __future__ import annotations

import math
import time
import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from app.knowledge.base import (
    EmbeddingUnavailableError,
    InsufficientEvidenceError,
    KnowledgeError,
    KnowledgeStore,
    RetrievalEvent,
    SearchBudgetExceededError,
    SearchInternalError,
    VectorStoreUnavailableError,
)
from app.knowledge.evidence import (
    ASSESSOR_PROMPT_VERSION,
    EvidenceAssessor,
    EvidenceStatus,
    RoundEvidence,
)
from app.knowledge.planning import (
    PLANNER_PROMPT_VERSION,
    QueryPlanner,
    SearchStrategy,
)
from app.knowledge.results import (
    AdaptiveSearchResult,
    Citation,
    RetrievalSummary,
)
from app.knowledge.retrieval import HybridRetriever

MAX_PLANNER_CALLS = 1
MAX_ROUNDS = 2
MAX_ROUND_QUERIES = (3, 2)
MAX_ASSESSOR_CALLS = 2
MAX_MODEL_CALLS = 3
ADAPTIVE_TOP_K = 6
ADAPTIVE_TOKEN_BUDGET = 3000

logger = logging.getLogger(__name__)


def query_digest(query: str) -> str:
    return "sha256:" + hashlib.sha256(query.encode("utf-8")).hexdigest()


@dataclass
class SearchBudget:
    """Tracks hard search limits that model output cannot alter."""

    deadline: float
    clock: Callable[[], float]
    planner_calls: int = 0
    assessor_calls: int = 0
    round_count: int = 0

    @classmethod
    def start(
        cls,
        *,
        timeout_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> "SearchBudget":
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        return cls(deadline=clock() + timeout_seconds, clock=clock)

    @property
    def model_calls(self) -> int:
        return self.planner_calls + self.assessor_calls

    def remaining_seconds(self) -> float:
        return self.deadline - self.clock()

    def ensure_time(self) -> None:
        if self.remaining_seconds() <= 0:
            raise SearchBudgetExceededError()

    def consume_planner_call(self) -> None:
        self.ensure_time()
        if (
            self.planner_calls >= MAX_PLANNER_CALLS
            or self.model_calls >= MAX_MODEL_CALLS
        ):
            raise SearchBudgetExceededError()
        self.planner_calls += 1

    def consume_assessor_call(self) -> None:
        self.ensure_time()
        if (
            self.assessor_calls >= MAX_ASSESSOR_CALLS
            or self.model_calls >= MAX_MODEL_CALLS
        ):
            raise SearchBudgetExceededError()
        self.assessor_calls += 1

    def start_round(self, *, query_count: int) -> None:
        self.ensure_time()
        if self.round_count >= MAX_ROUNDS:
            raise SearchBudgetExceededError()
        if (
            isinstance(query_count, bool)
            or not isinstance(query_count, int)
            or query_count < 1
            or query_count > MAX_ROUND_QUERIES[self.round_count]
        ):
            raise SearchBudgetExceededError()
        self.round_count += 1

    def external_timeout_seconds(self, max_seconds: int = 5) -> int:
        if (
            isinstance(max_seconds, bool)
            or not isinstance(max_seconds, int)
            or max_seconds < 1
        ):
            raise ValueError("max_seconds must be a positive integer")
        timeout = math.floor(min(max_seconds, self.remaining_seconds()))
        if timeout < 1:
            raise SearchBudgetExceededError()
        return timeout


class AdaptiveKnowledgeSearchService:
    """Run one server-bounded adaptive search and record one safe event."""

    def __init__(
        self,
        *,
        store: KnowledgeStore,
        retriever: HybridRetriever,
        planner: QueryPlanner,
        assessor: EvidenceAssessor,
        timeout_seconds: float = 15.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._retriever = retriever
        self._planner = planner
        self._assessor = assessor
        self._timeout_seconds = timeout_seconds
        self._clock = clock

    def search(
        self,
        *,
        organization_id: str,
        question: str,
        conversation_id: str | None = None,
    ) -> AdaptiveSearchResult:
        clean_question = question.strip()
        if not clean_question:
            raise ValueError("question must not be blank")

        started = self._clock()
        budget = SearchBudget.start(
            timeout_seconds=self._timeout_seconds, clock=self._clock
        )
        strategy = "unplanned"
        reason_code = None
        failure_stage = None
        planned_queries: list[str] = []
        trace_queries: list[dict] = []
        trace_candidates: list[dict] = []
        evidence_by_id: dict[str, RoundEvidence] = {}
        selected: list[RoundEvidence] = []
        assessment = None
        estimated_tokens = 0
        result: AdaptiveSearchResult | None = None
        outcome = "failed"

        try:
            planner_timeout = budget.external_timeout_seconds()
            budget.consume_planner_call()
            decision = self._planner.plan(
                question=clean_question,
                timeout_seconds=planner_timeout,
            )
            budget.ensure_time()
            estimated_tokens += decision.estimated_tokens
            plan = decision.plan
            strategy = plan.strategy.value.lower()
            reason_code = plan.reason_code.value

            if plan.strategy is SearchStrategy.NONE:
                outcome = "sufficient"
                result = self._result(
                    ok=True,
                    strategy=strategy,
                    round_count=0,
                    evidence_status="not_needed",
                    started=started,
                )
                return result

            round_queries = list(plan.queries)
            executed_keys: set[str] = set()
            while round_queries:
                budget.start_round(query_count=len(round_queries))
                round_number = budget.round_count
                for query_index, query in enumerate(round_queries):
                    planned_queries.append(query)
                    executed_keys.add(query.casefold())
                    trace_queries.append(
                        {
                            "round": round_number,
                            "query_index": query_index + 1,
                            "query_digest": query_digest(query),
                        }
                    )
                    retrieval = self._retriever.retrieve(
                        organization_id=organization_id,
                        query=query,
                        timeout_provider=budget.external_timeout_seconds,
                    )
                    budget.ensure_time()
                    estimated_tokens += retrieval.query_tokens
                    for rank, scored in enumerate(
                        retrieval.ranked_chunks, start=1
                    ):
                        chunk_id = scored.chunk.chunk_id
                        existing = evidence_by_id.get(chunk_id)
                        matched = (query_index,)
                        if existing is None:
                            evidence_by_id[chunk_id] = RoundEvidence(
                                chunk=scored.chunk,
                                fused_score=scored.fused_score,
                                matched_query_indexes=matched,
                                first_round=round_number,
                                first_query_index=query_index,
                            )
                        else:
                            merged = tuple(
                                sorted(
                                    set(existing.matched_query_indexes)
                                    | {query_index}
                                )
                            )
                            evidence_by_id[chunk_id] = RoundEvidence(
                                chunk=(
                                    scored.chunk
                                    if scored.fused_score > existing.fused_score
                                    else existing.chunk
                                ),
                                fused_score=max(
                                    scored.fused_score, existing.fused_score
                                ),
                                matched_query_indexes=merged,
                                first_round=existing.first_round,
                                first_query_index=existing.first_query_index,
                            )
                        trace_candidates.append(
                            {
                                "round": round_number,
                                "query_index": query_index + 1,
                                "chunk_id": chunk_id,
                                "fused_rank": rank,
                                "fused_score": scored.fused_score,
                                "resolution_status": "active",
                                "selection_reason": "candidate",
                            }
                        )

                selected = self._select_evidence(evidence_by_id.values())
                assessor_timeout = budget.external_timeout_seconds()
                budget.consume_assessor_call()
                assessment_decision = self._assessor.assess(
                    plan=plan,
                    evidence=selected,
                    round_number=round_number,
                    timeout_seconds=assessor_timeout,
                )
                if assessment_decision.model_calls == 0:
                    budget.assessor_calls -= 1
                budget.ensure_time()
                estimated_tokens += assessment_decision.estimated_tokens
                assessment = assessment_decision.assessment
                if assessment.status is EvidenceStatus.SUFFICIENT:
                    outcome = "sufficient"
                    break
                if (
                    plan.strategy is not SearchStrategy.MULTI
                    or round_number >= MAX_ROUNDS
                ):
                    break
                round_queries = []
                for followup in assessment.follow_up_queries:
                    key = followup.casefold()
                    if key not in executed_keys:
                        executed_keys.add(key)
                        round_queries.append(followup)
                if not round_queries:
                    break

            if assessment and assessment.status is EvidenceStatus.SUFFICIENT:
                citations = self._citations(selected)
                outcome = "sufficient"
                result = self._result(
                    ok=True,
                    strategy=strategy,
                    round_count=budget.round_count,
                    evidence_status="sufficient",
                    started=started,
                    citations=citations,
                    selected=[item.chunk for item in selected],
                )
            else:
                missing = (
                    list(assessment.missing_aspects)
                    if assessment
                    else ["no evidence found"]
                )
                error = InsufficientEvidenceError(missing_aspects=missing)
                outcome = "insufficient"
                result = self._result(
                    ok=False,
                    strategy=strategy,
                    round_count=budget.round_count,
                    evidence_status="insufficient",
                    started=started,
                    error=error,
                )
            return result
        except SearchBudgetExceededError as exc:
            failure_stage = "budget"
            result = self._failure(strategy, budget.round_count, started, exc)
            return result
        except (EmbeddingUnavailableError, VectorStoreUnavailableError) as exc:
            failure_stage = "retrieval"
            result = self._failure(strategy, budget.round_count, started, exc)
            return result
        except SearchInternalError as exc:
            failure_stage = "planner" if strategy == "unplanned" else "assessor"
            result = self._failure(strategy, budget.round_count, started, exc)
            return result
        finally:
            trace = {
                "schema_version": 4,
                "planner_prompt_version": PLANNER_PROMPT_VERSION,
                "assessor_prompt_version": ASSESSOR_PROMPT_VERSION,
                "reason_code": reason_code,
                "failure_stage": failure_stage,
                "queries": trace_queries,
                "candidates": trace_candidates,
            }
            selected_ids = {
                item.chunk.chunk_id for item in selected
            } if outcome == "sufficient" else set()
            for candidate in trace_candidates:
                if candidate["chunk_id"] in selected_ids:
                    candidate["resolution_status"] = "selected"
                    candidate["selection_reason"] = "selected"
            if result is not None:
                object.__setattr__(result, "retrieval_trace", trace)
            latency_ms = max(0, int(round((self._clock() - started) * 1000)))
            event = RetrievalEvent(
                event_id=str(uuid4()),
                organization_id=organization_id,
                conversation_id=conversation_id,
                strategy=strategy,
                original_query=query_digest(clean_question),
                planned_queries_json=json.dumps(
                    [query_digest(item) for item in planned_queries]
                ),
                round_count=budget.round_count,
                candidate_json=json.dumps(trace),
                selected_chunk_ids_json=json.dumps(
                    [item.chunk.chunk_id for item in selected]
                    if outcome == "sufficient"
                    else []
                ),
                outcome=outcome,
                latency_ms=latency_ms,
                model_calls=budget.model_calls,
                estimated_tokens=estimated_tokens + sum(
                    item.chunk.token_count for item in selected
                ),
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            try:
                self._store.record_retrieval_event(
                    organization_id=organization_id, event=event
                )
            except Exception as exc:  # event failure cannot alter search result
                logger.error(
                    "RETRIEVAL_EVENT_PERSIST_FAILED type=%s",
                    type(exc).__name__,
                )

    @staticmethod
    def _select_evidence(items) -> list[RoundEvidence]:
        ranked = sorted(items, key=lambda item: item.fused_score, reverse=True)
        selected: list[RoundEvidence] = []
        token_count = 0
        for item in ranked:
            if len(selected) >= ADAPTIVE_TOP_K:
                break
            if token_count + item.chunk.token_count > ADAPTIVE_TOKEN_BUDGET:
                continue
            selected.append(item)
            token_count += item.chunk.token_count
        return selected

    @staticmethod
    def _citations(items: list[RoundEvidence]) -> list[Citation]:
        return [
            Citation(
                citation_id=f"C{index}",
                document_id=item.chunk.document_id,
                version_id=item.chunk.version_id,
                chunk_id=item.chunk.chunk_id,
                title=item.chunk.document_title,
                heading_path=item.chunk.heading_path,
                content=item.chunk.content,
            )
            for index, item in enumerate(items, start=1)
        ]

    def _failure(self, strategy, rounds, started, error):
        return self._result(
            ok=False,
            strategy=strategy,
            round_count=rounds,
            evidence_status="failed",
            started=started,
            error=error,
        )

    def _result(
        self,
        *,
        ok,
        strategy,
        round_count,
        evidence_status,
        started,
        citations=(),
        selected=(),
        error=None,
    ):
        return AdaptiveSearchResult(
            ok=ok,
            citations=list(citations),
            retrieval_summary=RetrievalSummary(
                strategy=strategy,
                round_count=round_count,
                evidence_status=evidence_status,
                latency_ms=max(
                    0, int(round((self._clock() - started) * 1000))
                ),
            ),
            error=error,
            selected_chunks=list(selected),
        )
