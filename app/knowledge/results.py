"""Retrieval result objects and their public (HTTP/report) serialization.

``BaselineSearchResult`` deliberately separates the public surface (``ok``,
``citations``, ``retrieval_summary``, ``error``) from the internal
``selected_chunks`` (which carry raw body text). Only ``public_dict()`` is used
for HTTP/report output, so the full document body that backs a citation is
never broadcast beyond the structured citation ``content`` already chosen by
the retriever.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Sequence

from app.knowledge.base import ChunkWithDocumentTitle, KnowledgeError


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
