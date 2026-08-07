"""Synchronous knowledge-ingestion happy-path orchestration tests.

Uses recording fakes over every dependency boundary (store, loader, chunker,
embedding, vector store) so the service is exercised with no network. The
strict event order is asserted against a single shared ``events`` list, and the
Qdrant ``upsert`` fake asserts the document is not yet active while points are
written (activation only happens after the vector write is committed).
"""

from uuid import NAMESPACE_URL, uuid5

import pytest

from app.knowledge.base import (
    DocumentChunk,
    DocumentNotFoundError,
    DocumentSourceType,
    DocumentStatus,
    DocumentVersion,
    EmbeddingUnavailableError,
    IngestionJob,
    IngestionStatus,
    InvalidDocumentError,
    KnowledgeDocument,
)
from app.knowledge.chunking import CHUNKER_VERSION
from app.knowledge.document_loader import LOADER_VERSION
from app.knowledge.embeddings import EmbeddingVector, SparseValue, count_tokens
from app.knowledge.ingestion import IngestionReceipt, KnowledgeIngestionService
from app.knowledge.vector_store import VectorPoint

DEFAULT_EMBEDDING_DIMENSIONS = 1024


def _stable_chunk_id(organization_id: str, version_id: str, ordinal: int) -> str:
    """The Task 4 deterministic chunk id scheme (uuid5 over org:version:ordinal)."""
    return str(
        uuid5(NAMESPACE_URL, f"{organization_id}:{version_id}:{ordinal}")
    )


# ------------------------------------------------------------------ fakes


class RecordingKnowledgeStore:
    """Minimal store fake that records each top-level call name in order and
    keeps just enough state for the service and the upsert fake to reason about
    the document's active state."""

    def __init__(self, events):
        self.events = events
        self.documents: dict[str, KnowledgeDocument] = {}
        self._version_counter = 0

    def _new_version_id(self) -> str:
        self._version_counter += 1
        return f"version-{self._version_counter}"

    def _active(self, document_id: str) -> bool:
        doc = self.documents[document_id]
        return doc.status is DocumentStatus.ACTIVE and doc.active_version_id is not None

    # document ------------------------------------------------------------
    def create_document(
        self,
        *,
        organization_id: str,
        uploaded_by_user_id: str,
        title: str,
        source_type: DocumentSourceType,
    ) -> KnowledgeDocument:
        self.events.append("create_document")
        document_id = f"doc-{len(self.documents) + 1}"
        doc = KnowledgeDocument(
            document_id=document_id,
            organization_id=organization_id,
            uploaded_by_user_id=uploaded_by_user_id,
            title=title.strip(),
            source_type=source_type,
            status=DocumentStatus.PROCESSING,
            active_version_id=None,
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )
        self.documents[document_id] = doc
        return doc

    def get_document(
        self,
        *,
        organization_id: str,
        document_id: str,
    ) -> KnowledgeDocument:
        doc = self.documents.get(document_id)
        if doc is None or doc.organization_id != organization_id:
            raise DocumentNotFoundError(organization_id, document_id)
        return doc

    # version -------------------------------------------------------------
    def create_version(
        self,
        *,
        organization_id: str,
        document_id: str,
        content_hash: str,
        raw_text: str,
        loader_version: str,
        chunker_version: str,
        embedding_model: str,
        embedding_dimensions: int,
    ) -> DocumentVersion:
        self.events.append("create_version")
        version_id = self._new_version_id()
        return DocumentVersion(
            version_id=version_id,
            organization_id=organization_id,
            document_id=document_id,
            version_no=1,
            content_hash=content_hash,
            raw_text=raw_text,
            loader_version=loader_version,
            chunker_version=chunker_version,
            embedding_model=embedding_model,
            embedding_dimensions=embedding_dimensions,
            created_at="2026-01-01T00:00:00+00:00",
        )

    # job -----------------------------------------------------------------
    def create_job(
        self,
        *,
        organization_id: str,
        document_id: str,
        version_id: str,
    ) -> IngestionJob:
        self.events.append("create_job")
        return IngestionJob(
            job_id="job-1",
            organization_id=organization_id,
            document_id=document_id,
            version_id=version_id,
            status=IngestionStatus.QUEUED,
            attempt_count=0,
            error_code=None,
            error_message=None,
            started_at=None,
            finished_at=None,
            created_at="2026-01-01T00:00:00+00:00",
        )

    def mark_job_running(
        self,
        *,
        organization_id: str,
        job_id: str,
    ) -> IngestionJob | None:
        self.events.append("mark_job_running")
        return IngestionJob(
            job_id=job_id,
            organization_id=organization_id,
            document_id="doc-1",
            version_id="version-1",
            status=IngestionStatus.RUNNING,
            attempt_count=1,
            error_code=None,
            error_message=None,
            started_at="2026-01-01T00:00:00+00:00",
            finished_at=None,
            created_at="2026-01-01T00:00:00+00:00",
        )

    # chunks --------------------------------------------------------------
    def replace_chunks(
        self,
        *,
        organization_id: str,
        document_id: str,
        version_id: str,
        chunks,
    ) -> None:
        self.events.append("replace_chunks")

    def activate_version(
        self,
        *,
        organization_id: str,
        document_id: str,
        version_id: str,
        job_id: str,
    ) -> KnowledgeDocument:
        self.events.append("activate_version")
        doc = self.documents[document_id]
        updated = KnowledgeDocument(
            document_id=doc.document_id,
            organization_id=doc.organization_id,
            uploaded_by_user_id=doc.uploaded_by_user_id,
            title=doc.title,
            source_type=doc.source_type,
            status=DocumentStatus.ACTIVE,
            active_version_id=version_id,
            created_at=doc.created_at,
            updated_at="2026-01-02T00:00:00+00:00",
        )
        self.documents[document_id] = updated
        return updated


