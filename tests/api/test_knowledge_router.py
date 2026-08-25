"""Tenant-scoped knowledge management HTTP API tests.

All services are fakes; no test touches the network, SQLite or Qdrant. The
router under test only talks to a ``KnowledgeIngestionService`` and a
``KnowledgeStore``, both injected as recording/deterministic stand-ins.
"""

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.api.knowledge_router import create_knowledge_router
from app.application.organization_service import TenantContext
from app.knowledge.base import (
    DocumentDisabledError,
    DocumentNotFoundError,
    DocumentSourceType,
    DocumentStatus,
    DocumentVersion,
    IngestionJob,
    IngestionStatus,
    InvalidDocumentError,
    KnowledgeDocument,
)
from app.knowledge.ingestion import IngestionReceipt
from app.organizations.base import MembershipRole


ORG_A = "org-a"
ORG_B = "org-b"
USER_A = "user-a"
USER_B = "user-b"


# ------------------------------------------------------------------ fakes


class FakeIngestionService:
    """Records every ingestion call; returns a fixed receipt.

    Emulates the real loader's content checks (size/UTF-8/empty) so the tests
    exercise the router's 422 mapping for empty or non-UTF-8 bodies, exactly as
    the production loader would raise ``InvalidDocumentError``.
    """

    def __init__(self) -> None:
        self.new_document_calls: list[dict] = []
        self.new_version_calls: list[dict] = []
        self.receipt = IngestionReceipt(
            document_id="doc-upload",
            version_id="version-upload",
            job_id="job-upload",
            status=IngestionStatus.SUCCEEDED,
            deduplicated=False,
        )

    def _load(self, content: bytes) -> None:
        try:
            text = content.decode("utf-8")
        except (UnicodeDecodeError, UnicodeError):
            raise InvalidDocumentError(reason="document is not valid utf-8")
        if not text.strip():
            raise InvalidDocumentError(reason="document is empty")

    def ingest_new_document(self, **kwargs) -> IngestionReceipt:
        self._load(kwargs["content"])
        self.new_document_calls.append(kwargs)
        return self.receipt

    def ingest_new_version(self, **kwargs) -> IngestionReceipt:
        self._load(kwargs["content"])
        self.new_version_calls.append(kwargs)
        return self.receipt


class FakeKnowledgeStore:
    """In-memory tenant-scoped store mirroring the read surface the router
    needs: list_documents, get_document, list_versions, latest-job lookup and
    set_document_status. ``DocumentNotFoundError`` is raised for ids missing or
    owned by another organization, exactly like the real SQLite store."""

    def __init__(self) -> None:
        self._documents: dict[str, KnowledgeDocument] = {}
        self._versions: dict[str, DocumentVersion] = {}
        self._jobs: dict[str, IngestionJob] = {}
        self.status_calls: list[dict] = []
        self._disabled: set[tuple[str, str]] = set()

    # seed helpers used by tests to build cross-tenant fixtures
    def add_document(self, document: KnowledgeDocument) -> None:
        self._documents[document.document_id] = document

    def add_version(self, version: DocumentVersion) -> None:
        self._versions[version.version_id] = version

    def add_job(self, job: IngestionJob) -> None:
        self._jobs[job.job_id] = job

    # read surface consumed by the router
    def list_documents(self, *, organization_id: str):
        return [
            doc
            for doc in self._documents.values()
            if doc.organization_id == organization_id
        ]

    def get_document(self, *, organization_id: str, document_id: str):
        doc = self._documents.get(document_id)
        if doc is None or doc.organization_id != organization_id:
            raise DocumentNotFoundError(organization_id, document_id)
        return doc

    def list_versions(self, *, organization_id: str, document_id: str):
        self.get_document(organization_id=organization_id, document_id=document_id)
        return [
            version
            for version in self._versions.values()
            if version.organization_id == organization_id
            and version.document_id == document_id
        ]

    def get_latest_job_for_version(
        self,
        *,
        organization_id: str,
        document_id: str,
        version_id: str,
        job_id: str | None = None,
    ):
        if job_id is not None:
            # Job-by-id lookup is scoped solely by organization_id (the real
            # store ignores document_id/version_id when job_id is supplied).
            job = self._jobs.get(job_id)
            if job is None or job.organization_id != organization_id:
                return None
            return job
        self.get_document(organization_id=organization_id, document_id=document_id)
        candidates = [
            job
            for job in self._jobs.values()
            if job.organization_id == organization_id
            and job.document_id == document_id
            and job.version_id == version_id
        ]
        if not candidates:
            return None
        return max(
            candidates, key=lambda job: (job.created_at, job.job_id)
        )

    def set_document_status(
        self,
        *,
        organization_id: str,
        document_id: str,
        status: DocumentStatus,
    ):
        doc = self.get_document(organization_id=organization_id, document_id=document_id)
        if status is DocumentStatus.DISABLED:
            if (organization_id, document_id) in self._disabled:
                raise DocumentDisabledError(organization_id, document_id)
            self._disabled.add((organization_id, document_id))
        else:
            self._disabled.discard((organization_id, document_id))
        self.status_calls.append(
            {"organization_id": organization_id, "document_id": document_id, "status": status}
        )
        return KnowledgeDocument(
            document_id=doc.document_id,
            organization_id=doc.organization_id,
            uploaded_by_user_id=doc.uploaded_by_user_id,
            title=doc.title,
            source_type=doc.source_type,
            status=status,
            active_version_id=doc.active_version_id,
            created_at=doc.created_at,
            updated_at=doc.updated_at,
        )


