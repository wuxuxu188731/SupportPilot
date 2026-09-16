"""Tenant-scoped knowledge document management HTTP API.

Router factory consumes only trusted dependencies: ``KnowledgeIngestionService``
(ideally our orchestration), ``KnowledgeStore`` (read/status surface) and a
``get_current_tenant`` dependency that yields a trusted ``TenantContext``. The
``organization_id`` in every request/response path comes exclusively from that
context — never from the multipart body, so a client cannot submit a
``organization_id`` field to choose a scope.

Write operations (upload, new version, disable, enable) first check the caller
is an admin. Reads are tenant-scoped via the context; a document/job id that
belongs to another organization surfaces as a generic 404 with no cross-tenant
title leak.

Error responses carry only a stable machine-readable ``code`` and a safe
``message`` (from ``KnowledgeError``), never raw document bodies or tracebacks.
"""

from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, Request

from app.application.organization_service import TenantContext
from app.knowledge.base import (
    DocumentDisabledError,
    DocumentNotFoundError,
    DocumentSourceType,
    DocumentStatus,
    InvalidDocumentError,
    KnowledgeError,
    KnowledgeStore,
)
from app.knowledge.document_loader import MAX_DOCUMENT_BYTES
from app.knowledge.ingestion import KnowledgeIngestionService
from app.knowledge.outline import extract_outline
from app.organizations.base import MembershipRole
from app.schemas.knowledge import (
    DocumentContentResponse,
    IngestionJobResponse,
    IngestionReceiptResponse,
    KnowledgeDocumentDetailResponse,
    KnowledgeDocumentSummaryResponse,
)

# Stable HTTP status per knowledge error code (brief Step 5 fixed mapping).
# ``safe_message`` from ``KnowledgeError`` is safe to expose verbatim.
_STATUS_BY_CODE = {
    "DOCUMENT_NOT_FOUND": 404,
    "INVALID_DOCUMENT": 422,
    "DOCUMENT_DISABLED": 409,
    "DUPLICATE_DOCUMENT_VERSION": 409,
}
# Ingestion infrastructure failure: embedding / vector store / generic pipeline
# failures after rows exist are server-side (503), never leaked as 'no evidence'.
_STATUS_BY_CODE_DEFAULT = 503


