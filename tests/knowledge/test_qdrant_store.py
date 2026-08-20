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
from app.knowledge.vector_store import VectorPoint, VectorPointIdentity
from app.knowledge.qdrant_store import VectorConfigurationError

COLLECTION = "supportpilot_knowledge_te4_1024_v1"


class FakeQdrantClient:
    """Records create_collection / upsert / query_points / payload_index calls."""

    def __init__(self, *, points=None):
        self.points = points or {}
        self.create_calls = []
        self.upsert_calls = []
        self.query_calls = []
        self.retrieve_calls = []
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
        # Mirrors the REAL CollectionInfo shape (qdrant-client >= 1.18): the
        # vector config lives on CollectionInfo.config.params.vectors (dict
        # name -> VectorParams) and ConfigurationInfo.config.params.sparse_vectors.
        default = self._shape_info(
            {"dense": type("V", (), {"size": 1024})}, sparse={"sparse": None}
        )
        return self._collections_info.get(name, default)

    def upsert(self, **kwargs):
        self.upsert_calls.append(kwargs)

    def query_points(self, **kwargs):
        self.query_calls.append(kwargs)
        return type("QR", (), {"points": []})()

    def retrieve(self, **kwargs):
        self.retrieve_calls.append(kwargs)
        return [self.points[point_id] for point_id in kwargs["ids"] if point_id in self.points]

    # --- helpers for the mismatch tests ---

    @staticmethod
    def _shape_info(vectors, *, sparse):
        """Build a CollectionInfo in the REAL shape (config.params.*)."""
        config = type(
            "Config",
            (),
            {
                "params": type(
                    "Params",
                    (),
                    {"vectors": vectors, "sparse_vectors": sparse},
                )
            },
        )
        return type("C", (), {"config": config})


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
    wrong = FakeQdrantClient._shape_info(
        {"dense": type("V", (), {"size": 512})}, sparse={"sparse": None}
    )
    client._collections_info[COLLECTION] = wrong
    store = QdrantVectorStore(client=client, collection_name=COLLECTION)

    with pytest.raises(VectorConfigurationError):
        store.ensure_collection()


def test_ensure_collection_raises_when_lacking_dense_vector():
    client = FakeQdrantClient(points=[])
    client.collections.add(COLLECTION)
    missing_dense = FakeQdrantClient._shape_info({}, sparse={"sparse": None})
    client._collections_info[COLLECTION] = missing_dense
    store = QdrantVectorStore(client=client, collection_name=COLLECTION)

    with pytest.raises(VectorConfigurationError, match="lacks 'dense'"):
        store.ensure_collection()


def test_ensure_collection_raises_when_lacking_sparse_vector():
    client = FakeQdrantClient(points=[])
    client.collections.add(COLLECTION)
    missing_sparse = FakeQdrantClient._shape_info(
        {"dense": type("V", (), {"size": 1024})}, sparse={}
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
    from uuid import NAMESPACE_URL, uuid5

    client = FakeQdrantClient(points=[])
    store = QdrantVectorStore(client=client, collection_name=COLLECTION)

    def chunk_id(chunk: str) -> str:
        # Mirror the Task 4 deterministic uuid5 chunk-id scheme.
        return str(uuid5(NAMESPACE_URL, chunk))

    def point(chunk: str) -> VectorPoint:
        cid = chunk_id(chunk)
        return VectorPoint(
            chunk_id=cid,
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
    # The point id IS the deterministic uuid5 chunk_id (a real-Qdrant-valid
    # UUID), not a composite string.
    assert first_id == chunk_id("chunk-1")
    assert second_id == first_id

    # A different chunk maps to a distinct id.
    store.upsert(points=[point("chunk-2")])
    assert client.upsert_calls[2]["points"][0].id == chunk_id("chunk-2")
    assert client.upsert_calls[2]["points"][0].id != first_id


def identity() -> VectorPointIdentity:
    return VectorPointIdentity(
        chunk_id="chunk-1",
        organization_id="org-a",
        document_id="doc-1",
        version_id="version-1",
        ordinal=0,
    )


def record(point_identity: VectorPointIdentity, **payload_overrides):
    payload = {
        "organization_id": point_identity.organization_id,
        "document_id": point_identity.document_id,
        "version_id": point_identity.version_id,
        "chunk_id": point_identity.chunk_id,
        "ordinal": point_identity.ordinal,
    }
    payload.update(payload_overrides)
    return type("Record", (), {"id": point_identity.chunk_id, "payload": payload})()


def test_validate_point_identities_retrieves_payload_without_vectors():
    expected = identity()
    client = FakeQdrantClient(points={expected.chunk_id: record(expected)})
    store = QdrantVectorStore(client=client, collection_name=COLLECTION)

    store.validate_point_identities(expected=(expected,))

    assert client.retrieve_calls == [{
        "collection_name": COLLECTION,
        "ids": [expected.chunk_id],
        "with_payload": True,
        "with_vectors": False,
    }]


@pytest.mark.parametrize(
    "points",
    [
        {},
        {"chunk-1": record(identity(), organization_id="wrong-tenant")},
        {"chunk-1": record(identity(), version_id="wrong-version")},
        {"chunk-1": record(identity(), content="forbidden document body")},
    ],
)
def test_validate_point_identities_rejects_missing_or_wrong_payload(points):
    store = QdrantVectorStore(
        client=FakeQdrantClient(points=points), collection_name=COLLECTION
    )

    with pytest.raises(ValueError, match="vector point identity drift"):
        store.validate_point_identities(expected=(identity(),))


def test_validate_point_identities_converts_transport_errors_to_unavailable():
    from qdrant_client.http.exceptions import UnexpectedResponse

    class ExplodingRetrieveClient(FakeQdrantClient):
        def retrieve(self, **kwargs):
            raise UnexpectedResponse(503, "Unavailable", b"raw body", {})

    store = QdrantVectorStore(
        client=ExplodingRetrieveClient(), collection_name=COLLECTION
    )

    with pytest.raises(VectorStoreUnavailableError):
        store.validate_point_identities(expected=(identity(),))


def test_validate_point_identities_converts_response_handling_connection_failure():
    from qdrant_client.http.exceptions import ResponseHandlingException

    wrapped = ResponseHandlingException(ConnectionError("raw connection detail"))

    class ExplodingRetrieveClient(FakeQdrantClient):
        def retrieve(self, **kwargs):
            raise wrapped

    store = QdrantVectorStore(
        client=ExplodingRetrieveClient(), collection_name=COLLECTION
    )

    with pytest.raises(VectorStoreUnavailableError) as captured:
        store.validate_point_identities(expected=(identity(),))

    assert captured.value.__cause__ is wrapped


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


def test_search_passes_timeout_to_query_points():
    client = FakeQdrantClient(points=[])
    store = QdrantVectorStore(client=client, collection_name=COLLECTION)

    store.search(
        organization_id="org-a",
        active_version_ids=["version-1"],
        query_embedding=embedding_vector(),
        prefetch_limit=8,
        result_limit=8,
        timeout_seconds=3,
    )

    assert client.query_calls[0]["timeout"] == 3


@pytest.mark.parametrize("timeout_seconds", [0, -1, 1.5, True])
def test_search_rejects_non_positive_integer_timeout(timeout_seconds):
    client = FakeQdrantClient(points=[])
    store = QdrantVectorStore(client=client, collection_name=COLLECTION)

    with pytest.raises(ValueError):
        store.search(
            organization_id="org-a",
            active_version_ids=["version-1"],
            query_embedding=embedding_vector(),
            prefetch_limit=8,
            result_limit=8,
            timeout_seconds=timeout_seconds,
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
