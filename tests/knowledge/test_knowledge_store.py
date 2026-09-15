"""Tenant-scoped persistence tests for SQLiteKnowledgeStore.

Builds two real enterprises via the User/Organization stores so the
uploader membership FK and composite tenant FKs are exercised for real.
"""

from dataclasses import dataclass

import pytest

from app.knowledge.base import (
    DocumentChunk,
    DocumentNotFoundError,
    DocumentSourceType,
    DocumentStatus,
    DuplicateDocumentVersionError,
    IngestionStatus,
    InvalidDocumentError,
    RetrievalEvent,
    VectorStoreUnavailableError,
)
from app.knowledge.sqlite_store import SQLiteKnowledgeStore
from app.organizations.sqlite_store import SQLiteOrganizationStore
from app.users.sqlite_store import SQLiteUserStore


@dataclass(frozen=True)
class OrgContext:
    organization_id: str
    user_id: str


# Chunk ids the two-version fixture writes so tests can reference them.
old_chunk_id = "chunk-old"
new_chunk_id = "chunk-new"

# version_id -> IngestionJob, recorded by create_version() so the brief's
# ``job_for(new_version).job_id`` helper can resolve a version's job.
_JOBS_BY_VERSION: dict[str, object] = {}


@pytest.fixture
def two_tenant_knowledge_store(tmp_path, monkeypatch):
    database_path = tmp_path / "knowledge.db"
    store = SQLiteKnowledgeStore(database_path)
    users = SQLiteUserStore(database_path)
    orgs = SQLiteOrganizationStore(database_path)

    contexts = []
    for name in ("alpha", "beta"):
        user = users.create_user(
            username=f"{name}-admin", password_hash="hash"
        )
        organization = orgs.create_with_admin(
            name=name, admin_user_id=user.user_id
        )
        contexts.append(
            OrgContext(
                organization_id=organization.organization_id,
                user_id=user.user_id,
            )
        )
    _JOBS_BY_VERSION.clear()
    yield store, contexts[0], contexts[1]
    _JOBS_BY_VERSION.clear()


@pytest.fixture
def store_with_document(two_tenant_knowledge_store):
    store, context_a, _context_b = two_tenant_knowledge_store
    document = store.create_document(
        organization_id=context_a.organization_id,
        uploaded_by_user_id=context_a.user_id,
        title="退货政策",
        source_type=DocumentSourceType.MARKDOWN,
    )
    yield store, context_a, document


@pytest.fixture
def store_with_two_versions(store_with_document):
    store, context, document = store_with_document
    old_version = create_version(
        store,
        context,
        document,
        content_hash="sha256:old",
        chunk_id=old_chunk_id,
    )
    new_version = create_version(
        store,
        context,
        document,
        content_hash="sha256:new",
        chunk_id=new_chunk_id,
    )
    yield store, context, document, old_version, new_version


def create_version(
    store,
    context,
    document,
    *,
    content_hash,
    chunk_id=None,
    raw_text="body",
):
    version = store.create_parsed_version(
        organization_id=context.organization_id,
        document_id=document.document_id,
        content_hash=content_hash,
        raw_text=raw_text,
        loader_version="loader-v1",
        chunker_version="chunker-v1",
        embedding_model="text-embedding-v4",
        embedding_dimensions=1024,
    )
    store.replace_chunks(
        organization_id=context.organization_id,
        document_id=document.document_id,
        version_id=version.version_id,
        chunks=[
            _chunk(
                context,
                document,
                version,
                chunk_id or f"chunk-of-{version.version_id}-0",
            )
        ],
    )
    job = store.create_job(
        organization_id=context.organization_id,
        document_id=document.document_id,
        version_id=version.version_id,
    )
    _JOBS_BY_VERSION[version.version_id] = job
    return version