def _error_payload(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _require_admin(context: TenantContext) -> None:
    if context.role is not MembershipRole.ADMIN:
        raise HTTPException(status_code=403, detail="admin role required")


def _knowledge_error_response(exc: KnowledgeError) -> HTTPException:
    status = _STATUS_BY_CODE.get(exc.code, _STATUS_BY_CODE_DEFAULT)
    return HTTPException(
        status_code=status,
        detail=_error_payload(exc.code, exc.safe_message),
    )


def _extract_source_type(filename: str) -> DocumentSourceType:
    """按扩展名映射来源类型；未知或缺失扩展名一律 422。

    放行的扩展名必须与 ``DocumentLoader`` 实际支持的来源类型保持一致：
    Markdown/TXT 本地解码，DOCX/PDF 交给外部解析服务提取。
    """
    lower = (filename or "").lower()
    if lower.endswith(".md") or lower.endswith(".markdown"):
        return DocumentSourceType.MARKDOWN
    if lower.endswith(".txt"):
        return DocumentSourceType.TEXT
    if lower.endswith(".docx"):
        return DocumentSourceType.WORD
    if lower.endswith(".pdf"):
        return DocumentSourceType.PDF
    # Empty (no extension) and unknown types both 422. 旧的 .doc（非 OOXML）
    # 与 .pptx 等不在支持范围内，不给「猜格式」的余地。
    raise HTTPException(
        status_code=422,
        detail=_error_payload(
            "INVALID_DOCUMENT",
            "unsupported file type; use .md/.markdown, .txt, .docx or .pdf",
        ),
    )


async def _read_upload_named_fields(request: Request) -> tuple[str, bytes, DocumentSourceType]:
    """Validate the multipart form is EXACTLY {title, file} and return the
    title, at most MAX_DOCUMENT_BYTES bytes, and the mapped source type.

    An extra field (e.g. a client-supplied ``organization_id``) yields 422 and
    is never silently ignored or used for scoping.
    """
    form = await request.form()
    field_names = {name for name, _ in form.multi_items()}
    if field_names != {"title", "file"}:
        raise HTTPException(
            status_code=422,
            detail=_error_payload(
                "INVALID_DOCUMENT",
                "multipart form must contain exactly the fields 'title' and 'file'",
            ),
        )
    title = form["title"]
    upload = form["file"]
    source_type = _extract_source_type(upload.filename or "")
    content = await upload.read(MAX_DOCUMENT_BYTES + 1)
    if len(content) > MAX_DOCUMENT_BYTES:
        raise HTTPException(
            status_code=422,
            detail=_error_payload(
                "INVALID_DOCUMENT", "document exceeds maximum size"
            ),
        )
    return title, content, source_type


def create_knowledge_router(
    *,
    ingestion_service: KnowledgeIngestionService,
    knowledge_store: KnowledgeStore,
    get_current_tenant: Callable[..., TenantContext],
) -> APIRouter:
    router = APIRouter(
        prefix="/knowledge",
        tags=["knowledge"],
    )

    @router.post(
        "/documents/",
        response_model=IngestionReceiptResponse,
        status_code=201,
    )
    async def upload_document(
        request: Request,
        tenant: TenantContext = Depends(get_current_tenant),
    ) -> IngestionReceiptResponse:
        """上传新文档：登记并入队，立刻返回 ``queued``。

        解析（DOCX/PDF 走外部服务，实测约 27 秒）不再发生在这个请求里，
        否则会阻塞事件循环并让大文档撞上前端上传超时。接口只做三件事：
        校验、落库（document + 预留版本 + queued 任务 + 暂存原始字节）、入队。
        前端已按 ``queued`` 回执启动任务轮询，由
        ``GET /knowledge/ingestion-jobs/{job_id}/`` 推进到终态。
        """
        _require_admin(tenant)
        title, content, source_type = await _read_upload_named_fields(request)
        try:
            receipt = ingestion_service.queue_new_document(
                organization_id=tenant.organization_id,
                uploaded_by_user_id=tenant.user_id,
                title=title,
                source_type=source_type,
                content=content,
            )
        except InvalidDocumentError as exc:
            raise HTTPException(
                status_code=422,
                detail=_error_payload(exc.code, exc.safe_message),
            ) from exc
        except KnowledgeError as exc:
            raise _knowledge_error_response(exc) from exc
        return IngestionReceiptResponse.from_receipt(receipt)

    @router.post(
        "/documents/{document_id}/versions/",
        response_model=IngestionReceiptResponse,
        status_code=201,
    )
    async def upload_document_version(
        document_id: str,
        request: Request,
        tenant: TenantContext = Depends(get_current_tenant),
    ) -> IngestionReceiptResponse:
        """上传新版本：与新建文档同样只登记入队并返回 ``queued``。"""
        _require_admin(tenant)
        title, content, source_type = await _read_upload_named_fields(request)
        try:
            receipt = ingestion_service.queue_new_version(
                organization_id=tenant.organization_id,
                uploaded_by_user_id=tenant.user_id,
                document_id=document_id,
                source_type=source_type,
                content=content,
            )
        except (DocumentNotFoundError, InvalidDocumentError) as exc:
            raise HTTPException(
                status_code=422 if isinstance(exc, InvalidDocumentError) else 404,
                detail=_error_payload(exc.code, exc.safe_message),
            ) from exc
        except KnowledgeError as exc:
            raise _knowledge_error_response(exc) from exc
        return IngestionReceiptResponse.from_receipt(receipt)

    @router.get(
        "/documents/",
        response_model=list[KnowledgeDocumentSummaryResponse],
    )
    def list_documents(
        tenant: TenantContext = Depends(get_current_tenant),
    ) -> list[KnowledgeDocumentSummaryResponse]:
        documents = knowledge_store.list_documents(
            organization_id=tenant.organization_id,
        )
        return [
            KnowledgeDocumentSummaryResponse.from_document(document)
            for document in documents
        ]

    @router.get(
        "/documents/{document_id}/",
        response_model=KnowledgeDocumentDetailResponse,
    )
    def get_document_detail(
        document_id: str,
        tenant: TenantContext = Depends(get_current_tenant),
    ) -> KnowledgeDocumentDetailResponse:
        try:
            document = knowledge_store.get_document(
                organization_id=tenant.organization_id,
                document_id=document_id,
            )
        except DocumentNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail=_error_payload(exc.code, exc.safe_message),
            ) from exc
        versions = knowledge_store.list_versions(
            organization_id=tenant.organization_id,
            document_id=document_id,
        )
        latest_job = None
        if document.active_version_id is not None:
            latest_job = knowledge_store.get_latest_job_for_version(
                organization_id=tenant.organization_id,
                document_id=document_id,
                version_id=document.active_version_id,
            )
        return KnowledgeDocumentDetailResponse.from_document_components(
            document=document,
            versions=versions,
            latest_job=latest_job,
        )

    @router.get(
        "/documents/{document_id}/versions/{version_id}/content/",
        response_model=DocumentContentResponse,
    )
    def get_document_version_content(
        document_id: str,
        version_id: str,
        tenant: TenantContext = Depends(get_current_tenant),
    ) -> DocumentContentResponse:
        """读取指定版本归一化 Markdown 正文（成员可读，含历史版本）。

        为什么必须显式带 ``version_id``：知识块的 ``start_offset``/``end_offset``
        是相对**某一个版本**正文的字符偏移，版本一换偏移就失效，因此跳转必须绑定
        版本，不能隐式用"当前有效版本"。

        为什么不需要重新解析：``raw_text`` 就是入库时解析/归一化的那份 Markdown
        （PDF/Word 已在入库阶段转换完成），切块与引用跳转都以它为准。

        错误口径与其它读接口一致：文档不存在、跨租户、版本不属于该文档、
        以及版本尚未解析出正文（``raw_text`` 为 NULL）一律 404，不区分原因，
        也不返回空正文冒充成功。文档被停用（disabled）时正文仍可读——
        历史引用可能正指向它。
        """
        try:
            document = knowledge_store.get_document(
                organization_id=tenant.organization_id,
                document_id=document_id,
            )
            version = knowledge_store.get_version_by_id(
                organization_id=tenant.organization_id,
                document_id=document_id,
                version_id=version_id,
            )
        except DocumentNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail=_error_payload(exc.code, exc.safe_message),
            ) from exc
        if version.raw_text is None:
            # 异步入库的版本会先落库、后回填正文；此时没有可展示的内容。
            # 用与"不存在"相同的 404 口径，避免暴露"该 id 存在但还没解析"。
            raise HTTPException(
                status_code=404,
                detail=_error_payload(
                    "DOCUMENT_NOT_FOUND",
                    f"document {document_id} not found for organization "
                    f"{tenant.organization_id}",
                ),
            )
        return DocumentContentResponse.from_version_components(
            document=document,
            version=version,
            text=version.raw_text,
            outline=extract_outline(version.raw_text),
        )

    @router.post(
        "/documents/{document_id}/disable/",
        response_model=KnowledgeDocumentSummaryResponse,
        status_code=200,
    )
    def disable_document(
        document_id: str,
        tenant: TenantContext = Depends(get_current_tenant),
    ) -> KnowledgeDocumentSummaryResponse:
        _require_admin(tenant)
        try:
            document = knowledge_store.set_document_status(
                organization_id=tenant.organization_id,
                document_id=document_id,
                status=DocumentStatus.DISABLED,
            )
        except DocumentDisabledError as exc:
            raise HTTPException(
                status_code=409,
                detail=_error_payload(exc.code, exc.safe_message),
            ) from exc
        except DocumentNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail=_error_payload(exc.code, exc.safe_message),
            ) from exc
        return KnowledgeDocumentSummaryResponse.from_document(document)

    @router.post(
        "/documents/{document_id}/enable/",
        response_model=KnowledgeDocumentSummaryResponse,
        status_code=200,
    )
    def enable_document(
        document_id: str,
        tenant: TenantContext = Depends(get_current_tenant),
    ) -> KnowledgeDocumentSummaryResponse:
        _require_admin(tenant)
        try:
            document = knowledge_store.set_document_status(
                organization_id=tenant.organization_id,
                document_id=document_id,
                status=DocumentStatus.ACTIVE,
            )
        except DocumentNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail=_error_payload(exc.code, exc.safe_message),
            ) from exc
        return KnowledgeDocumentSummaryResponse.from_document(document)

    @router.get(
        "/ingestion-jobs/{job_id}/",
        response_model=IngestionJobResponse,
    )
    def get_ingestion_job(
        job_id: str,
        tenant: TenantContext = Depends(get_current_tenant),
    ) -> IngestionJobResponse:
        # The store's job-by-id lookup is scoped by organization_id; a missing
        # or cross-tenant job is indistinguishable and yields a generic 404.
        job = knowledge_store.get_latest_job_for_version(
            organization_id=tenant.organization_id,
            document_id="",
            version_id="",
            job_id=job_id,
        )
        if job is None:
            raise HTTPException(
                status_code=404,
                detail=_error_payload(
                    "INGESTION_JOB_NOT_FOUND",
                    "ingestion job not found",
                ),
            )
        return IngestionJobResponse.from_job(job)

    return router
