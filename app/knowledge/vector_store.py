"""Vector store contract: points, candidates and the persistence boundary.

Tenant safety and payload minimalism are enforced here in the contract:
``VectorPoint`` carries only ids and an ordinal plus the embedding vector.
Document body text never lives in the vector-store payload — it stays in
SQLite and is joined back onto ``VectorCandidate`` ids at retrieval time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

from app.knowledge.embeddings import EmbeddingVector


@dataclass(frozen=True)
class VectorPoint:
    chunk_id: str
    organization_id: str
    document_id: str
    version_id: str
    ordinal: int
    embedding: EmbeddingVector


@dataclass(frozen=True)
class VectorCandidate:
    chunk_id: str
    document_id: str
    version_id: str
    ordinal: int
    score: float


class VectorStore(Protocol):
    """Persistence boundary for tenant-scoped vector points.

    ``organization_id`` and active version ids are always supplied by the
    caller so no cross-tenant lookup is possible; ``active_version_ids`` is
    part of the Qdrant filter so disabled/old versions are never retrieved.
    """

    def ensure_collection(self) -> None:
        raise NotImplementedError

    def upsert(self, *, points: Sequence[VectorPoint]) -> None:
        raise NotImplementedError

    def search(
        self,
        *,
        organization_id: str,
        active_version_ids: Sequence[str],
        query_embedding: EmbeddingVector,
        prefetch_limit: int,
        result_limit: int,
    ) -> list[VectorCandidate]:
        raise NotImplementedError