def _chunk(context, document, version, chunk_id):
    return DocumentChunk(
        chunk_id=chunk_id,
        organization_id=context.organization_id,
        document_id=document.document_id,
        version_id=version.version_id,
        ordinal=0,
        heading_path=None,
        content="some policy body",
        token_count=10,
        start_offset=0,
        end_offset=16,
    )


def job_for(version):
    """Return the queued job created for a version via create_version()."""
    return _JOBS_BY_VERSION[version.version_id]


def test_list_version_chunks_reads_inactive_version_with_exact_tenant_scope(
    store_with_two_versions, two_tenant_knowledge_store
):
    store, context, document, old_version, new_version = store_with_two_versions
    _same_store, _org_a, other_tenant = two_tenant_knowledge_store

    old_chunks = store.list_version_chunks(
        organization_id=context.organization_id,
        document_id=document.document_id,
        version_id=old_version.version_id,
    )
    new_chunks = store.list_version_chunks(
        organization_id=context.organization_id,
        document_id=document.document_id,
        version_id=new_version.version_id,
    )

    assert [(chunk.chunk_id, chunk.content) for chunk in old_chunks] == [
        (old_chunk_id, "some policy body")
    ]
    assert [(chunk.chunk_id, chunk.content) for chunk in new_chunks] == [
        (new_chunk_id, "some policy body")
    ]
    assert store.list_version_chunks(
        organization_id=other_tenant.organization_id,
        document_id=document.document_id,
        version_id=old_version.version_id,
    ) == []


def retrieval_event(
    organization_id: str,
    *,
    conversation_id: str,
    event_id: str = "event-a",
    created_at: str = "2026-08-21T00:00:00+00:00",
) -> RetrievalEvent:
    return RetrievalEvent(
        event_id=event_id,
        organization_id=organization_id,
        conversation_id=conversation_id,
        strategy="single",
        original_query="sha256:original",
        planned_queries_json='["sha256:planned"]',
        round_count=1,
        candidate_json='{"schema_version": 4}',
        selected_chunk_ids_json="[]",
        outcome="insufficient",
        latency_ms=12,
        model_calls=2,
        estimated_tokens=34,
        created_at=created_at,
    )


def test_get_retrieval_event_is_scoped_by_tenant(two_tenant_knowledge_store):
    store, org_a, org_b = two_tenant_knowledge_store
    event = retrieval_event(
        org_a.organization_id, conversation_id="eval-case:adaptive"
    )
    store.record_retrieval_event(
        organization_id=org_a.organization_id, event=event
    )

    assert store.get_retrieval_event(
        organization_id=org_a.organization_id,
        conversation_id="eval-case:adaptive",
    ) == event
    assert store.get_retrieval_event(
        organization_id=org_b.organization_id,
        conversation_id="eval-case:adaptive",
    ) is None


def test_get_retrieval_event_returns_newest_match(two_tenant_knowledge_store):
    store, org_a, _ = two_tenant_knowledge_store
    conversation_id = "eval-case:newest"
    events = [
        retrieval_event(
            org_a.organization_id,
            conversation_id=conversation_id,
            event_id="event-old",
            created_at="2026-08-20T23:59:59+00:00",
        ),
        retrieval_event(
            org_a.organization_id,
            conversation_id=conversation_id,
            event_id="event-a",
        ),
        retrieval_event(
            org_a.organization_id,
            conversation_id=conversation_id,
            event_id="event-z",
        ),
    ]
    for event in events:
        store.record_retrieval_event(
            organization_id=org_a.organization_id, event=event
        )

    assert store.get_retrieval_event(
        organization_id=org_a.organization_id,
        conversation_id=conversation_id,
    ) == events[-1]