class RecordingLoader:
    def __init__(self, events):
        self.events = events

    def load(self, content, source_type):
        self.events.append("load")
        return _LoadedDocument(content)


# A tiny stand-in for the loader's LoadedDocument (text + sections).
class _Record:
    def __init__(self, text):
        self.text = text
        self.sections = ()


def _LoadedDocument(content_bytes):
    return _Record(content_bytes.decode("utf-8"))


class RecordingChunker:
    def __init__(self, events):
        self.events = events

    def split(self, document, *, organization_id, document_id, version_id):
        self.events.append("chunk")
        # Two chunks with Task 4 stable ids so the upsert test can assert the
        # points reused exactly those ids.
        return [
            DocumentChunk(
                chunk_id=_stable_chunk_id(organization_id, version_id, 0),
                organization_id=organization_id,
                document_id=document_id,
                version_id=version_id,
                ordinal=0,
                heading_path=None,
                content="first chunk content",
                token_count=3,
                start_offset=0,
                end_offset=18,
            ),
            DocumentChunk(
                chunk_id=_stable_chunk_id(organization_id, version_id, 1),
                organization_id=organization_id,
                document_id=document_id,
                version_id=version_id,
                ordinal=1,
                heading_path=None,
                content="second chunk content",
                token_count=3,
                start_offset=19,
                end_offset=38,
            ),
        ]


class RecordingEmbedding:
    """Returns fixed, deterministic dense+sparse vectors; never sees anything
    but plain text to embed (no titles/org ids/api keys)."""

    def __init__(self, events, dimensions=DEFAULT_EMBEDDING_DIMENSIONS):
        self.events = events
        self.dimensions = dimensions
        self.seen_texts: list[str] = []
        self.embedded_inputs = 0

    def embed_documents(self, texts):
        self.events.append("embed_documents")
        self.embedded_inputs += len(texts)
        self.seen_texts.extend(texts)
        return [
            EmbeddingVector(
                dense=tuple(float(i % 7) for i in range(self.dimensions)),
                sparse=(SparseValue(index=0, value=1.0),),
                token_count=count_tokens(t),
            )
            for t in texts
        ]

    def embed_query(self, text):
        return EmbeddingVector(
            dense=tuple(0.5 for _ in range(self.dimensions)),
            sparse=(),
            token_count=count_tokens(text),
        )


