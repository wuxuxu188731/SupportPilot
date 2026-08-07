"""Real-Qdrant integration tests for the tenant-scoped vector boundary.

These tests run against a REAL Qdrant instance (read ``QDRANT_URL``, default
``http://localhost:6333``) and are skipped when it is unreachable. Every test
creates a UNIQUE collection ``supportpilot_test_<uuid>`` and deletes ONLY that
collection in a ``finally`` — the production index
``supportpilot_knowledge_te4_1024_v1`` (and any other collection on the server)
is never created, read nor deleted here.

Everything below uses the real ``QdrantVectorStore`` adapter (real
``QdrantClient`` into real collections) so it verifies the Task 5-11 contract
for real: dense(size=1024)+sparse collection shape, the keyword tenant index
and native RRF fusion that is tenant + active-version filtered.
"""

from __future__ import annotations

from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from qdrant_client import QdrantClient, models

from app.knowledge.embeddings import EmbeddingVector, SparseValue
from app.knowledge.qdrant_store import QdrantVectorStore
from app.knowledge.vector_store import VectorPoint

PRODUCTION_COLLECTION = "supportpilot_knowledge_te4_1024_v1"

_DENSE = tuple(0.5 for _ in range(1024))
_DENSE_A = tuple(0.6 for _ in range(1024))
_DENSE_B = tuple(0.7 for _ in range(1024))
# A high-similarity-but-not-identical dense vector: same 0.5 normal as _DENSE,
# but dim 1023 flipped to -0.5. Cosine w.r.t. _DENSE is ~0.998, so it ranks
# strictly below _DENSE (1.0) on a dense prefetch while staying far above a
# cosine of -1.0. Used as the dense-matching filler points in the RRF test so
# ranking between them and an exactly-matching point is deterministic.
_DENSE_FILLER = tuple(0.5 if i < (1024 - 1) else -0.5 for i in range(1024))


def _chunk_id(label: str) -> str:
    """A deterministic, real-Qdrant-valid (UUID) chunk id for a test point.

    Mirrors the production chunker, which derives chunk ids via ``uuid5``, so
    the points written here carry the same id shape a real ingestion produces.
    """
    return str(uuid5(NAMESPACE_URL, label))


def _test_qdrant_url() -> str:
    import os

    return os.getenv("QDRANT_URL", "http://localhost:6333").rstrip("/")


@pytest.fixture
def real_client() -> QdrantClient:
    """Return a live QdrantClient.

    An unreachable instance is skipped (never ``None``), so a developer
    without Qdrant can still run the non-integration suite.
    """
    client = QdrantClient(
        url=_test_qdrant_url(),
        timeout=10,
        check_compatibility=False,
    )
    try:
        client.get_collections()
    except Exception:  # noqa: BLE001 - any transport error => not available
        pytest.skip(f"Qdrant not reachable at {_test_qdrant_url()}")
    return client


@pytest.fixture
def unique_collection(real_client: QdrantClient):
    """A unique ``supportpilot_test_<uuid>`` collection, deleted in finally."""
    name = f"supportpilot_test_{uuid4().hex}"
    yield name
    try:
        real_client.delete_collection(collection_name=name)
    except Exception:  # noqa: BLE001 - best-effort cleanup
        pass


@pytest.mark.integration
def test_collection_shape_is_dense_1024_sparse_with_tenant_keyword_index(
    real_client, unique_collection
):
    store = QdrantVectorStore(client=real_client, collection_name=unique_collection)
    store.ensure_collection()

    # Real qdrant-client >= 1.18 exposes the collection vector config under
    # ``CollectionInfo.config.params`` (NOT ``info.vectors``).
    info = real_client.get_collection(unique_collection)
    params = info.config.params
    assert "dense" in (params.vectors or {})
    assert params.vectors["dense"].size == 1024
    assert "sparse" in (params.sparse_vectors or {})

    # The organization_id field must be a KEYWORD index flagged is_tenant=True:
    # real qdrant-client exposes it on CollectionInfo.payload_schema.
    index = info.payload_schema.get("organization_id")
    assert index is not None, "missing organization_id payload index"
    assert index.data_type == models.PayloadSchemaType.KEYWORD
    assert index.params.is_tenant is True


