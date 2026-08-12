"""Production assembly of the knowledge RAG services.

``create_knowledge_services`` wires real adapters — SQLite persistence, the
DashScope embedding client, the Qdrant vector store and the Baseline retriever —
into the three services the application consumes (store, ingestion, baseline).

Collection initialisation is deliberately deferred: constructing the Qdrant
client (and wrapping it in :class:`QdrantVectorStore`) performs NO network I/O.
``get_collections()`` / ``ensure_collection()`` only happen lazily on the first
ingestion or an explicit ops check, so importing the application never depends
on Qdrant or DashScope being reachable.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from qdrant_client import QdrantClient

from app.core.config import KnowledgeSettings
from app.knowledge.chunking import KnowledgeChunker
from app.knowledge.dashscope_embeddings import DashScopeEmbeddingClient
from app.knowledge.document_loader import DocumentLoader
from app.knowledge.ingestion import KnowledgeIngestionService
from app.knowledge.qdrant_store import QdrantVectorStore
from app.knowledge.retrieval import BaselineKnowledgeSearchService, HybridRetriever
from app.knowledge.service import AdaptiveKnowledgeSearchService
from app.knowledge.structured_llm import OpenAIStructuredJSONClient
from app.knowledge.planning import QueryPlanner
from app.knowledge.evidence import EvidenceAssessor
from app.knowledge.sqlite_store import SQLiteKnowledgeStore

# Guard against an accidental eager probe. Keep in sync with the client
# intended for real Qdrant access; used to make any unintended connection
# impossible vs. the configured URL at assembly time.
_CONNECT_TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class KnowledgeServices:
    """The fully-wired knowledge services, ready for the application root."""

    store: SQLiteKnowledgeStore
    ingestion: KnowledgeIngestionService
    baseline: BaselineKnowledgeSearchService
    retriever: HybridRetriever
    adaptive: AdaptiveKnowledgeSearchService


def create_knowledge_services(
    database_path: str | Path,
    settings: KnowledgeSettings,
    *,
    qdrant_client: QdrantClient | None = None,
    llm_client=None,
    model_name: str = "deepseek-v4-flash",
) -> KnowledgeServices:
    """Build the production knowledge service graph.

    ``database_path`` may be a path that does not yet exist; the SQLite store
    migrates it into place.

    When ``qdrant_client`` is injected (tests / smoke runners) it is used
    verbatim and never probed. When it is omitted, only ``QdrantClient(...)``
    is constructed — no collections are created or listed, deferring all
    network work to the first ingestion / explicit ops check.
    """
    store = SQLiteKnowledgeStore(database_path=database_path)

    loader = DocumentLoader()
    chunker = KnowledgeChunker()

    embedding = DashScopeEmbeddingClient(
        api_key=settings.dashscope_api_key,
        # Wire the configured DASHSCOPE_BASE_URL through to the SDK's
        # process-global base_http_api_url so an override actually takes effect
        # (see I3); a default value leaves the SDK's own default untouched.
        base_url=settings.dashscope_base_url,
    )

    if qdrant_client is None:
        # Construction only. Deliberately no get_collections()/ensure_collection().
        # check_compatibility=False suppresses QdrantClient's eager in-constructor
        # server-version probe, so assembly performs zero network I/O against the
        # configured URL.
        qdrant_client = QdrantClient(
            url=settings.qdrant_url,
            timeout=_CONNECT_TIMEOUT_SECONDS,
            check_compatibility=False,
        )
    vector_store = QdrantVectorStore(
        client=qdrant_client,
        collection_name=settings.qdrant_collection,
    )

    ingestion = KnowledgeIngestionService(
        store=store,
        loader=loader,
        chunker=chunker,
        embedding=embedding,
        vector_store=vector_store,
        embedding_model=settings.embedding_model,
        embedding_dimensions=settings.embedding_dimensions,
    )

    retriever = HybridRetriever(
        store=store,
        embedding=embedding,
        vector_store=vector_store,
    )
    baseline = BaselineKnowledgeSearchService(
        store=store,
        embedding=embedding,
        vector_store=vector_store,
        retriever=retriever,
    )
    if llm_client is None:
        from app.core.config import create_llm_client
        llm_client = create_llm_client()
    structured = OpenAIStructuredJSONClient(
        llm_client, model_name=model_name
    )
    adaptive = AdaptiveKnowledgeSearchService(
        store=store,
        retriever=retriever,
        planner=QueryPlanner(structured),
        assessor=EvidenceAssessor(
            client=structured,
            min_fused_score=settings.min_fused_score,
        ),
        timeout_seconds=settings.search_timeout_seconds,
    )

    return KnowledgeServices(
        store=store,
        ingestion=ingestion,
        baseline=baseline,
        retriever=retriever,
        adaptive=adaptive,
    )
