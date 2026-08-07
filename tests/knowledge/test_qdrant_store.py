"""Qdrant collection/point/query mapping tests for QdrantVectorStore.

Uses a fake ``QdrantClient`` so every assertion runs with zero network I/O.
The fake records the exact request structure the store builds, letting us
assert the tenant/version filter is applied identically to both prefetches
and that RRF is delegated to Qdrant (native, not in Python).
"""

import pytest

from qdrant_client import models

from app.knowledge.base import VectorStoreUnavailableError
from app.knowledge.embeddings import EmbeddingVector, SparseValue
from app.knowledge.qdrant_store import QdrantVectorStore
from app.knowledge.vector_store import VectorPoint
from app.knowledge.qdrant_store import VectorConfigurationError

COLLECTION = "supportpilot_knowledge_te4_1024_v1"


class FakeQdrantClient:
    """Records create_collection / upsert / query_points / payload_index calls."""

    def __init__(self, *, points=None):
        self.points = points or {}
        self.create_calls = []
        self.upsert_calls = []
        self.query_calls = []
        self.payload_index_calls = []
        self.collections = set()
        self._collections_info = {}

    # --- QdrantClient surface the store depends on ---

    def get_collections(self):
        class _Collections:
            def __init__(self, names):
                self.collections = [type("C", (), {"name": n})() for n in names]

        return _Collections(self.collections)

    def create_collection(self, **kwargs):
        self.create_calls.append(kwargs)
        name = kwargs["collection_name"]
        self.collections.add(name)

    def create_payload_index(self, **kwargs):
        self.payload_index_calls.append(kwargs)

    def get_collection(self, name):
        default = type(
            "C",
            (),
            {
                "vectors": {
                    "dense": type("V", (), {"size": 1024}),
                },
                "sparse_vectors": {"sparse": None},
            },
        )
        return self._collections_info.get(name, default)

    def upsert(self, **kwargs):
        self.upsert_calls.append(kwargs)

    def query_points(self, **kwargs):
        self.query_calls.append(kwargs)
        return type("QR", (), {"points": []})()

    # --- helpers for the mismatch tests ---

    def _set_info(self, name, vectors, dense_size):
        self._collections_info[name] = {
            "vectors": vectors,
            "dense_size": dense_size,
        }


def embedding_vector():
    return EmbeddingVector(
        dense=(0.1,) * 1024,
        sparse=(SparseValue(index=7, value=0.9), SparseValue(index=99, value=0.4)),
        token_count=4,
    )


def test_ensure_collection_creates_tenant_indexed_named_vectors():
    client = FakeQdrantClient(points=[])
    store = QdrantVectorStore(client=client, collection_name=COLLECTION)

    store.ensure_collection()

    create_call = client.create_calls[0]
    assert create_call["collection_name"] == COLLECTION
    assert create_call["vectors_config"]["dense"].size == 1024
    assert "sparse" in create_call["sparse_vectors_config"]
    payload_index_call = client.payload_index_calls[0]
    assert payload_index_call["field_name"] == "organization_id"
    assert payload_index_call["field_schema"].is_tenant is True


def test_ensure_collection_is_idempotent_when_collection_exists():
    client = FakeQdrantClient(points=[])
    client.collections.add(COLLECTION)
    store = QdrantVectorStore(client=client, collection_name=COLLECTION)

    store.ensure_collection()

    assert client.create_calls == []
    assert client.payload_index_calls == []


def test_ensure_collection_raises_on_dense_size_mismatch():
    client = FakeQdrantClient(points=[])
    client.collections.add(COLLECTION)
    wrong = type("C", (), {"vectors": {"dense": type("V", (), {"size": 512})}, "sparse_vectors": {"sparse": None}})
    client._collections_info[COLLECTION] = wrong
    store = QdrantVectorStore(client=client, collection_name=COLLECTION)

    with pytest.raises(VectorConfigurationError):
        store.ensure_collection()


def test_ensure_collection_raises_when_lacking_dense_vector():
    client = FakeQdrantClient(points=[])
    client.collections.add(COLLECTION)
    missing_dense = type(
        "C",
        (),
        {
            "vectors": {"sparse": None},
            "sparse_vectors": {"sparse": None},
        },
    )
    client._collections_info[COLLECTION] = missing_dense
    store = QdrantVectorStore(client=client, collection_name=COLLECTION)

    with pytest.raises(VectorConfigurationError, match="lacks 'dense'"):
        store.ensure_collection()


def test_ensure_collection_raises_when_lacking_sparse_vector():
    client = FakeQdrantClient(points=[])
    client.collections.add(COLLECTION)
    missing_sparse = type(
        "C",
        (),
        {
            "vectors": {"dense": type("V", (), {"size": 1024})},
            "sparse_vectors": {},
        },
    )
    client._collections_info[COLLECTION] = missing_sparse
    store = QdrantVectorStore(client=client, collection_name=COLLECTION)

    with pytest.raises(VectorConfigurationError, match="lacks 'sparse'"):
        store.ensure_collection()