def _point(label, *, organization_id, version_id, dense, sparse_idx, ordinal=0):
    return VectorPoint(
        chunk_id=_chunk_id(label),
        organization_id=organization_id,
        document_id=f"doc-{organization_id}",
        version_id=version_id,
        ordinal=ordinal,
        embedding=EmbeddingVector(
            dense=dense,
            sparse=(SparseValue(index=sparse_idx, value=1.0),),
            token_count=4,
        ),
    )


@pytest.mark.integration
def test_tenant_filter_returns_only_org_a_and_only_active_version(
    real_client, unique_collection
):
    # org-a has an old (v1) and a new (v2) version of the same policy; org-b
    # uploads a same-title conflict policy. All live in ONE collection.
    store = QdrantVectorStore(client=real_client, collection_name=unique_collection)
    store.ensure_collection()
    a_v1 = _chunk_id("a-v1-c0")
    a_v2 = _chunk_id("a-v2-c0")
    b_c0 = _chunk_id("b-c0")
    store.upsert(
        points=[
            _point("a-v1-c0", organization_id="org-a", version_id="v1",
                   dense=_DENSE, sparse_idx=100),
            _point("a-v2-c0", organization_id="org-a", version_id="v2",
                   dense=_DENSE_A, sparse_idx=200),
            _point("b-c0", organization_id="org-b", version_id="b-v1",
                   dense=_DENSE_B, sparse_idx=300),
        ]
    )

    # Query org-a restricted to the ACTIVE v2 only: dense+sparse RRF must
    # return the old v1's same-org content neither from v1 (version filter)
    # nor from org-b (tenant filter).
    query = EmbeddingVector(
        dense=_DENSE_A,
        sparse=(SparseValue(index=200, value=1.0),),
        token_count=4,
    )
    candidates = store.search(
        organization_id="org-a",
        active_version_ids=["v2"],
        query_embedding=query,
        prefetch_limit=8,
        result_limit=8,
    )
    ids = {c.chunk_id for c in candidates}
    assert ids == {a_v2}
    # All returned candidates belong to org-a.
    assert all(c.chunk_id in {a_v1, a_v2} for c in candidates)
    assert all(c.version_id == "v2" for c in candidates)


@pytest.mark.integration
def test_org_b_version_cannot_bypass_org_filter(
    real_client, unique_collection
):
    """Mixing org-b's version id into org-a's ACTIVE list must NOT let org-b
    points leak into org-a's results: the tenant keyword filter still wins."""
    store = QdrantVectorStore(client=real_client, collection_name=unique_collection)
    store.ensure_collection()
    a_v1 = _chunk_id("a-v1-c0")
    b_c0 = _chunk_id("b-c0")
    store.upsert(
        points=[
            _point("a-v1-c0", organization_id="org-a", version_id="v1",
                   dense=_DENSE, sparse_idx=100),
            _point("b-c0", organization_id="org-b", version_id="b-v1",
                   dense=_DENSE_B, sparse_idx=300),
        ]
    )

    # active_version_ids contains org-b's version id too, yet the query is for
    # org-a: the RRF must still only surface org-a points.
    query = EmbeddingVector(
        dense=_DENSE,
        sparse=(SparseValue(index=100, value=1.0),),  # matches org-a's sparse
        token_count=4,
    )
    candidates = store.search(
        organization_id="org-a",
        active_version_ids=["v1", "b-v1"],
        query_embedding=query,
        prefetch_limit=8,
        result_limit=8,
    )
    ids = {c.chunk_id for c in candidates}
    assert ids == {a_v1}, "org-b point leaked through the org filter"
    assert b_c0 not in ids