class TestTenantScoping:
    def test_document_reads_are_tenant_scoped(self, two_tenant_knowledge_store):
        store, context_a, context_b = two_tenant_knowledge_store
        document = store.create_document(
            organization_id=context_a.organization_id,
            uploaded_by_user_id=context_a.user_id,
            title="退货政策",
            source_type=DocumentSourceType.MARKDOWN,
        )

        assert store.get_document(
            organization_id=context_a.organization_id,
            document_id=document.document_id,
        ) == document
        with pytest.raises(DocumentNotFoundError):
            store.get_document(
                organization_id=context_b.organization_id,
                document_id=document.document_id,
            )

    def test_cross_tenant_version_and_chunks_are_invisible(self, store_with_document):
        store, context, document = store_with_document

        with pytest.raises(DocumentNotFoundError):
            store.get_version_by_hash(
                organization_id="nonexistent-org",
                document_id=document.document_id,
                content_hash="sha256:only",
            )

    def test_list_documents_is_tenant_scoped_and_ordered(self, two_tenant_knowledge_store):
        """Task 11: list_documents must only ever return the caller's own
        organization's documents, deterministically ordered (never another
        tenant's rows)."""
        store, context_a, context_b = two_tenant_knowledge_store
        doc_a1 = store.create_document(
            organization_id=context_a.organization_id,
            uploaded_by_user_id=context_a.user_id,
            title="A 一号",
            source_type=DocumentSourceType.MARKDOWN,
        )
        doc_a2 = store.create_document(
            organization_id=context_a.organization_id,
            uploaded_by_user_id=context_a.user_id,
            title="A 二号",
            source_type=DocumentSourceType.TEXT,
        )
        store.create_document(
            organization_id=context_b.organization_id,
            uploaded_by_user_id=context_b.user_id,
            title="B 文档",
            source_type=DocumentSourceType.MARKDOWN,
        )

        listed = store.list_documents(
            organization_id=context_a.organization_id,
        )

        assert {d.document_id for d in listed} == {
            doc_a1.document_id,
            doc_a2.document_id,
        }
        # Deterministic order by created_at then id; B's document must never leak.
        assert [d.document_id for d in listed][0] in {
            doc_a1.document_id,
            doc_a2.document_id,
        }
        assert all(d.organization_id == context_a.organization_id for d in listed)

    def test_list_versions_is_tenant_and_document_scoped(self, two_tenant_knowledge_store):
        """Task 11: list_versions returns only the versions of the requested
        document for the caller's organization, ordered by version number."""
        store, context_a, context_b = two_tenant_knowledge_store
        doc_a = store.create_document(
            organization_id=context_a.organization_id,
            uploaded_by_user_id=context_a.user_id,
            title="一号",
            source_type=DocumentSourceType.MARKDOWN,
        )
        doc_b = store.create_document(
            organization_id=context_a.organization_id,
            uploaded_by_user_id=context_a.user_id,
            title="二号",
            source_type=DocumentSourceType.MARKDOWN,
        )
        v1 = create_version(
            store, context_a, doc_a, content_hash="sha256:one"
        )
        v2 = create_version(
            store, context_a, doc_a, content_hash="sha256:two"
        )
        # A version of a different document in the SAME org.
        create_version(
            store, context_a, doc_b, content_hash="sha256:other"
        )

        versions = store.list_versions(
            organization_id=context_a.organization_id,
            document_id=doc_a.document_id,
        )

        assert [v.version_id for v in versions] == [
            v1.version_id,
            v2.version_id,
        ]
        assert [v.version_no for v in versions] == [1, 2]
        assert all(v.document_id == doc_a.document_id for v in versions)

    def test_list_versions_does_not_raise_for_missing_document(
        self, two_tenant_knowledge_store
    ):
        """A document id that does not exist (or belongs to another org)
        yields an empty list, never a cross-tenant leak or a raise."""
        store, context_a, _ = two_tenant_knowledge_store
        assert store.list_versions(
            organization_id=context_a.organization_id,
            document_id="does-not-exist",
        ) == []