class RecordingVectorStore:
    """ensure_collection then upsert, recording order; upsert checks the
    document is NOT yet active."""

    def __init__(self, events, store, active_state_at_upsert):
        self.events = events
        self.store = store
        self.active_state_at_upsert = active_state_at_upsert
        self.points: list[VectorPoint] = []
        self.ensure_collections = 0

    def ensure_collection(self):
        self.ensure_collections += 1
        self.events.append("vector_ensure_collection")

    def upsert(self, *, points):
        self.events.append("vector_upsert")
        self.points = list(points)
        for point in self.points:
            self.active_state_at_upsert.append(
                self.store._active(point.document_id)
            )

    def search(self, **kwargs):
        return []


# -------------------------------------------------------------- fixtures


@pytest.fixture
def ingestion_scope():
    events: list[str] = []
    store = RecordingKnowledgeStore(events)
    loader = RecordingLoader(events)
    chunker = RecordingChunker(events)
    embedding = RecordingEmbedding(events)

    active_state_at_upsert: list[bool] = []
    vectors = RecordingVectorStore(events, store, active_state_at_upsert)

    class Scope:
        pass

    service = KnowledgeIngestionService(
        store=store,
        loader=loader,
        chunker=chunker,
        embedding=embedding,
        vector_store=vectors,
    )
    Scope.service = service
    Scope.events = events
    Scope.store = store
    Scope.embedding = embedding
    Scope.vectors = vectors
    Scope.active_state_at_upsert = active_state_at_upsert
    Scope.documents1 = store.documents
    return Scope


# ------------------------------------------------------------------ tests


def test_new_document_activates_only_after_vector_upsert(ingestion_scope):
    receipt = ingestion_scope.service.ingest_new_document(
        organization_id="org-a",
        uploaded_by_user_id="user-a",
        title="退货政策",
        source_type=DocumentSourceType.MARKDOWN,
        content=b"# Returns\nSeven days.",
    )

    assert ingestion_scope.events == [
        "load",
        "create_document",
        "create_version",
        "create_job",
        "mark_job_running",
        "chunk",
        "embed_documents",
        "replace_chunks",
        "vector_ensure_collection",
        "vector_upsert",
        "activate_version",
    ]
    assert receipt.status is IngestionStatus.SUCCEEDED
    assert receipt.deduplicated is False
    # At every upsert the document was still NOT active.
    assert ingestion_scope.active_state_at_upsert == [False, False]


def test_new_version_reuses_document_source_type_and_validates_upload(ingestion_scope):
    service = ingestion_scope.service
    first = service.ingest_new_document(
        organization_id="org-a",
        uploaded_by_user_id="user-a",
        title="A",
        source_type=DocumentSourceType.TEXT,
        content=b"some plain text body",
    )

    # Upload source_type must match the document's original: TEXT here.
    with pytest.raises(InvalidDocumentError):
        service.ingest_new_version(
            organization_id="org-a",
            uploaded_by_user_id="user-a",
            document_id=first.document_id,
            source_type=DocumentSourceType.MARKDOWN,
            content=b"# Different\ncontent",
        )

    # A matching second version succeeds and is deduplicated=False.
    receipt2 = service.ingest_new_version(
        organization_id="org-a",
        uploaded_by_user_id="user-a",
        document_id=first.document_id,
        source_type=DocumentSourceType.TEXT,
        content=b"# Second\nplain text body",
    )
    assert receipt2.status is IngestionStatus.SUCCEEDED
    assert receipt2.document_id == first.document_id


def test_new_version_missing_document_raises_not_found(ingestion_scope):
    with pytest.raises(DocumentNotFoundError):
        ingestion_scope.service.ingest_new_version(
            organization_id="org-a",
            uploaded_by_user_id="user-a",
            document_id="missing-doc",
            source_type=DocumentSourceType.TEXT,
            content=b"anything",
        )


def test_embedding_receives_only_chunk_contents(ingestion_scope):
    receipt = ingestion_scope.service.ingest_new_document(
        organization_id="org-a",
        uploaded_by_user_id="user-a",
        title="T",
        source_type=DocumentSourceType.TEXT,
        content=b"first chunk content second chunk content",
    )
    assert ingestion_scope.embedding.seen_texts == [
        "first chunk content",
        "second chunk content",
    ]
    # org id / title / api keys never leak into the embedding inputs.
    assert "org-a" not in " ".join(ingestion_scope.embedding.seen_texts)
    assert "user-a" not in " ".join(ingestion_scope.embedding.seen_texts)
    assert "T" not in " ".join(ingestion_scope.embedding.seen_texts)