# ----------------------------------------------------------------- fixture


def make_document(
    document_id: str,
    org_id: str,
    user_id: str,
    *,
    title: str,
    source_type: DocumentSourceType = DocumentSourceType.MARKDOWN,
    status: DocumentStatus = DocumentStatus.ACTIVE,
    active_version_id: str | None = None,
) -> KnowledgeDocument:
    return KnowledgeDocument(
        document_id=document_id,
        organization_id=org_id,
        uploaded_by_user_id=user_id,
        title=title,
        source_type=source_type,
        status=status,
        active_version_id=active_version_id,
        created_at="2026-08-01T00:00:00+00:00",
        updated_at="2026-08-01T00:00:00+00:00",
    )


def make_version(
    version_id: str,
    org_id: str,
    document_id: str,
    *,
    version_no: int,
    content_hash: str,
) -> DocumentVersion:
    return DocumentVersion(
        version_id=version_id,
        organization_id=org_id,
        document_id=document_id,
        version_no=version_no,
        content_hash=content_hash,
        raw_text="secret body must never appear in a response",
        loader_version="loader-v1",
        chunker_version="chunker-v1",
        embedding_model="text-embedding-v4",
        embedding_dimensions=1024,
        created_at="2026-08-01T00:00:00+00:00",
    )


def make_job(
    job_id: str,
    org_id: str,
    document_id: str,
    version_id: str,
    *,
    status: IngestionStatus = IngestionStatus.SUCCEEDED,
    error_code: str | None = None,
    error_message: str | None = None,
) -> IngestionJob:
    return IngestionJob(
        job_id=job_id,
        organization_id=org_id,
        document_id=document_id,
        version_id=version_id,
        status=status,
        attempt_count=1,
        error_code=error_code,
        error_message=error_message,
        started_at="2026-08-01T00:00:00+00:00",
        finished_at="2026-08-01T00:00:00+00:00",
        created_at="2026-08-01T00:00:00+00:00",
    )


@pytest.fixture
def client_and_service():
    """Build an app wired to fake services plus a store-shaped context that is
    reachable through ``service.store`` for seeding GET fixtures."""

    def _build(role: MembershipRole):
        service = FakeIngestionService()
        store = FakeKnowledgeStore()
        service.store = store  # allow tests to seed remote-tenant data

        def get_current_tenant():
            return TenantContext(user_id=USER_A, organization_id=ORG_A, role=role)

        app = FastAPI()
        app.include_router(
            create_knowledge_router(
                ingestion_service=service,
                knowledge_store=store,
                get_current_tenant=get_current_tenant,
            )
        )
        return TestClient(app), service

    return _build


# ------------------------------------------------ Step 1: upload & permission


def test_admin_upload_uses_tenant_context(client_and_service):
    client, service = client_and_service(role=MembershipRole.ADMIN)
    response = client.post(
        "/knowledge/documents/",
        data={"title": "退货政策"},
        files={"file": ("returns.md", b"# Returns\nSeven days", "text/markdown")},
    )

    assert response.status_code == 201
    assert service.new_document_calls[0]["organization_id"] == ORG_A
    assert service.new_document_calls[0]["uploaded_by_user_id"] == USER_A
    assert "organization_id" not in response.request.content.decode()