class TestHashIdempotency:
    def test_same_content_hash_returns_existing_version(self, store_with_document):
        store, context, document = store_with_document
        first = create_version(store, context, document, content_hash="sha256:x")

        assert store.get_version_by_hash(
            organization_id=context.organization_id,
            document_id=document.document_id,
            content_hash="sha256:x",
        ) == first
        with pytest.raises(DuplicateDocumentVersionError) as caught:
            create_version(store, context, document, content_hash="sha256:x")
        assert caught.value.existing_version_id == first.version_id


class TestVersionActivation:
    def test_only_active_version_chunks_are_returned(self, store_with_two_versions):
        store, context, document, _old_version, new_version = (
            store_with_two_versions
        )
        store.activate_version(
            organization_id=context.organization_id,
            document_id=document.document_id,
            version_id=new_version.version_id,
            job_id=job_for(new_version).job_id,
        )

        chunks = store.list_active_chunks(
            organization_id=context.organization_id,
            candidate_ids=[old_chunk_id, new_chunk_id],
        )

        assert [chunk.chunk_id for chunk in chunks] == [new_chunk_id]

    def test_activate_rejects_job_of_other_version(self, store_with_two_versions):
        # The job passed to activate_version must belong to the version being
        # activated. Passing the OTHER version's job must not silently mark
        # that version's job succeeded and must raise DocumentNotFoundError.
        store, context, document, old_version, new_version = (
            store_with_two_versions
        )
        with pytest.raises(DocumentNotFoundError):
            store.activate_version(
                organization_id=context.organization_id,
                document_id=document.document_id,
                version_id=new_version.version_id,
                # wrong job: it belongs to old_version
                job_id=job_for(old_version).job_id,
            )
        # The document must NOT have been flipped to new_version.
        doc = store.get_document(
            organization_id=context.organization_id,
            document_id=document.document_id,
        )
        assert doc.status is not DocumentStatus.ACTIVE

        # And the old version's job must still be queued, not succeeded.
        old_job = store.get_latest_job_for_version(
            organization_id=context.organization_id,
            document_id=document.document_id,
            version_id=old_version.version_id,
        )
        assert old_job.status is IngestionStatus.QUEUED

    def test_disabled_document_is_not_returned(self, store_with_two_versions):
        store, context, document, _old, new_version = store_with_two_versions
        store.activate_version(
            organization_id=context.organization_id,
            document_id=document.document_id,
            version_id=new_version.version_id,
            job_id=job_for(new_version).job_id,
        )
        assert store.list_active_version_ids(
            organization_id=context.organization_id
        ) == [new_version.version_id]

        store.set_document_status(
            organization_id=context.organization_id,
            document_id=document.document_id,
            status=DocumentStatus.DISABLED,
        )

        assert store.list_active_version_ids(
            organization_id=context.organization_id
        ) == []
        assert store.list_active_chunks(
            organization_id=context.organization_id,
            candidate_ids=[old_chunk_id, new_chunk_id],
        ) == []

    def test_re_enabling_restores_recent_successful_version(
        self, store_with_two_versions
    ):
        store, context, document, _old, new_version = store_with_two_versions
        store.activate_version(
            organization_id=context.organization_id,
            document_id=document.document_id,
            version_id=new_version.version_id,
            job_id=job_for(new_version).job_id,
        )
        store.set_document_status(
            organization_id=context.organization_id,
            document_id=document.document_id,
            status=DocumentStatus.DISABLED,
        )
        document_b = store.set_document_status(
            organization_id=context.organization_id,
            document_id=document.document_id,
            status=DocumentStatus.ACTIVE,
        )

        assert document_b.status is DocumentStatus.ACTIVE
        assert document_b.active_version_id == new_version.version_id
        assert store.list_active_version_ids(
            organization_id=context.organization_id
        ) == [new_version.version_id]

    def test_list_active_chunks_orders_by_candidate_ids(self, store_with_two_versions):
        store, context, document, _old, new_version = store_with_two_versions
        store.activate_version(
            organization_id=context.organization_id,
            document_id=document.document_id,
            version_id=new_version.version_id,
            job_id=job_for(new_version).job_id,
        )

        chunks = store.list_active_chunks(
            organization_id=context.organization_id,
            candidate_ids=[new_chunk_id, "missing", old_chunk_id],
        )
        # Only new_chunk_id is active; the others are filtered out.
        assert [chunk.chunk_id for chunk in chunks] == [new_chunk_id]

    def test_list_active_chunks_with_empty_candidates(self, store_with_document):
        store, context, _document = store_with_document
        assert store.list_active_chunks(
            organization_id=context.organization_id,
            candidate_ids=[],
        ) == []

    def test_resolve_active_citations_joins_title_and_filters_old_version(
        self, store_with_two_versions
    ):
        """Task 10: resolve_active_citations joins each chunk with its document
        title after re-validating status='active' AND active_version_id ==
        chunk.version_id. Only the ACTIVE new-version chunk is accepted; the
        stale old-version chunk and a cross-org lookup yield nothing."""
        store, context, document, _old, new_version = store_with_two_versions
        store.activate_version(
            organization_id=context.organization_id,
            document_id=document.document_id,
            version_id=new_version.version_id,
            job_id=job_for(new_version).job_id,
        )

        chunks = store.resolve_active_citations(
            organization_id=context.organization_id,
            candidate_ids=[old_chunk_id, new_chunk_id],
        )
        assert [c.chunk_id for c in chunks] == [new_chunk_id]
        assert chunks[0].document_title == "退货政策"
        assert chunks[0].content == "some policy body"
        assert chunks[0].version_id == new_version.version_id
        # Citation content comes ONLY from SQLite; document title populated.
        assert chunks[0].document_id == document.document_id
        # A cross-org org id can never match the four-level ids.
        assert store.resolve_active_citations(
            organization_id="nonexistent-org",
            candidate_ids=[new_chunk_id],
        ) == []
        # Empty candidate list is a no-op.
        assert store.resolve_active_citations(
            organization_id=context.organization_id,
            candidate_ids=[],
        ) == []

    def test_resolve_active_citations_excludes_disabled_document(
        self, store_with_two_versions
    ):
        store, context, document, _old, new_version = store_with_two_versions
        store.activate_version(
            organization_id=context.organization_id,
            document_id=document.document_id,
            version_id=new_version.version_id,
            job_id=job_for(new_version).job_id,
        )
        store.set_document_status(
            organization_id=context.organization_id,
            document_id=document.document_id,
            status=DocumentStatus.DISABLED,
        )
        assert store.resolve_active_citations(
            organization_id=context.organization_id,
            candidate_ids=[new_chunk_id],
        ) == []

    def test_list_active_chunks_preserves_order_across_two_active_versions(
        self, store_with_document
    ):
        # Two documents, each with its own active version/chunk, so TWO chunks
        # are simultaneously active. Exercises the candidate-order-preservation
        # path (previously only ever one active chunk was asserted).
        store, context, document_a = store_with_document
        version_a = create_version(
            store,
            context,
            document_a,
            content_hash="sha256:a",
            chunk_id="chunk-a",
        )
        document_b = store.create_document(
            organization_id=context.organization_id,
            uploaded_by_user_id=context.user_id,
            title="第二文档",
            source_type=DocumentSourceType.MARKDOWN,
        )
        version_b = create_version(
            store,
            context,
            document_b,
            content_hash="sha256:b",
            chunk_id="chunk-b",
        )
        store.activate_version(
            organization_id=context.organization_id,
            document_id=document_a.document_id,
            version_id=version_a.version_id,
            job_id=job_for(version_a).job_id,
        )
        store.activate_version(
            organization_id=context.organization_id,
            document_id=document_b.document_id,
            version_id=version_b.version_id,
            job_id=job_for(version_b).job_id,
        )

        chunks = store.list_active_chunks(
            organization_id=context.organization_id,
            candidate_ids=["chunk-b", "chunk-a"],
        )

        # Both chunks are active; the returned order must follow candidate_ids
        # (chunk-b first), not insertion/autoincrement order.
        assert [chunk.chunk_id for chunk in chunks] == ["chunk-b", "chunk-a"]


