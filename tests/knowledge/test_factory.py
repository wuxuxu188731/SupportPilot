"""Production assembly tests for the knowledge services factory.

Task 11: ``create_knowledge_services`` must wire real adapters
(SQLiteKnowledgeStore, KnowledgeIngestionService, BaselineKnowledgeSearchService)
and, when a qdrant client is injected, must NOT make any network call at
assembly time (no ``get_collections`` / ``ensure_collection``). Collection
initialisation is deferred to first ingestion / explicit ops check.
"""

from dataclasses import dataclass, field

import pytest

from app.core.config import KnowledgeSettings


@dataclass
class RecordingQdrantClient:
    """A qdrant-shaped fake that records every network call it makes.

    The real factory never touches the network at assembly time, so a freshly
    built factory must leave ``network_calls`` empty — proving that wiring
    Qdrant does not eagerly probe the server.
    """

    network_calls: list[str] = field(default_factory=list)

    # The subset of QdrantClient methods the factory could call. Each records
    # itself so a test can assert collection init is deferred (not done here).
    def get_collections(self):  # pragma: no cover - only called if factory is wrong
        self.network_calls.append("get_collections")
        return object()

    def create_collection(self, **kwargs):  # pragma: no cover
        self.network_calls.append("create_collection")

    def create_payload_index(self, **kwargs):  # pragma: no cover
        self.network_calls.append("create_payload_index")

    def upsert(self, **kwargs):  # pragma: no cover
        self.network_calls.append("upsert")

    def query_points(self, **kwargs):  # pragma: no cover
        self.network_calls.append("query_points")


@pytest.mark.parametrize(
    ("url", "expects_proxy_bypass"),
    [
        ("http://localhost:6333", True),
        ("http://127.0.0.1:6333", True),
        ("http://[::1]:6333", True),
        ("https://qdrant.example.com", False),
    ],
)
def test_factory_bypasses_environment_proxy_only_for_loopback_qdrant(
    tmp_path, monkeypatch, url, expects_proxy_bypass
):
    from app.knowledge import factory

    captured = {}

    def construct_qdrant(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(factory, "QdrantClient", construct_qdrant)
    settings = KnowledgeSettings(
        dashscope_api_key="test-key",
        dashscope_base_url="https://dashscope.test/api/v1",
        qdrant_url=url,
    )

    factory.create_knowledge_services(
        tmp_path / "app.db", settings, llm_client=object()
    )

    if expects_proxy_bypass:
        assert captured["trust_env"] is False
    else:
        assert "trust_env" not in captured


def test_factory_wires_real_adapters_without_network(tmp_path, monkeypatch):
    from app.knowledge.dashscope_embeddings import DashScopeEmbeddingClient
    from app.knowledge.factory import create_knowledge_services
    from app.knowledge.ingestion import KnowledgeIngestionService
    from app.knowledge.qdrant_store import QdrantVectorStore
    from app.knowledge.retrieval import BaselineKnowledgeSearchService
    from app.knowledge.sqlite_store import SQLiteKnowledgeStore

    settings = KnowledgeSettings(
        dashscope_api_key="test-key",
        dashscope_base_url="https://dashscope.test/api/v1",
        qdrant_url="http://qdrant.test:6333",
    )
    qdrant = RecordingQdrantClient()

    services = create_knowledge_services(
        tmp_path / "app.db", settings, qdrant_client=qdrant,
        llm_client=object(), model_name="test-model"
    )

    assert isinstance(services.store, SQLiteKnowledgeStore)
    assert isinstance(services.ingestion, KnowledgeIngestionService)
    assert isinstance(services.baseline, BaselineKnowledgeSearchService)
    from app.knowledge.service import AdaptiveKnowledgeSearchService
    from app.knowledge.retrieval import HybridRetriever
    assert isinstance(services.adaptive, AdaptiveKnowledgeSearchService)
    assert isinstance(services.retriever, HybridRetriever)

    # The injected dashscope client + qdrant vector store are real (not fakes),
    # yet assembly touched the network zero times.
    assert isinstance(services.ingestion._embedding, DashScopeEmbeddingClient)
    assert isinstance(services.ingestion._vector_store, QdrantVectorStore)
    assert services.vector_store is services.ingestion._vector_store
    assert qdrant.network_calls == []


def test_factory_without_injected_qdrant_never_probes_network(tmp_path, monkeypatch):
    """With no qdrant_client injected, the factory still only CONSTRUCTS the
    client; it must not call get_collections()/ensure_collection() (which would
    hit the network against the configured URL)."""
    from app.knowledge.factory import create_knowledge_services

    # qdrant.invalid is a reserved TLD that never resolves; if the factory tried
    # to probe it, constructing services would raise a connection error.
    settings = KnowledgeSettings(
        dashscope_api_key="test-key",
        dashscope_base_url="https://dashscope.test/api/v1",
        qdrant_url="http://qdrant.invalid:6333",
    )

    services = create_knowledge_services(
        tmp_path / "app.db", settings, llm_client=object()
    )

    # Construction succeeded without connecting: the vector store is wired but
    # collection init is deferred.
    from app.knowledge.qdrant_store import QdrantVectorStore

    assert isinstance(services.ingestion._vector_store, QdrantVectorStore)
