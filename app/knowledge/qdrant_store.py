"""Qdrant adapter for the tenant-scoped vector store.

Implements :class:`VectorStore` against a real ``QdrantClient``. Collection
naming follows the fixed contract (``supportpilot_knowledge_te4_1024_v1``)
with named vectors ``dense`` (1024-dim COSINE) and ``sparse``, plus a keyword
index on ``organization_id`` flagged ``is_tenant=True``.

Hybrid retrieval is fully delegated to Qdrant: two prefetches (dense and
sparse) share one tenant + active-versions filter and Qdrant fuses them with
native RRF (``FusionQuery(Fusion.RRF)``). The caller can inject a fake client
so the exact request structure is testable without a network.
"""

from __future__ import annotations

from threading import Lock
from typing import Sequence

from qdrant_client import QdrantClient
from qdrant_client import models
from qdrant_client.http.exceptions import UnexpectedResponse

from app.knowledge.base import VectorStoreUnavailableError
from app.knowledge.embeddings import EmbeddingVector
from app.knowledge.vector_store import (
    VectorCandidate,
    VectorPoint,
    VectorPointIdentity,
)

DENSE_VECTOR_NAME = "dense"
SPARSE_VECTOR_NAME = "sparse"
DENSE_SIZE = 1024

# Qdrant payload field names; only these five metadata fields ever reach the
# index. Document body text is never among them — it stays in SQLite and is
# re-joined at retrieval time.
_ORGANIZATION_FIELD = "organization_id"
_VERSION_FIELD = "version_id"
_CHUNK_ID_FIELD = "chunk_id"
_DOCUMENT_ID_FIELD = "document_id"
_ORDINAL_FIELD = "ordinal"


class VectorConfigurationError(Exception):
    """Raised when an existing collection does not match the expected shape.

    A real (or already-created) collection that lacks the named vectors or has
    the wrong dense dimension is a contract violation: we raise rather than
    silently mixing configs in place.
    """