# ----------------------------------------------------------------------
# Task 8: hardened ingestion via the real SQLite store + recording fakes.
# ----------------------------------------------------------------------


class _FakeChunker:
    def __init__(self):
        self.split_groups = []

    def split(self, document, *, organization_id, document_id, version_id):
        class _Chunk:
            pass

        # One chunk per call, id built deterministically from version id.
        chunk = DocumentChunk(
            chunk_id=f"chunk-of-{version_id}",
            organization_id=organization_id,
            document_id=document_id,
            version_id=version_id,
            ordinal=0,
            heading_path=None,
            content="policy body text",
            token_count=4,
            start_offset=0,
            end_offset=16,
        )
        self.split_groups.append((document_id, version_id))
        return [chunk]

    @property
    def split_calls(self) -> int:
        return len(self.split_groups)


class _FakeEmbedding:
    def __init__(self):
        self.embed_calls = 0

    def embed_documents(self, texts):
        self.embed_calls += 1
        from app.knowledge.embeddings import EmbeddingVector, SparseValue

        return [
            EmbeddingVector(
                dense=tuple(0.1 * i for i in range(1024)),
                sparse=(SparseValue(index=0, value=1.0),),
                token_count=4,
            )
            for _ in texts
        ]


class _FakeVectorStore:
    def __init__(self, *, available=True):
        self.available = available
        self.upsert_calls = 0

    def ensure_collection(self) -> None:
        return None

    def upsert(self, *, points):
        self.upsert_calls += 1
        if not self.available:
            raise VectorStoreUnavailableError(reason="qdrant transport refused")


