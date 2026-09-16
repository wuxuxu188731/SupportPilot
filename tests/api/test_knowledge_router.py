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
    ParsingUnavailableError,
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

    Emulates the real loader's content checks so the tests exercise the
    router's 422 mapping for empty or non-UTF-8 bodies, exactly as the
    production loader would raise ``InvalidDocumentError``.

    DOCX/PDF 走外部解析，真实 loader 对它们**不做** UTF-8 解码，所以这里
    必须按来源类型分支——否则会误以为二进制文档「编码非法」。

    路由用的是 ``queue_new_document`` / ``queue_new_version``（登记入队，
    立刻返回 queued），因此这两个方法才是被路由调用的入口；断言里仍然检查
    它们收到的参数，与改造前的 ``ingest_*`` 完全一致。
    """

    def __init__(self) -> None:
        self.new_document_calls: list[dict] = []
        self.new_version_calls: list[dict] = []
        # 非 None 时 ingest 直接抛出该异常，用于验证基础设施故障的 HTTP 映射。
        self.raise_on_ingest: Exception | None = None
        self.receipt = IngestionReceipt(
            document_id="doc-upload",
            version_id="version-upload",
            job_id="job-upload",
            status=IngestionStatus.QUEUED,
            deduplicated=False,
        )

    def _load(self, content: bytes, source_type: DocumentSourceType) -> None:
        if source_type in (DocumentSourceType.WORD, DocumentSourceType.PDF):
            if not content.strip():
                raise InvalidDocumentError(reason="document is empty")
            return
        try:
            text = content.decode("utf-8")
        except (UnicodeDecodeError, UnicodeError):
            raise InvalidDocumentError(reason="document is not valid utf-8")
        if not text.strip():
            raise InvalidDocumentError(reason="document is empty")

    def queue_new_document(self, **kwargs) -> IngestionReceipt:
        self._load(kwargs["content"], kwargs["source_type"])
        if self.raise_on_ingest is not None:
            raise self.raise_on_ingest
        self.new_document_calls.append(kwargs)
        return self.receipt

    def queue_new_version(self, **kwargs) -> IngestionReceipt:
        self._load(kwargs["content"], kwargs["source_type"])
        if self.raise_on_ingest is not None:
            raise self.raise_on_ingest
        self.new_version_calls.append(kwargs)
        return self.receipt


class FakeKnowledgeStore:
    """内存版、按租户隔离的知识库 store，镜像路由实际用到的那部分读接口：
    ``list_documents``、``get_document``、``list_versions``、``get_version_by_id``、
    最近任务查询与 ``set_document_status``。

    隔离口径与真实 SQLite store 一致：id 不存在、或属于其它企业时统一抛
    ``DocumentNotFoundError``，测试因此能验证"跨租户读不到"而不是"恰好没数据"。
    """

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

    def get_version_by_id(
        self,
        *,
        organization_id: str,
        document_id: str,
        version_id: str,
    ):
        """按 (企业, 文档, 版本) 三重限定取版本，与真实 store 的 WHERE 条件一一对应。

        这三重校验缺一不可：``version_id`` 与 ``document_id`` 是 URL 上两个独立的
        路径参数，客户端可以把它们自由组合（拿 A 企业的版本 id 配 B 企业的文档 id，
        或配同企业另一个文档的 id）。少了 ``organization_id``/``document_id`` 任一
        条件，测试就会在"跨租户/跨文档读取"上假通过。
        """
        version = self._versions.get(version_id)
        if (
            version is None
            or version.organization_id != organization_id
            or version.document_id != document_id
        ):
            raise DocumentNotFoundError(organization_id, document_id)
        return version

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
    raw_text: str | None = "secret body must never appear in a response",
) -> DocumentVersion:
    return DocumentVersion(
        version_id=version_id,
        organization_id=org_id,
        document_id=document_id,
        version_no=version_no,
        content_hash=content_hash,
        source_hash=None,
        # 默认值是"绝不允许出现在响应里的哨兵正文"：详情/版本接口的防泄漏断言
        # 依赖它。只有正文接口的用例才显式传入真实正文。
        # 显式传 None 用来模拟"版本已预留但尚未解析"（异步入库中间态）。
        raw_text=raw_text,
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
            "status": "queued",
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
    # 保护行为：未知/缺失扩展名、空文件、非 UTF-8 文本一律 422。
    # 注意 docx/pdf 已在本轮放开（见下方专门用例），不再属于「非法扩展名」。
    client, service = client_and_service(role=MembershipRole.ADMIN)

    pptx = client.post(
        "/knowledge/documents/",
        data={"title": "t"},
        files={
            "file": (
                "deck.pptx",
                b"PK\x03\x04",
                "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            )
        },
    )
    legacy_doc = client.post(
        "/knowledge/documents/",
        data={"title": "t"},
        files={"file": ("legacy.doc", b"\xd0\xcf\x11\xe0", "application/msword")},
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

    for response in (pptx, legacy_doc, no_ext, empty, bad_utf8):
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "INVALID_DOCUMENT"


def test_docx_and_pdf_uploads_are_accepted(client_and_service):
    # 保护行为：.docx / .pdf 必须被放行并映射到正确的来源类型，
    # 且**不得**被当成非 UTF-8 文本拒绝——这两种格式是二进制，
    # 真实 loader 对它们不做本地解码。
    client, service = client_and_service(role=MembershipRole.ADMIN)

    docx = client.post(
        "/knowledge/documents/",
        data={"title": "Word 文档"},
        files={"file": ("policy.docx", b"PK\x03\x04\xff\xfe\x00binary", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    pdf = client.post(
        "/knowledge/documents/",
        data={"title": "PDF 文档"},
        files={"file": ("policy.pdf", b"%PDF-1.7\n%\xff\xfe\x00binary", "application/pdf")},
    )

    assert docx.status_code == 201, docx.text
    assert pdf.status_code == 201, pdf.text
    assert [call["source_type"] for call in service.new_document_calls] == [
        DocumentSourceType.WORD,
        DocumentSourceType.PDF,
    ]


def test_docx_and_pdf_version_uploads_are_accepted(client_and_service):
    # 保护行为：已有文档上传新版本时（upload_document_version 路径）
    # .pdf 也必须被放行——这条分支与「新文档」是两段独立代码。
    client, service = client_and_service(role=MembershipRole.ADMIN)

    response = client.post(
        "/knowledge/documents/doc-a/versions/",
        data={"title": "PDF 文档"},
        files={"file": ("policy.pdf", b"%PDF-1.7\n%\xff\xfe\x00binary", "application/pdf")},
    )

    assert response.status_code == 201, response.text
    assert service.new_version_calls[0]["source_type"] is DocumentSourceType.PDF


def test_parsing_unavailable_is_not_reported_as_invalid_document(client_and_service):
    # 保护行为：解析服务不可用绝不能被映射成 422 INVALID_DOCUMENT ——
    # 那等于拿「文档非法」指责用户传了坏文件。
    #
    # 入库异步化后上传接口不再等待解析，因此这类失败不再以 HTTP 状态码表达，
    # 而是**只在任务记录上**以稳定错误码出现（见 tests/knowledge/test_ingestion.py
    # 的 run_job 用例与 tests/knowledge/test_ingestion_worker.py 的端到端用例）。
    # 这里断言的是接口层不再假装「文档非法」：既不 422，也不会把上传判死。
    client, service = client_and_service(role=MembershipRole.ADMIN)
    service.raise_on_ingest = ParsingUnavailableError(reason="llamaparse timeout")

    response = client.post(
        "/knowledge/documents/",
        data={"title": "PDF 文档"},
        files={"file": ("policy.pdf", b"%PDF-1.7", "application/pdf")},
    )

    assert response.status_code != 422
    detail = response.json().get("detail") if response.status_code >= 400 else None
    assert detail is None or detail.get("code") != "INVALID_DOCUMENT"


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


# ------------------------------------------- 正文读取接口（content endpoint）

# 详情/版本接口必须继续隐藏正文，这里用哨兵值断言"没有泄漏"。
SENTINEL_RAW_TEXT = "secret body must never appear in a response"

# 真实感正文：既覆盖中文多字节，也覆盖两级标题（用于目录偏移断言）。
CONTENT_TEXT = "# 退货总则\n\n本店支持七天无理由退货。\n\n## 3.2 退货流程\n\n签收后 7 日内可申请退货。"


def _seed_content_document(
    service,
    *,
    document_id: str = "doc-a",
    version_id: str = "version-1",
    org_id: str = ORG_A,
    user_id: str = USER_A,
    title: str = "企业A政策",
    source_type: DocumentSourceType = DocumentSourceType.MARKDOWN,
    status: DocumentStatus = DocumentStatus.ACTIVE,
    active_version_id: str | None = "version-1",
    raw_text: str | None = CONTENT_TEXT,
) -> None:
    """播撒"一个文档 + 一个版本"，供正文接口用例复用。

    ``raw_text`` 显式传 None 用来模拟异步入库的中间态（版本已预留、正文未回填）。
    """
    service.store.add_document(
        make_document(
            document_id,
            org_id,
            user_id,
            title=title,
            source_type=source_type,
            status=status,
            active_version_id=active_version_id,
        )
    )
    service.store.add_version(
        make_version(
            version_id,
            org_id,
            document_id,
            version_no=1,
            content_hash="sha256:content",
            raw_text=raw_text,
        )
    )


def test_content_endpoint_returns_markdown_and_outline_for_member(client_and_service):
    # 保护行为：普通成员（agent）可读正文；响应含归一化 Markdown、字符长度与
    # 标题目录（含每个标题的行首字符偏移），且 offset 按 Python 字符计数
    # ——中文一个字算 1（不是 utf-8 的 3 字节），前端据此做偏移越界自检。
    client, service = client_and_service(role=MembershipRole.AGENT)
    _seed_content_document(service)

    response = client.get("/knowledge/documents/doc-a/versions/version-1/content/")

    assert response.status_code == 200
    body = response.json()
    assert body["document_id"] == "doc-a"
    assert body["version_id"] == "version-1"
    assert body["version_no"] == 1
    assert body["title"] == "企业A政策"
    assert body["source_type"] == "markdown"
    assert body["status"] == "active"
    assert body["active_version_id"] == "version-1"
    assert body["loader_version"] == "loader-v1"
    assert body["chunker_version"] == "chunker-v1"
    assert body["content_hash"] == "sha256:content"
    assert body["text"] == CONTENT_TEXT
    assert body["text_length"] == len(CONTENT_TEXT)
    assert body["outline"] == [
        {
            "level": 1,
            "title": "退货总则",
            "heading_path": "退货总则",
            "char_offset": 0,
        },
        {
            "level": 2,
            "title": "3.2 退货流程",
            "heading_path": "退货总则/3.2 退货流程",
            "char_offset": CONTENT_TEXT.index("## 3.2 退货流程"),
        },
    ]


def test_content_endpoint_converted_pdf_reports_source_type_and_text(client_and_service):
    # 保护行为：PDF/Word 文档的"正文"就是入库时转换出的 Markdown，
    # 来源类型如实上报，前端据此提示"与原文排版可能不一致"。
    client, service = client_and_service(role=MembershipRole.AGENT)
    _seed_content_document(
        service,
        source_type=DocumentSourceType.PDF,
        title="售后服务总则",
    )

    response = client.get("/knowledge/documents/doc-a/versions/version-1/content/")

    assert response.status_code == 200
    body = response.json()
    assert body["source_type"] == "pdf"
    assert body["text"] == CONTENT_TEXT


def test_content_endpoint_is_404_for_other_organization_document(client_and_service):
    # 边界情况（跨租户）：企业 B 的文档正文不能被企业 A 的成员读到，
    # 且 404 响应里不得出现 B 企业的标题或正文——与"不存在"完全同一口径。
    client, service = client_and_service(role=MembershipRole.AGENT)
    _seed_content_document(
        service,
        document_id="doc-b",
        version_id="version-b",
        org_id=ORG_B,
        user_id=USER_B,
        title="企业B机密标题",
        raw_text="# 企业B机密正文",
    )

    response = client.get("/knowledge/documents/doc-b/versions/version-b/content/")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "DOCUMENT_NOT_FOUND"
    assert "企业B机密标题" not in response.text
    assert "企业B机密正文" not in response.text


def test_content_endpoint_is_404_for_other_organization_version_id(client_and_service):
    # 边界情况（跨租户 + 混搭 id）：用本企业自己的 document_id 配其它企业的
    # version_id。document_id 与 version_id 是 URL 上两个独立参数，客户端可以
    # 自由组合，因此版本查询必须同时限定企业与文档。
    client, service = client_and_service(role=MembershipRole.AGENT)
    _seed_content_document(service, document_id="doc-a", version_id="version-a")
    _seed_content_document(
        service,
        document_id="doc-b",
        version_id="version-b",
        org_id=ORG_B,
        user_id=USER_B,
        title="企业B政策",
        raw_text="# 企业B正文",
    )

    response = client.get("/knowledge/documents/doc-a/versions/version-b/content/")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "DOCUMENT_NOT_FOUND"
    assert "企业B正文" not in response.text


def test_content_endpoint_is_404_for_version_of_another_document(client_and_service):
    # 边界情况（跨文档）：同企业内，用 doc-a 的 document_id 去读 doc-b 的版本，
    # 同样必须 404——否则四层 id 校验形同虚设。
    client, service = client_and_service(role=MembershipRole.AGENT)
    _seed_content_document(service, document_id="doc-a", version_id="version-a")
    _seed_content_document(
        service,
        document_id="doc-b",
        version_id="version-b",
        title="另一个文档",
        raw_text="# 另一个文档的正文",
    )

    response = client.get("/knowledge/documents/doc-a/versions/version-b/content/")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "DOCUMENT_NOT_FOUND"


def test_content_endpoint_is_404_when_version_has_no_parsed_text(client_and_service):
    # 边界情况（异步入库中间态）：版本行已预留但正文尚未回填（raw_text 为 NULL）。
    # 必须 404，绝不能返回空正文冒充成功——那会让用户以为文档本来就是空的。
    client, service = client_and_service(role=MembershipRole.AGENT)
    _seed_content_document(service, raw_text=None)

    response = client.get("/knowledge/documents/doc-a/versions/version-1/content/")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "DOCUMENT_NOT_FOUND"
    # 连带要求：正文缺失时既不能返回空正文，也不能把元数据里的正文带出来。
    assert SENTINEL_RAW_TEXT not in response.text


def test_content_endpoint_reads_disabled_document(client_and_service):
    # 保护行为：文档被停用只影响"新的知识检索"，不影响查看正文——
    # 历史引用可能正指向这份已停用文档。
    client, service = client_and_service(role=MembershipRole.AGENT)
    _seed_content_document(service, status=DocumentStatus.DISABLED)

    response = client.get("/knowledge/documents/doc-a/versions/version-1/content/")

    assert response.status_code == 200
    assert response.json()["status"] == "disabled"
    assert response.json()["text"] == CONTENT_TEXT


def test_content_endpoint_reads_historical_version_with_active_version_id(
    client_and_service,
):
    # 保护行为：引用可能来自历史版本，因此正文接口必须能按 version_id 读非
    # 有效版本，并在响应里同时给出 active_version_id，供前端提示
    # "该引用来自历史版本 vN，当前有效版本为 vM"。
    client, service = client_and_service(role=MembershipRole.AGENT)
    _seed_content_document(service, document_id="doc-a", version_id="version-old")
    service.store.add_document(
        make_document(
            "doc-a",
            ORG_A,
            USER_A,
            title="企业A政策",
            active_version_id="version-new",
        )
    )
    service.store.add_version(
        make_version(
            "version-new",
            ORG_A,
            "doc-a",
            version_no=2,
            content_hash="sha256:new",
            raw_text="# 新版本正文",
        )
    )

    response = client.get("/knowledge/documents/doc-a/versions/version-old/content/")

    assert response.status_code == 200
    body = response.json()
    assert body["version_id"] == "version-old"
    assert body["version_no"] == 1
    assert body["active_version_id"] == "version-new"
    assert body["text"] == CONTENT_TEXT
    # 当前有效版本的新正文不得出现在历史版本的响应里。
    assert "新版本正文" not in response.text


def test_content_endpoint_does_not_leak_other_versions_of_same_document(
    client_and_service,
):
    # 边界情况：同一文档的其它版本正文（默认哨兵值）不得被顺带带出。
    client, service = client_and_service(role=MembershipRole.AGENT)
    _seed_content_document(service, document_id="doc-a", version_id="version-1")
    service.store.add_version(
        make_version(
            "version-2", ORG_A, "doc-a", version_no=2, content_hash="sha256:two"
        )
    )

    response = client.get("/knowledge/documents/doc-a/versions/version-1/content/")

    assert response.status_code == 200
    assert response.json()["text"] == CONTENT_TEXT
    assert SENTINEL_RAW_TEXT not in response.text


def test_content_endpoint_is_404_for_unknown_document_or_version(client_and_service):
    # 边界情况：文档或版本完全不存在时都是 404（与跨租户同一口径，
    # 不区分"不存在"和"不属于你"）。
    client, service = client_and_service(role=MembershipRole.AGENT)
    _seed_content_document(service, document_id="doc-a", version_id="version-1")

    missing_document = client.get(
        "/knowledge/documents/doc-missing/versions/version-1/content/"
    )
    missing_version = client.get(
        "/knowledge/documents/doc-a/versions/version-missing/content/"
    )

    assert missing_document.status_code == 404
    assert missing_version.status_code == 404
    assert missing_document.json()["detail"]["code"] == "DOCUMENT_NOT_FOUND"
    assert missing_version.json()["detail"]["code"] == "DOCUMENT_NOT_FOUND"