class QdrantVectorStore:
    """Qdrant-backed :class:`VectorStore`.

    ``client`` may be a real :class:`QdrantClient` or any fake exposing the
    subset of methods used here (``get_collections``, ``create_collection``,
    ``create_payload_index``, ``upsert``, ``query_points``).
    """

    def __init__(
        self,
        *,
        client: QdrantClient,
        collection_name: str,
        dense_size: int = DENSE_SIZE,
    ) -> None:
        self._client = client
        self._collection_name = collection_name
        self._dense_size = dense_size
        self._lock = Lock()

    # ------------------------------------------------------------------ #
    # Collection
    # ------------------------------------------------------------------ #

    def ensure_collection(self) -> None:
        """Create the collection + tenant keyword index once, idempotently.

        If the collection already exists, its shape is validated against the
        expected named vectors and dense size; a mismatch raises
        :class:`VectorConfigurationError` instead of mutating in place.
        """
        with self._lock:
            exists = self._collection_exists()
            if exists:
                # Fully idempotent: shape-validate but never rewrite in place.
                self._verify_collection_shape()
                return

            try:
                self._client.create_collection(
                    collection_name=self._collection_name,
                    vectors_config={
                        DENSE_VECTOR_NAME: models.VectorParams(
                            size=self._dense_size,
                            distance=models.Distance.COSINE,
                        )
                    },
                    sparse_vectors_config={
                        SPARSE_VECTOR_NAME: models.SparseVectorParams(
                            index=models.SparseIndexParams(on_disk=False)
                        )
                    },
                )
                self._ensure_organization_index()
            except (UnexpectedResponse, ConnectionError, TimeoutError) as exc:
                raise VectorStoreUnavailableError(
                    reason=f"create collection: {type(exc).__name__}: {exc}"
                ) from exc

    def _collection_exists(self) -> bool:
        names = {c.name for c in self._client.get_collections().collections}
        return self._collection_name in names

    def _verify_collection_shape(self) -> None:
        # Real qdrant-client (>= 1.18) exposes the vector config on
        # ``CollectionInfo.config.params``: named dense vectors at
        # ``params.vectors`` (dict name -> VectorParams) and named sparse at
        # ``params.sparse_vectors``. Older/mis-build fakes that keep it at
        # ``info.vectors`` are not the shape we validate against.
        try:
            info = self._client.get_collection(self._collection_name)
        except UnexpectedResponse as exc:
            raise VectorStoreUnavailableError(
                reason=f"inspect collection: {exc}"
            ) from exc
        params = getattr(info, "config", None)
        params = getattr(params, "params", None)
        vectors = getattr(params, "vectors", None) or {}
        if DENSE_VECTOR_NAME not in vectors:
            raise VectorConfigurationError(
                f"collection {self._collection_name} lacks {DENSE_VECTOR_NAME!r} vector"
            )
        size = getattr(vectors[DENSE_VECTOR_NAME], "size", None)
        if size != self._dense_size:
            raise VectorConfigurationError(
                f"collection {self._collection_name} dense size {size} != {self._dense_size}"
            )
        sparse = getattr(params, "sparse_vectors", None) or {}
        if SPARSE_VECTOR_NAME not in sparse:
            raise VectorConfigurationError(
                f"collection {self._collection_name} lacks {SPARSE_VECTOR_NAME!r} vector"
            )

    def _ensure_organization_index(self) -> None:
        self._client.create_payload_index(
            collection_name=self._collection_name,
            field_name=_ORGANIZATION_FIELD,
            field_schema=models.KeywordIndexParams(
                type=models.KeywordIndexType.KEYWORD,
                is_tenant=True,
            ),
        )

    # ------------------------------------------------------------------ #
    # Upsert
    # ------------------------------------------------------------------ #

    def upsert(self, *, points: Sequence[VectorPoint]) -> None:
        if not points:
            return
        # Only tenant/point metadata is written to the payload. Body text never
        # enters Qdrant; it is re-joined from SQLite at search time.
        qdrant_points = [
            models.PointStruct(
                id=self._point_id(point),
                vector={
                    DENSE_VECTOR_NAME: point.embedding.dense,
                    SPARSE_VECTOR_NAME: self._sparse_vector(point.embedding),
                },
                payload={
                    _ORGANIZATION_FIELD: point.organization_id,
                    _DOCUMENT_ID_FIELD: point.document_id,
                    _VERSION_FIELD: point.version_id,
                    _CHUNK_ID_FIELD: point.chunk_id,
                    _ORDINAL_FIELD: point.ordinal,
                },
            )
            for point in points
        ]
        try:
            self._client.upsert(
                collection_name=self._collection_name,
                points=qdrant_points,
            )
        except (UnexpectedResponse, ConnectionError, TimeoutError) as exc:
            raise VectorStoreUnavailableError(
                reason=f"upsert: {type(exc).__name__}: {exc}"
            ) from exc

    def validate_point_identities(
        self, *, expected: Sequence[VectorPointIdentity]
    ) -> None:
        """Verify exact content-free metadata for a known set of point ids."""

        expected = tuple(expected)
        expected_by_id = {item.chunk_id: item for item in expected}
        if len(expected_by_id) != len(expected):
            raise ValueError("vector point identity drift")
        if not expected:
            return
        try:
            records = self._client.retrieve(
                collection_name=self._collection_name,
                ids=list(expected_by_id),
                with_payload=True,
                with_vectors=False,
            )
        except (UnexpectedResponse, ConnectionError, TimeoutError) as exc:
            raise VectorStoreUnavailableError(
                reason=f"retrieve identities: {type(exc).__name__}"
            ) from exc

        actual_by_id: dict[str, VectorPointIdentity] = {}
        for record in records:
            payload = record.payload if isinstance(record.payload, dict) else {}
            chunk_id = payload.get(_CHUNK_ID_FIELD)
            organization_id = payload.get(_ORGANIZATION_FIELD)
            document_id = payload.get(_DOCUMENT_ID_FIELD)
            version_id = payload.get(_VERSION_FIELD)
            ordinal = payload.get(_ORDINAL_FIELD)
            if (
                not isinstance(chunk_id, str)
                or not isinstance(organization_id, str)
                or not isinstance(document_id, str)
                or not isinstance(version_id, str)
                or isinstance(ordinal, bool)
                or not isinstance(ordinal, int)
                or str(record.id) != chunk_id
                or chunk_id in actual_by_id
            ):
                raise ValueError("vector point identity drift")
            actual_by_id[chunk_id] = VectorPointIdentity(
                chunk_id=chunk_id,
                organization_id=organization_id,
                document_id=document_id,
                version_id=version_id,
                ordinal=ordinal,
            )
        if actual_by_id != expected_by_id:
            raise ValueError("vector point identity drift")

    # ------------------------------------------------------------------ #
    # Hybrid search with native RRF
    # ------------------------------------------------------------------ #

    def search(
        self,
        *,
        organization_id: str,
        active_version_ids: Sequence[str],
        query_embedding: EmbeddingVector,
        prefetch_limit: int,
        result_limit: int,
        timeout_seconds: int = 5,
    ) -> list[VectorCandidate]:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be a positive integer")
        versions = list(active_version_ids)
        if not versions:
            return []

        tenant_filter = self._tenant_and_versions_filter(organization_id, versions)
        prefetch = [
            models.Prefetch(
                query=list(query_embedding.dense),
                using=DENSE_VECTOR_NAME,
                filter=tenant_filter,
                limit=prefetch_limit,
            ),
            models.Prefetch(
                query=models.SparseVector(
                    indices=[item.index for item in query_embedding.sparse],
                    values=[item.value for item in query_embedding.sparse],
                ),
                using=SPARSE_VECTOR_NAME,
                filter=tenant_filter,
                limit=prefetch_limit,
            ),
        ]
        try:
            response = self._client.query_points(
                collection_name=self._collection_name,
                prefetch=prefetch,
                query=models.FusionQuery(fusion=models.Fusion.RRF),
                limit=result_limit,
                with_payload=True,
                with_vectors=False,
                timeout=timeout_seconds,
            )
        except (UnexpectedResponse, ConnectionError, TimeoutError) as exc:
            raise VectorStoreUnavailableError(
                reason=f"search: {type(exc).__name__}: {exc}"
            ) from exc

        return [
            VectorCandidate(
                chunk_id=str(payload[_CHUNK_ID_FIELD]),
                document_id=str(payload[_DOCUMENT_ID_FIELD]),
                version_id=str(payload[_VERSION_FIELD]),
                ordinal=int(payload[_ORDINAL_FIELD]),
                score=float(getattr(point, "score", 0.0)),
            )
            for point in response.points
            for payload in [point.payload or {}]
        ]

    def _tenant_and_versions_filter(
        self, organization_id: str, versions: list[str]
    ) -> models.Filter:
        return models.Filter(
            must=[
                models.FieldCondition(
                    key=_ORGANIZATION_FIELD,
                    match=models.MatchValue(value=organization_id),
                ),
                models.FieldCondition(
                    key=_VERSION_FIELD,
                    match=models.MatchAny(any=versions),
                ),
            ]
        )

    @staticmethod
    def _sparse_vector(embedding: EmbeddingVector) -> models.SparseVector:
        return models.SparseVector(
            indices=[item.index for item in embedding.sparse],
            values=[item.value for item in embedding.sparse],
        )

    @staticmethod
    def _point_id(point: VectorPoint) -> str:
        # The chunk_id is already a deterministic uuid5 (Task 4 chunker), so it
        # doubles as the Qdrant point id. Real Qdrant only accepts an unsigned
        # integer or a UUID as a point id; re-upserting the same chunk reuses the
        # same UUID, so overwrite-idempotency is preserved without a lossy
        # composite string.
        return point.chunk_id