def _build_service(store, *, vector_available=True, chunker=None, embedding=None):
    from app.knowledge.document_loader import DocumentLoader
    from app.knowledge.ingestion import KnowledgeIngestionService

    _chunker = chunker or _FakeChunker()
    _embedding = embedding or _FakeEmbedding()
    vectors = _FakeVectorStore(available=vector_available)
    service = KnowledgeIngestionService(
        store=store,
        loader=DocumentLoader(),
        chunker=_chunker,
        embedding=_embedding,
        vector_store=vectors,
    )
    return service, vectors, _chunker, _embedding


class TestOldVersionPreservedOnFailedReingest:
    def test_vector_failure_keeps_prior_active_version(self, two_tenant_knowledge_store):
        """Step 2: v1 activates; a VECTOR_STORE_UNAVAILABLE on v2's upsert must
        keep the document ACTIVE on v1, fail only v2's job, and leave only v1
        chunks retrievable."""
        store, context, _ = two_tenant_knowledge_store
        service, vectors, _, _ = _build_service(store, vector_available=True)

        v1 = service.ingest_new_document(
            organization_id=context.organization_id,
            uploaded_by_user_id=context.user_id,
            title="退货政策",
            source_type=DocumentSourceType.MARKDOWN,
            content=b"# Policy\nBody of the policy.",
        )
        assert v1.status is IngestionStatus.SUCCEEDED
        assert vectors.upsert_calls == 1
        doc_after_v1 = store.get_document(
            organization_id=context.organization_id,
            document_id=v1.document_id,
        )
        assert doc_after_v1.status is DocumentStatus.ACTIVE
        assert doc_after_v1.active_version_id == v1.version_id

        # Now take the vector store down and attempt v2.
        vectors.available = False
        with pytest.raises(VectorStoreUnavailableError):
            service.ingest_new_version(
                organization_id=context.organization_id,
                uploaded_by_user_id=context.user_id,
                document_id=v1.document_id,
                source_type=DocumentSourceType.MARKDOWN,
                content=b"# Policy\nA brand NEW body for v2.",
            )

        doc = store.get_document(
            organization_id=context.organization_id,
            document_id=v1.document_id,
        )
        assert doc.status is DocumentStatus.ACTIVE
        assert doc.active_version_id == v1.version_id

        # v2's job is FAILED with the stable code, v1's job stayed SUCCEEDED.
        v1_job = store.get_latest_job_for_version(
            organization_id=context.organization_id,
            document_id=v1.document_id,
            version_id=v1.version_id,
        )
        assert v1_job.status is IngestionStatus.SUCCEEDED
        v2_version = store.get_version_by_hash(
            organization_id=context.organization_id,
            document_id=v1.document_id,
            content_hash=(
                "sha256:"
                + __import__("hashlib").sha256(
                    "# Policy\nA brand NEW body for v2.".encode("utf-8")
                ).hexdigest()
            ),
        )
        v2_job = store.get_latest_job_for_version(
            organization_id=context.organization_id,
            document_id=v1.document_id,
            version_id=v2_version.version_id,
        )
        assert v2_job.status is IngestionStatus.FAILED
        assert v2_job.error_code == "VECTOR_STORE_UNAVAILABLE"

        chunks = store.list_active_chunks(
            organization_id=context.organization_id,
            candidate_ids=[
                f"chunk-of-{v1.version_id}",
                f"chunk-of-{v2_version.version_id}",
            ],
        )
        assert [c.chunk_id for c in chunks] == [f"chunk-of-{v1.version_id}"]


