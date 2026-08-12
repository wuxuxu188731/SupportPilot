"""Fixed Top-K baseline knowledge retrieval.

Runs a single raw question through the fixed pipeline: query embedding ->
tenant/version-filtered dense+sparse RRF (native Qdrant) -> second-pass SQLite
validation -> Top-5 citations within a 3,000-token budget, recording a body-free
retrieval event on every path.

The algorithm order is fixed (see ``search``):

1. validate the stripped question is non-empty
2. read the org's active version ids (empty -> insufficient, no embed/Qdrant)
3. embed the query once
4. Qdrant dense/sparse Top-8, native RRF returns at most 8 candidates
5. ``resolve_active_citations`` second-pass validation
6. dedupe by candidate score descending; same-version ordinal-difference-1
   chunks keep the higher-score one (order rebuilt from candidate scores)
7. within 3,000 tokens select at most 5; generate C1..Cn in final order
8. ``time.perf_counter()`` measures latency
9. a finally path persists the RetrievalEvent (metadata only, no bodies)
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from collections.abc import Callable
from typing import Sequence
from uuid import uuid4

from app.knowledge.base import (
    ChunkWithDocumentTitle,
    EmbeddingUnavailableError,
    InvalidDocumentError,
    KnowledgeStore,
    RetrievalEvent,
    VectorStoreUnavailableError,
)
from app.knowledge.embeddings import EmbeddingClient
from app.knowledge.results import (
    BaselineSearchResult,
    Citation,
    QueryRetrievalResult,
    RetrievalCandidateTrace,
    RetrievalSummary,
    RetrievalTrace,
    ScoredChunk,
    SelectionReason,
)
from app.knowledge.vector_store import VectorCandidate, VectorStore

STRATEGY = "baseline"
ROUND_COUNT = 1
TOP_K = 5
TOKEN_BUDGET = 3000
PREFETCH_LIMIT = 8
RESULT_LIMIT = 8
MODEL_CALLS = 0


class HybridRetriever:
    """Perform one tenant-scoped hybrid query without top-level side effects."""

    def __init__(
        self,
        *,
        store: KnowledgeStore,
        embedding: EmbeddingClient,
        vector_store: VectorStore,
    ) -> None:
        self._store = store
        self._embedding = embedding
        self._vector_store = vector_store

    def retrieve(
        self,
        *,
        organization_id: str,
        query: str,
        timeout_provider: Callable[[], int] | None = None,
    ) -> QueryRetrievalResult:
        clean_query = query.strip()
        if not clean_query:
            raise InvalidDocumentError(reason="query must not be blank")

        active_versions = self._store.list_active_version_ids(
            organization_id=organization_id
        )
        if not active_versions:
            return QueryRetrievalResult(
                query=clean_query,
                ranked_chunks=(),
                raw_candidates=(),
                query_tokens=0,
            )

        get_timeout = timeout_provider or (lambda: 5)
        query_embedding = self._embedding.embed_query(
            clean_query,
            timeout_seconds=get_timeout(),
        )
        candidates = self._vector_store.search(
            organization_id=organization_id,
            active_version_ids=active_versions,
            query_embedding=query_embedding,
            prefetch_limit=PREFETCH_LIMIT,
            result_limit=RESULT_LIMIT,
            timeout_seconds=get_timeout(),
        )

        candidate_by_id, _ = BaselineKnowledgeSearchService._canonical_candidates(
            candidates
        )
        resolved = self._store.resolve_active_citations(
            organization_id=organization_id,
            candidate_ids=list(candidate_by_id),
        )
        ranked = BaselineKnowledgeSearchService._rank_unique(
            resolved, candidate_by_id
        )
        return QueryRetrievalResult(
            query=clean_query,
            ranked_chunks=tuple(
                ScoredChunk(
                    chunk=chunk,
                    fused_score=candidate_by_id[chunk.chunk_id].score,
                )
                for chunk in ranked
            ),
            raw_candidates=tuple(candidates),
            query_tokens=query_embedding.token_count,
        )


class BaselineKnowledgeSearchService:
    """A single-question, fixed-budget, deterministic baseline retriever.

    Deliberately ignores adaptive-planning budgets: no planner, no assessor, one
    embedding call, one hybrid search, exactly 3,000-token / 5-citation caps.
    ``store``/``embedding``/``vector_store`` are Protocols, so any concrete
    implementation can be injected and tested with recording fakes.
    """

    def __init__(
        self,
        *,
        store: KnowledgeStore,
        embedding: EmbeddingClient,
        vector_store: VectorStore,
        retriever: HybridRetriever | None = None,
    ) -> None:
        self._store = store
        self._retriever = retriever or HybridRetriever(
            store=store,
            embedding=embedding,
            vector_store=vector_store,
        )

    # -------------------------------------------------------------- public

    def search(
        self,
        *,
        organization_id: str,
        question: str,
        conversation_id: str | None = None,
    ) -> BaselineSearchResult:
        clean_question = question.strip()
        if not clean_question:
            raise InvalidDocumentError(reason="question must not be blank")

        start = time.perf_counter()

        # State shared between the happy path and the finally-event writer.
        retrieval_trace = RetrievalTrace.empty()
        query_tokens = 0
        outcome = "insufficient"
        evidence_status = "insufficient"
        ok = True
        error = None
        citations: list[Citation] = []
        selected_chunks: list[ChunkWithDocumentTitle] = []

        try:
            retrieval = self._retriever.retrieve(
                organization_id=organization_id,
                query=clean_question,
            )
            query_tokens = retrieval.query_tokens
            candidates = list(retrieval.raw_candidates)
            if candidates:
                candidate_by_id, canonical_rank_by_id = (
                    self._canonical_candidates(candidates)
                )
                ranked = [item.chunk for item in retrieval.ranked_chunks]
                selected, selection_reason_by_id = self._select_within_budget(
                    ranked
                )
                retrieval_trace = self._build_trace(
                    candidates=candidates,
                    canonical_rank_by_id=canonical_rank_by_id,
                    resolved=ranked,
                    selection_reason_by_id=selection_reason_by_id,
                )
                if selected:
                    outcome = "sufficient"
                    evidence_status = "sufficient"
                    selected_chunks = selected
                    citations = self._build_citations(selected)
        except VectorStoreUnavailableError as exc:
            ok = False
            error = exc
            outcome = "failed"
            evidence_status = "failed"
        except EmbeddingUnavailableError as exc:
            ok = False
            error = exc
            outcome = "failed"
            evidence_status = "failed"
        finally:
            latency_ms = int(round((time.perf_counter() - start) * 1000))
            estimated_tokens = query_tokens + sum(
                ref.token_count for ref in selected_chunks
            )
            self._record_event(
                organization_id=organization_id,
                conversation_id=conversation_id,
                question=clean_question,
                retrieval_trace=retrieval_trace,
                selected_ids=[ref.chunk_id for ref in selected_chunks],
                outcome=outcome,
                latency_ms=latency_ms,
                estimated_tokens=estimated_tokens,
            )

        summary = RetrievalSummary(
            strategy=STRATEGY,
            round_count=ROUND_COUNT,
            evidence_status=evidence_status,
            latency_ms=latency_ms,
        )
        return BaselineSearchResult(
            ok=ok,
            citations=citations,
            retrieval_summary=summary,
            error=error,
            selected_chunks=selected_chunks,
            retrieval_trace=retrieval_trace,
        )

    # ------------------------------------------------------------- private

    @staticmethod
    def _canonical_candidates(
        candidates: Sequence[VectorCandidate],
    ) -> tuple[dict[str, VectorCandidate], dict[str, int]]:
        """Choose the highest-scored occurrence of each chunk ID."""
        candidate_by_id: dict[str, VectorCandidate] = {}
        canonical_rank_by_id: dict[str, int] = {}
        for rank, candidate in enumerate(candidates, start=1):
            current = candidate_by_id.get(candidate.chunk_id)
            if current is None or candidate.score > current.score:
                candidate_by_id[candidate.chunk_id] = candidate
                canonical_rank_by_id[candidate.chunk_id] = rank
        return candidate_by_id, canonical_rank_by_id

    @staticmethod
    def _rank_unique(
        resolved: Sequence[ChunkWithDocumentTitle],
        candidate_by_id: dict[str, VectorCandidate],
    ) -> list[ChunkWithDocumentTitle]:
        """Order active chunks by fused score and retain each chunk ID once.

        Ordinal describes document order, not semantic duplication. In
        particular, consecutive Markdown sections commonly have adjacent
        ordinals while carrying complementary policy evidence.
        """
        ranked = sorted(
            resolved,
            key=lambda ref: candidate_by_id[ref.chunk_id].score,
            reverse=True,
        )
        kept: list[ChunkWithDocumentTitle] = []
        seen_ids: set[str] = set()
        for ref in ranked:
            if ref.chunk_id in seen_ids:
                continue
            seen_ids.add(ref.chunk_id)
            kept.append(ref)
        return kept

    @classmethod
    def _select_within_budget(
        cls, ranked: Sequence[ChunkWithDocumentTitle]
    ) -> tuple[list[ChunkWithDocumentTitle], dict[str, SelectionReason]]:
        """Walk in final order, taking at most ``TOP_K`` chunks whose combined
        token count stays within ``TOKEN_BUDGET``."""
        selected: list[ChunkWithDocumentTitle] = []
        reasons: dict[str, SelectionReason] = {}
        total = 0
        for ref in ranked:
            if len(selected) >= TOP_K:
                reasons[ref.chunk_id] = "top_k_exceeded"
                continue
            if total + ref.token_count > TOKEN_BUDGET:
                reasons[ref.chunk_id] = "token_budget_exceeded"
                continue
            selected.append(ref)
            reasons[ref.chunk_id] = "selected"
            total += ref.token_count
        return selected, reasons

    @staticmethod
    def _build_trace(
        *,
        candidates: Sequence[VectorCandidate],
        canonical_rank_by_id: dict[str, int],
        resolved: Sequence[ChunkWithDocumentTitle],
        selection_reason_by_id: dict[str, SelectionReason],
    ) -> RetrievalTrace:
        resolved_ids = {ref.chunk_id for ref in resolved}
        traced: list[RetrievalCandidateTrace] = []
        for rank, candidate in enumerate(candidates, start=1):
            is_canonical = canonical_rank_by_id[candidate.chunk_id] == rank
            if not is_canonical:
                resolution_status = (
                    "active"
                    if candidate.chunk_id in resolved_ids
                    else "filtered_inactive_or_invalid"
                )
                reason: SelectionReason = "duplicate_chunk_id"
            elif candidate.chunk_id not in resolved_ids:
                resolution_status = "filtered_inactive_or_invalid"
                reason = "filtered_inactive_or_invalid"
            else:
                reason = selection_reason_by_id[candidate.chunk_id]
                resolution_status = (
                    "selected" if reason == "selected" else "active"
                )
            traced.append(
                RetrievalCandidateTrace(
                    chunk_id=candidate.chunk_id,
                    fused_rank=rank,
                    fused_score=candidate.score,
                    resolution_status=resolution_status,
                    selection_reason=reason,
                )
            )
        return RetrievalTrace(candidates=tuple(traced))

    @staticmethod
    def _build_citations(
        selected: Sequence[ChunkWithDocumentTitle],
    ) -> list[Citation]:
        return [
            Citation(
                citation_id=f"C{i}",
                document_id=ref.document_id,
                version_id=ref.version_id,
                chunk_id=ref.chunk_id,
                title=ref.document_title,
                heading_path=ref.heading_path,
                content=ref.content,
            )
            for i, ref in enumerate(selected, start=1)
        ]

    def _record_event(
        self,
        *,
        organization_id: str,
        conversation_id: str | None,
        question: str,
        retrieval_trace: RetrievalTrace,
        selected_ids: list[str],
        outcome: str,
        latency_ms: int,
        estimated_tokens: int,
    ) -> None:
        event = RetrievalEvent(
            event_id=str(uuid4()),
            organization_id=organization_id,
            conversation_id=conversation_id,
            strategy=STRATEGY,
            original_query=question,
            planned_queries_json=json.dumps([question]),
            round_count=ROUND_COUNT,
            candidate_json=json.dumps(retrieval_trace.to_dict()),
            selected_chunk_ids_json=json.dumps(selected_ids),
            outcome=outcome,
            latency_ms=latency_ms,
            model_calls=MODEL_CALLS,
            estimated_tokens=estimated_tokens,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self._store.record_retrieval_event(
            organization_id=organization_id,
            event=event,
        )