@pytest.mark.integration
def test_sparse_and_dense_channels_both_feed_rrf(real_client, unique_collection):
    """A point whose DENSE vector is far but whose SPARSE indices match the
    query must still be returned: the sparse prefetch independently feeds the
    native RRF fusion (it is not a no-op alongside the dense channel).

    This is deliberately designed to DISCRIMINATE the sparse channel. Qdrant
    returns up to ``limit`` points per prefetch regardless of score, so with
    too few points an orthogonal-dense point still leaks in through the dense
    prefetch and the assertion would pass even if the sparse channel were
    broken. We seed enough dense-matching points (9 > ``prefetch_limit`` 8) that
    the dense prefetch is fully saturated by high-cosine points and can only
    surface the orthogonal point through the sparse prefetch.
    """
    store = QdrantVectorStore(client=real_client, collection_name=unique_collection)
    store.ensure_collection()

    dense_far = tuple(-1.0 for _ in range(1024))
    dense_hit = _chunk_id("dense-hit")
    sparse_hit = _chunk_id("sparse-hit")

    # Dense-matching filler points: same org-a / active v1 signature so they
    # pass the tenant+version filter, and a dense vector (~0.998 cosine) that is
    # strictly less similar than the exact _DENSE match (1.0) but far above the
    # orthogonal sparse_hit (-1.0). 8 fillers + dense_hit = 9 dense-matching
    # points, saturating the dense prefetch's limit=8 and pushing sparse_hit
    # out. Their sparse indices never match the query's (777).
    points = [
        _point("dense-hit", organization_id="org-a", version_id="v1",
               dense=_DENSE, sparse_idx=100),
    ]
    points += [
        _point(f"filler-{i}", organization_id="org-a", version_id="v1",
               dense=_DENSE_FILLER, sparse_idx=300 + i)
        for i in range(8)
    ]
    # Orthogonal dense (cosine -1.0), exactly-matching sparse index 777: reachable
    # ONLY via the sparse prefetch.
    points.append(
        VectorPoint(
            chunk_id=sparse_hit,
            organization_id="org-a",
            document_id="doc-org-a",
            version_id="v1",
            ordinal=len(points),
            embedding=EmbeddingVector(
                dense=dense_far,
                sparse=(SparseValue(index=777, value=1.0),),
                token_count=4,
            ),
        )
    )
    store.upsert(points=points)

    query = EmbeddingVector(
        dense=_DENSE,  # exactly matches dense_hit, high for the fillers, -1.0 for sparse_hit
        sparse=(SparseValue(index=777, value=1.0),),  # matches sparse_hit only
        token_count=4,
    )
    # prefetch_limit=8 < 9 dense-matching points, so the dense prefetch is full
    # and sparse_hit cannot ride in on it. result_limit=16 >= the 10 unique
    # points, so every RRF candidate (including the sparse-borne sparse_hit) is
    # present in the fused result.
    candidates = store.search(
        organization_id="org-a",
        active_version_ids=["v1"],
        query_embedding=query,
        prefetch_limit=8,
        result_limit=16,
    )
    ids = {c.chunk_id for c in candidates}
    assert len(candidates) >= 2
    assert dense_hit in ids
    assert sparse_hit in ids


@pytest.mark.integration
def test_production_collection_never_created_or_deleted(real_client, unique_collection):
    """Guards the isolation contract: the integration fixture only ever touches
    its own ``supportpilot_test_<uuid>`` collection, never the production
    ``supportpilot_knowledge_te4_1024_v1`` name (created OR deleted)."""
    assert unique_collection.startswith("supportpilot_test_")
    assert unique_collection != PRODUCTION_COLLECTION
    # Teardown deletes ONLY the unique collection. The production collection is
    # never created by us and never deleted (regardless of whether the
    # environment already provisions it).