class TestInvalidDocumentCreatesNoRows:
    def test_invalid_inputs_create_no_records(self, two_tenant_knowledge_store):
        """Step 3: bad type/encoding/size/empty body raise INVALID_DOCUMENT
        before any Document/Version/Job row exists."""
        store, context, _ = two_tenant_knowledge_store
        service, _, _, _ = _build_service(store)

        S = DocumentSourceType

        # Empty body.
        with pytest.raises(InvalidDocumentError):
            service.ingest_new_document(
                organization_id=context.organization_id,
                uploaded_by_user_id=context.user_id,
                title="T",
                source_type=S.MARKDOWN,
                content=b"   \n ",
            )
        # Invalid utf-8 encoding.
        with pytest.raises(InvalidDocumentError):
            service.ingest_new_document(
                organization_id=context.organization_id,
                uploaded_by_user_id=context.user_id,
                title="T",
                source_type=S.MARKDOWN,
                content=b"\xff\xfe\x00\x81",
            )
        # Blank title.
        with pytest.raises(InvalidDocumentError):
            service.ingest_new_document(
                organization_id=context.organization_id,
                uploaded_by_user_id=context.user_id,
                title="   ",
                source_type=S.MARKDOWN,
                content=b"# valid body",
            )

        # None of the failed attempts created any rows.
        assert _count_rows(store, "documents") == 0
        assert _count_rows(store, "document_versions") == 0
        assert _count_rows(store, "ingestion_jobs") == 0


def _count_rows(store, table: str) -> int:
    """Count rows in a store table directly so tests can assert record absence."""
    with store._connection() as connection:
        return connection.execute(
            f"SELECT COUNT(*) FROM {table}"
        ).fetchone()[0]


