"""Retrieval result objects and their public (HTTP/report) serialization.

``BaselineSearchResult`` deliberately separates the public surface (``ok``,
``citations``, ``retrieval_summary``, ``error``) from the internal
``selected_chunks`` (which carry raw body text). Only ``public_dict()`` is used
for HTTP/report output, so the full document body that backs a citation is
never broadcast beyond the structured citation ``content`` already chosen by
the retriever.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Literal, Sequence

from app.knowledge.base import ChunkWithDocumentTitle, KnowledgeError
from app.knowledge.vector_store import VectorCandidate


def _public_dict(instance) -> dict:
    """Serialize a frozen dataclass to a public dict (no private fields)."""
    return {
        field.name: getattr(instance, field.name)
        for field in fields(instance)
        if not field.name.startswith("_")
    }


@dataclass(frozen=True)
class Citation:
    """A single grounded, structured reference.

    ``citation_id`` is the server-generated ``C1..Cn`` label the model may cite
    in its answer; ``content`` is the trusted fragment body that came only from
    SQLite (never from a vector-store payload).
    """

    citation_id: str
    document_id: str
    version_id: str
    chunk_id: str
    title: str
    heading_path: str | None
    content: str

    def public_dict(self) -> dict:
        return _public_dict(self)


@dataclass(frozen=True)
class RetrievalSummary:
    """Public metrics for one retrieval round, with no internal reasoning."""

    strategy: str
    round_count: int
    evidence_status: str
    latency_ms: int

    def public_dict(self) -> dict:
        return _public_dict(self)


ResolutionStatus = Literal[
    "selected",
    "active",
    "filtered_inactive_or_invalid",
]
SelectionReason = Literal[
    "selected",
    "top_k_exceeded",
    "token_budget_exceeded",
    "filtered_inactive_or_invalid",
    "duplicate_chunk_id",
]


@dataclass(frozen=True)
class RetrievalCandidateTrace:
    """Content-free diagnostic decision for one fused candidate occurrence."""

    chunk_id: str
    fused_rank: int
    fused_score: float
    resolution_status: ResolutionStatus
    selection_reason: SelectionReason

    def to_dict(self) -> dict:
        return _public_dict(self)


@dataclass(frozen=True)
class RetrievalTrace:
    """Versioned, metadata-only retrieval trace used by eval and audit logs."""

    candidates: Sequence[RetrievalCandidateTrace] = ()
    schema_version: int = 2

    @classmethod
    def empty(cls) -> "RetrievalTrace":
        return cls()

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }


@dataclass(frozen=True)
class ScoredChunk:
    """One SQLite-validated chunk paired with its fused vector score."""

    chunk: ChunkWithDocumentTitle
    fused_score: float


@dataclass(frozen=True)
class QueryRetrievalResult:
    """Internal result of one event-free hybrid retrieval query."""

    query: str
    ranked_chunks: tuple[ScoredChunk, ...]
    raw_candidates: tuple[VectorCandidate, ...]
    query_tokens: int


@dataclass(frozen=True)
class BaselineSearchResult:
    """Top-level outcome of a baseline knowledge search.

    ``ok`` is True whenever retrieval ran to completion; ``evidence_status``
    distinguishes a healthy-but-empty ("insufficient") run from an
    infrastructure failure. ``selected_chunks`` is diagnostic-only internal state
    and must never be serialized.
    """

    ok: bool
    citations: Sequence[Citation]
    retrieval_summary: RetrievalSummary
    error: KnowledgeError | None
    selected_chunks: Sequence[ChunkWithDocumentTitle]
    retrieval_trace: RetrievalTrace = field(default_factory=RetrievalTrace.empty)

    def public_dict(self) -> dict:
        """HTTP/report-safe shape that never exposes ``selected_chunks``."""
        return {
            "ok": self.ok,
            "citations": [
                citation.public_dict() for citation in self.citations
            ],
            "retrieval_summary": self.retrieval_summary.public_dict(),
            "error": (
                None
                if self.error is None
                else {
                    "code": self.error.code,
                    "message": self.error.safe_message,
                }
            ),
        }


@dataclass(frozen=True)
class AdaptiveSearchResult:
    """Public adaptive result with all raw chunks and trace kept internal."""

    ok: bool
    citations: Sequence[Citation]
    retrieval_summary: RetrievalSummary
    error: KnowledgeError | None
    selected_chunks: Sequence[ChunkWithDocumentTitle]
    retrieval_trace: dict = field(default_factory=dict)

    def public_dict(self) -> dict:
        summary = self.retrieval_summary.public_dict()
        data = {
            "strategy": summary["strategy"],
            "evidence_status": summary["evidence_status"],
            "citations": [item.public_dict() for item in self.citations],
            "retrieval_summary": summary,
        }
        if self.ok:
            data["result_code"] = (
                "SEARCH_NOT_NEEDED"
                if summary["evidence_status"] == "not_needed"
                else "KNOWLEDGE_FOUND"
            )
            return {"ok": True, "data": data}
        return {
            "ok": False,
            "error": {
                "code": self.error.code if self.error else "SEARCH_INTERNAL_ERROR",
                "message": (
                    self.error.safe_message
                    if self.error
                    else "knowledge search could not be completed"
                ),
            },
            "data": data,
        }