def test_ensure_collection_converts_transport_errors_to_unavailable():
    from qdrant_client.http.exceptions import UnexpectedResponse

    class ExplodingCreateClient(FakeQdrantClient):
        def create_collection(self, **kwargs):
            raise UnexpectedResponse(500, "Internal Server Error", b"boom", {})

    client = ExplodingCreateClient(points=[])
    store = QdrantVectorStore(client=client, collection_name=COLLECTION)

    with pytest.raises(VectorStoreUnavailableError):
        store.ensure_collection()


def both_filters_equal(
    prefetch,
    *,
    organization_id: str,
    active_version_ids: list[str],
) -> bool:
    """Assert every prefetch carries the same tenant + versions filter."""
    filters = [item.filter for item in prefetch]
    assert all(f is not None for f in filters)
    expected_must_conditions = {
        "organization_id": organization_id,
        "version_id": active_version_ids,
    }
    observed = []
    for f in filters:
        if isinstance(f.must, list):
            conditions = {i.key: i for i in f.must}
        else:
            conditions = {}
        org = conditions.get("organization_id")
        ver = conditions.get("version_id")
        org_ok = (
            org is not None
            and isinstance(org.match, models.MatchValue)
            and org.match.value == organization_id
        )
        ver_ok = (
            ver is not None
            and isinstance(ver.match, models.MatchAny)
            and ver.match.any == active_version_ids
        )
        observed.append(org_ok and ver_ok)
    assert observed, "no prefetch carried both tenant and version conditions"
    return all(observed)


def test_upsert_point_payload_excludes_document_body():
    client = FakeQdrantClient(points=[])
    store = QdrantVectorStore(client=client, collection_name=COLLECTION)

    store.upsert(
        points=[
            VectorPoint(
                chunk_id="chunk-1",
                organization_id="org-a",
                document_id="doc-1",
                version_id="version-1",
                ordinal=0,
                embedding=embedding_vector(),
            )
        ]
    )

    assert len(client.upsert_calls) == 1
    payload = client.upsert_calls[0]["points"][0].payload
    assert set(payload) == {
        "organization_id",
        "document_id",
        "version_id",
        "chunk_id",
        "ordinal",
    }
    assert payload["organization_id"] == "org-a"
    assert payload["document_id"] == "doc-1"
    assert payload["version_id"] == "version-1"
    assert payload["chunk_id"] == "chunk-1"
    assert payload["ordinal"] == 0


def test_upsert_point_id_is_deterministic_per_org_version_chunk():
    client = FakeQdrantClient(points=[])
    store = QdrantVectorStore(client=client, collection_name=COLLECTION)

    def point(chunk_id: str) -> VectorPoint:
        return VectorPoint(
            chunk_id=chunk_id,
            organization_id="org-a",
            document_id="doc-1",
            version_id="version-1",
            ordinal=0,
            embedding=embedding_vector(),
        )

    # Re-upserting the same chunk yields an identical point id, so Qdrant
    # replaces the existing point rather than appending a duplicate.
    store.upsert(points=[point("chunk-1")])
    store.upsert(points=[point("chunk-1")])

    first_id = client.upsert_calls[0]["points"][0].id
    second_id = client.upsert_calls[1]["points"][0].id
    assert first_id == "org-a:version-1:chunk-1"
    assert second_id == first_id

    # A different chunk maps to a distinct id.
    store.upsert(points=[point("chunk-2")])
    assert client.upsert_calls[2]["points"][0].id == "org-a:version-1:chunk-2"
    assert client.upsert_calls[2]["points"][0].id != first_id


def test_search_filters_both_prefetches_by_tenant_and_active_versions():
    client = FakeQdrantClient(points=[])
    store = QdrantVectorStore(client=client, collection_name=COLLECTION)

    store.search(
        organization_id="org-a",
        active_version_ids=["version-1", "version-2"],
        query_embedding=embedding_vector(),
        prefetch_limit=8,
        result_limit=8,
    )

    request = client.query_calls[0]
    assert len(request["prefetch"]) == 2
    assert {item.using for item in request["prefetch"]} == {
        "dense",
        "sparse",
    }
    assert all(item.limit == 8 for item in request["prefetch"])
    assert request["query"].fusion == models.Fusion.RRF
    assert request["limit"] == 8
    assert both_filters_equal(
        request["prefetch"],
        organization_id="org-a",
        active_version_ids=["version-1", "version-2"],
    )


def test_search_returns_empty_without_calling_qdrant_for_no_active_versions():
    client = FakeQdrantClient(points=[])
    store = QdrantVectorStore(client=client, collection_name=COLLECTION)

    result = store.search(
        organization_id="org-a",
        active_version_ids=[],
        query_embedding=embedding_vector(),
        prefetch_limit=8,
        result_limit=8,
    )

    assert result == []
    assert client.query_calls == []


def test_query_points_unexpected_response_raises_vector_store_unavailable():
    from qdrant_client.http.exceptions import UnexpectedResponse

    class ExplodingClient(FakeQdrantClient):
        def query_points(self, **kwargs):
            raise UnexpectedResponse(500, "Internal Server Error", b"boom", {})

    client = ExplodingClient(points=[])
    store = QdrantVectorStore(client=client, collection_name=COLLECTION)

    with pytest.raises(VectorStoreUnavailableError):
        store.search(
            organization_id="org-a",
            active_version_ids=["version-1"],
            query_embedding=embedding_vector(),
            prefetch_limit=8,
            result_limit=8,
        )