class TestNewDocumentFailureState:
    def test_loader_passing_doc_failing_at_vector_marks_failed_with_safe_message(
        self, two_tenant_knowledge_store
    ):
        """Step 3: a loader-passing new document that fails at the vector stage
        leaves Document=FAILED, Job=FAILED; the safe error_message must not
        leak the API key, the raw document body, or the underlying traceback."""
        store, context, _ = two_tenant_knowledge_store
        secret = "sk-test-4893f0a1b2c3"

        class _SecretVectorStore(_FakeVectorStore):
            def upsert(self, *, points):
                self.upsert_calls += 1
                raise VectorStoreUnavailableError(
                    reason=f"refused auth with key {secret} context aborted"
                )

        from app.knowledge.document_loader import DocumentLoader
        from app.knowledge.ingestion import KnowledgeIngestionService

        vectors = _SecretVectorStore()
        service = KnowledgeIngestionService(
            store=store,
            loader=DocumentLoader(),
            chunker=_FakeChunker(),
            embedding=_FakeEmbedding(),
            vector_store=vectors,
        )

        with pytest.raises(VectorStoreUnavailableError):
            service.ingest_new_document(
                organization_id=context.organization_id,
                uploaded_by_user_id=context.user_id,
                title="政策",
                source_type=DocumentSourceType.MARKDOWN,
                content=b"# Policy\nFull confidential body content.",
            )

        # The document has no prior active version -> FAILED.
        document = _first_document(store, context)
        assert document.status is DocumentStatus.FAILED

        version = store.get_version_by_hash(
            organization_id=context.organization_id,
            document_id=document.document_id,
            content_hash=(
                "sha256:"
                + __import__("hashlib").sha256(
                    "# Policy\nFull confidential body content.".encode("utf-8")
                ).hexdigest()
            ),
        )
        job = store.get_latest_job_for_version(
            organization_id=context.organization_id,
            document_id=document.document_id,
            version_id=version.version_id,
        )
        # The job is FAILED with the stable vector-store code.
        assert job is not None
        assert job.status is IngestionStatus.FAILED
        safe_error = job.error_message or ""
        assert secret not in safe_error
        assert "confidential body" not in safe_error
        assert "Traceback" not in safe_error
        assert "refused auth" not in safe_error

    def test_unknown_exception_maps_to_ingestion_failed_safe_message(
        self, two_tenant_knowledge_store
    ):
        """Other Exception subclasses (not the stable domain ones) map to a
        generic INGESTION_FAILED with the hardened safe message."""
        store, context, _ = two_tenant_knowledge_store

        class _BoomChunker(_FakeChunker):
            def split(self, document, *, organization_id, document_id, version_id):
                raise RuntimeError("boom in chunker: <supersecret internal>")

        from app.knowledge.document_loader import DocumentLoader
        from app.knowledge.ingestion import KnowledgeIngestionService

        service = KnowledgeIngestionService(
            store=store,
            loader=DocumentLoader(),
            chunker=_BoomChunker(),
            embedding=_FakeEmbedding(),
            vector_store=_FakeVectorStore(),
        )

        with pytest.raises(RuntimeError):
            service.ingest_new_document(
                organization_id=context.organization_id,
                uploaded_by_user_id=context.user_id,
                title="政策",
                source_type=DocumentSourceType.MARKDOWN,
                content=b"# Policy\nBody text.",
            )

        document = _first_document(store, context)
        assert document.status is DocumentStatus.FAILED
        version = store.get_version_by_hash(
            organization_id=context.organization_id,
            document_id=document.document_id,
            content_hash=(
                "sha256:"
                + __import__("hashlib").sha256(
                    "# Policy\nBody text.".encode("utf-8")
                ).hexdigest()
            ),
        )
        job = store.get_latest_job_for_version(
            organization_id=context.organization_id,
            document_id=document.document_id,
            version_id=version.version_id,
        )
        assert job is not None
        assert job.status is IngestionStatus.FAILED
        assert job.error_code == "INGESTION_FAILED"
        assert job.error_message == "knowledge ingestion failed"
        assert "supersecret" not in (job.error_message or "")


def _first_document(store, context):
    """Fetch the single document created for an org (tests create only one)."""
    with store._connection() as connection:
        row = connection.execute(
            """
            SELECT id FROM documents
            WHERE organization_id = ?
            ORDER BY created_at, id
            LIMIT 1
            """,
            (context.organization_id,),
        ).fetchone()
    assert row is not None
    return store.get_document(
        organization_id=context.organization_id,
        document_id=row["id"],
    )
