"""Tenant-scoped persistence tests for SQLiteKnowledgeStore.

Builds two real enterprises via the User/Organization stores so the
uploader membership FK and composite tenant FKs are exercised for real.
"""

from dataclasses import dataclass

import pytest

from app.knowledge import (
    DocumentChunk,
    DocumentNotFoundError,
    DocumentSourceType,
    DocumentStatus,
    DuplicateDocumentVersionError,
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
    version = store.create_version(
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