def test_agent_cannot_upload(client_and_service):
    client, _ = client_and_service(role=MembershipRole.AGENT)
    response = client.post(
        "/knowledge/documents/",
        data={"title": "Policy"},
        files={"file": ("policy.txt", b"body", "text/plain")},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "admin role required"


# ---------------------------------------------- Step 2: full route matrix


def test_upload_response_shape_and_type_mapping(client_and_service):
    client, service = client_and_service(role=MembershipRole.ADMIN)

    md = client.post(
        "/knowledge/documents/",
        data={"title": "退货"},
        files={"file": ("returns.md", b"# R", "text/markdown")},
    )
    txt = client.post(
        "/knowledge/documents/",
        data={"title": "policy"},
        files={"file": ("policy.markdown", b"# more", "text/markdown")},
    )
    plain = client.post(
        "/knowledge/documents/",
        data={"title": "policy"},
        files={"file": ("policy.txt", b"body", "text/plain")},
    )

    for response in (md, txt, plain):
        assert response.status_code == 201
        assert response.json() == {
            "document_id": "doc-upload",
            "version_id": "version-upload",
            "job_id": "job-upload",
            "status": "succeeded",
            "deduplicated": False,
        }
    assert service.new_document_calls[0]["source_type"] is DocumentSourceType.MARKDOWN
    assert service.new_document_calls[1]["source_type"] is DocumentSourceType.MARKDOWN
    assert service.new_document_calls[2]["source_type"] is DocumentSourceType.TEXT
    assert service.new_document_calls[0]["content"] == b"# R"


def test_list_documents_is_tenant_scoped(client_and_service):
    client, service = client_and_service(role=MembershipRole.ADMIN)
    service.store.add_document(
        make_document("doc-a", ORG_A, USER_A, title="企业A政策",
                      active_version_id="version-1")
    )
    service.store.add_document(
        make_document("doc-b", ORG_B, USER_B, title="企业B政策")
    )

    response = client.get("/knowledge/documents/")

    assert response.status_code == 200
    docs = response.json()
    assert [doc["document_id"] for doc in docs] == ["doc-a"]
    assert all("企业B政策" not in doc["title"] for doc in docs)
    # never serialize raw document bodies
    assert not any("raw_text" in doc for doc in docs)


def test_document_detail_returns_versions_and_latest_job(client_and_service):
    client, service = client_and_service(role=MembershipRole.ADMIN)
    service.store.add_document(
        make_document("doc-a", ORG_A, USER_A, title="企业A政策",
                      active_version_id="version-2")
    )
    service.store.add_version(
        make_version("version-1", ORG_A, "doc-a", version_no=1, content_hash="sha256:one")
    )
    service.store.add_version(
        make_version("version-2", ORG_A, "doc-a", version_no=2, content_hash="sha256:two")
    )
    service.store.add_job(
        make_job("job-2", ORG_A, "doc-a", "version-2")
    )

    response = client.get("/knowledge/documents/doc-a/")

    assert response.status_code == 200
    body = response.json()
    assert body["document_id"] == "doc-a"
    assert body["title"] == "企业A政策"
    assert [v["version_no"] for v in body["versions"]] == [1, 2]
    # raw_text must never leak on any version
    assert not any("raw_text" in version for version in body["versions"])
    assert body["latest_job"]["job_id"] == "job-2"
    assert body["latest_job"]["status"] == "succeeded"


def test_detail_cross_tenant_is_404_without_title_leak(client_and_service):
    client, service = client_and_service(role=MembershipRole.ADMIN)
    service.store.add_document(
        make_document("doc-b", ORG_B, USER_B, title="企业B机密标题")
    )

    response = client.get("/knowledge/documents/doc-b/")

    assert response.status_code == 404
    assert "企业B机密标题" not in response.text
    assert response.json()["detail"]["code"] == "DOCUMENT_NOT_FOUND"


def test_list_jobs_returns_current_org_job(client_and_service):
    client, service = client_and_service(role=MembershipRole.ADMIN)
    service.store.add_document(
        make_document("doc-a", ORG_A, USER_A, title="企业A政策",
                      active_version_id="version-1")
    )
    service.store.add_job(make_job("job-1", ORG_A, "doc-a", "version-1"))

    response = client.get("/knowledge/ingestion-jobs/job-1/")

    assert response.status_code == 200
    assert response.json()["job_id"] == "job-1"
    assert response.json()["error_code"] is None


def test_list_jobs_cross_tenant_is_404(client_and_service):
    client, service = client_and_service(role=MembershipRole.ADMIN)
    service.store.add_document(
        make_document("doc-b", ORG_B, USER_B, title="企业B")
    )
    service.store.add_job(make_job("job-b", ORG_B, "doc-b", "version-b"))

    response = client.get("/knowledge/ingestion-jobs/job-b/")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "INGESTION_JOB_NOT_FOUND"


def test_admin_uploads_new_version(client_and_service):
    client, service = client_and_service(role=MembershipRole.ADMIN)
    response = client.post(
        "/knowledge/documents/doc-1/versions/",
        data={"title": "v2"},
        files={"file": ("v2.md", b"# Two", "text/markdown")},
    )

    assert response.status_code == 201
    assert service.new_version_calls[0]["organization_id"] == ORG_A
    assert service.new_version_calls[0]["document_id"] == "doc-1"
    assert service.new_version_calls[0]["content"] == b"# Two"


def test_agent_cannot_upload_new_version(client_and_service):
    client, _ = client_and_service(role=MembershipRole.AGENT)
    response = client.post(
        "/knowledge/documents/doc-1/versions/",
        data={"title": "v2"},
        files={"file": ("v2.md", b"# Two", "text/markdown")},
    )
    assert response.status_code == 403


def test_disable_and_enable_require_admin_and_return_200(client_and_service):
    client, service = client_and_service(role=MembershipRole.ADMIN)
    service.store.add_document(
        make_document("doc-a", ORG_A, USER_A, title="企业A政策")
    )

    disable = client.post("/knowledge/documents/doc-a/disable/")
    enable = client.post("/knowledge/documents/doc-a/enable/")

    assert disable.status_code == 200
    assert disable.json()["status"] == "disabled"
    assert enable.status_code == 200
    assert enable.json()["status"] == "active"
    assert [call["status"] for call in service.store.status_calls] == [
        DocumentStatus.DISABLED,
        DocumentStatus.ACTIVE,
    ]


def test_disable_requires_admin(client_and_service):
    client, _ = client_and_service(role=MembershipRole.AGENT)
    assert client.post("/knowledge/documents/doc-a/disable/").status_code == 403
    assert client.post("/knowledge/documents/doc-a/enable/").status_code == 403


def test_extensions_and_empty_body_mapped_to_invalid_document(client_and_service):
    client, service = client_and_service(role=MembershipRole.ADMIN)

    pdf = client.post(
        "/knowledge/documents/",
        data={"title": "t"},
        files={"file": ("doc.pdf", b"%PDF", "application/pdf")},
    )
    no_ext = client.post(
        "/knowledge/documents/",
        data={"title": "t"},
        files={"file": ("doc", b"body")},
    )
    empty = client.post(
        "/knowledge/documents/",
        data={"title": "t"},
        files={"file": ("e.md", b"", "text/markdown")},
    )
    bad_utf8 = client.post(
        "/knowledge/documents/",
        data={"title": "t"},
        files={"file": ("e.md", b"\xff\xfe", "text/markdown")},
    )

    for response in (pdf, no_ext, empty, bad_utf8):
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "INVALID_DOCUMENT"
    assert service.new_document_calls == []


def test_file_over_max_bytes_is_invalid_document(client_and_service):
    from app.knowledge.document_loader import MAX_DOCUMENT_BYTES

    client, service = client_and_service(role=MembershipRole.ADMIN)
    response = client.post(
        "/knowledge/documents/",
        data={"title": "t"},
        files={"file": ("big.md", b"x" * (MAX_DOCUMENT_BYTES + 1), "text/markdown")},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INVALID_DOCUMENT"
    assert service.new_document_calls == []


def test_extra_organization_id_field_is_rejected(client_and_service):
    client, service = client_and_service(role=MembershipRole.ADMIN)

    response = client.post(
        "/knowledge/documents/",
        data={"title": "t", "organization_id": "org-someone-else"},
        files={"file": ("d.md", b"# R", "text/markdown")},
    )

    assert response.status_code == 422
    assert service.new_document_calls == []