def test_vector_points_use_stable_chunk_ids(ingestion_scope):
    receipt = ingestion_scope.service.ingest_new_document(
        organization_id="org-a",
        uploaded_by_user_id="user-a",
        title="T",
        source_type=DocumentSourceType.TEXT,
        content=b"first chunk content second chunk content",
    )
    version_id = receipt.version_id
    assert [p.chunk_id for p in ingestion_scope.vectors.points] == [
        _stable_chunk_id("org-a", version_id, 0),
        _stable_chunk_id("org-a", version_id, 1),
    ]
    # Every point carries the trusted organization id and the point embedding
    # came straight from the EmbeddingClient responses.
    assert all(p.organization_id == "org-a" for p in ingestion_scope.vectors.points)
    assert all(
        p.embedding.token_count
        for p in ingestion_scope.vectors.points
    )


def test_ensure_collection_fires_after_replace_chunks_before_upsert(ingestion_scope):
    ingestion_scope.service.ingest_new_document(
        organization_id="org-a",
        uploaded_by_user_id="user-a",
        title="T",
        source_type=DocumentSourceType.TEXT,
        content=b"first chunk content second chunk content",
    )
    ordered = [e for e in ingestion_scope.events if e in {
        "replace_chunks", "vector_ensure_collection", "vector_upsert",
    }]
    assert ordered == [
        "replace_chunks",
        "vector_ensure_collection",
        "vector_upsert",
    ]


def test_title_is_stripped_and_length_validated(ingestion_scope):
    service = ingestion_scope.service
    # Whitespace-padded title is stripped to a valid length.
    receipt = service.ingest_new_document(
        organization_id="org-a",
        uploaded_by_user_id="user-a",
        title="  Given title  ",
        source_type=DocumentSourceType.TEXT,
        content=b"body",
    )
    from app.knowledge.base import KnowledgeDocument
    assert (
        ingestion_scope.store.documents[receipt.document_id].title
        == "Given title"
    )

    # Blank title -> invalid.
    with pytest.raises(InvalidDocumentError):
        service.ingest_new_document(
            organization_id="org-a",
            uploaded_by_user_id="user-a",
            title="   ",
            source_type=DocumentSourceType.TEXT,
            content=b"body",
        )
    # Over-long title -> invalid.
    with pytest.raises(InvalidDocumentError):
        service.ingest_new_document(
            organization_id="org-a",
            uploaded_by_user_id="user-a",
            title="x" * 201,
            source_type=DocumentSourceType.TEXT,
            content=b"body",
        )


def test_embedding_vector_count_mismatch_fails_before_upsert_and_activate():
    """A degraded embedding response (wrong vector count) must fail loudly
    before any point is upserted or any version is activated, so a searchable
    version can never be left with Qdrant points that don't mirror SQLite."""
    events: list[str] = []
    store = RecordingKnowledgeStore(events)
    loader = RecordingLoader(events)
    chunker = RecordingChunker(events)
    active_state_at_upsert: list[bool] = []
    vectors = RecordingVectorStore(events, store, active_state_at_upsert)

    class SparseEmbedding(RecordingEmbedding):
        """Embedding that silently drops the last vector (returns N-1)."""

        def embed_documents(self, texts):
            self.events.append("embed_documents")
            full = super().embed_documents(texts)
            return full[:-1]

    embedding = SparseEmbedding(events)

    service = KnowledgeIngestionService(
        store=store,
        loader=loader,
        chunker=chunker,
        embedding=embedding,
        vector_store=vectors,
    )

    with pytest.raises(EmbeddingUnavailableError):
        service.ingest_new_document(
            organization_id="org-a",
            uploaded_by_user_id="user-a",
            title="T",
            source_type=DocumentSourceType.TEXT,
            content=b"first chunk content second chunk content",
        )

    # The failure happens after embed but before any chunk persistence in the
    # vector store: no upsert, no activation, and no points written.
    assert "vector_upsert" not in events
    assert "activate_version" not in events
    assert vectors.points == []
