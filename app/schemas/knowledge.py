"""HTTP response models for the tenant-scoped knowledge management API.

All models use ``extra="forbid"`` so an unexpected payload key is rejected
instead of silently dropped. Raw document bodies and raw log/traceback detail
are deliberately excluded: ``DocumentVersionResponse`` never carries
``raw_text`` and ``IngestionJobResponse`` only exposes the stable safe
``error_code``/``error_message`` pinned by the ingestion service (never the
underlying traceback or provider transport detail).

唯一例外是 :class:`DocumentContentResponse`：它是专门的「查看正文」接口响应，
**按客户端显式指定的版本**返回归一化 Markdown 正文。正文只允许出现在这个模型里，
详情/版本列表响应继续保持不含 ``raw_text``。
"""

from pydantic import BaseModel, ConfigDict

from app.knowledge.base import (
    DocumentSourceType,
    DocumentStatus,
    DocumentVersion,
    IngestionJob,
    IngestionStatus,
    KnowledgeDocument,
)
from app.knowledge.ingestion import IngestionReceipt
from app.knowledge.outline import DocumentOutlineItem


class IngestionReceiptResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str
    version_id: str
    job_id: str
    status: IngestionStatus
    deduplicated: bool

    @classmethod
    def from_receipt(cls, receipt: IngestionReceipt) -> "IngestionReceiptResponse":
        return cls(
            document_id=receipt.document_id,
            version_id=receipt.version_id,
            job_id=receipt.job_id,
            status=receipt.status,
            deduplicated=receipt.deduplicated,
        )


class DocumentVersionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version_id: str
    version_no: int
    # 解析正文的 sha256；入库异步化后版本行会先在上传接口里预留，此时正文尚未
    # 解析出来，因此该字段可能为 null（对应任务的 queued/running 阶段）。
    content_hash: str | None
    loader_version: str
    chunker_version: str
    embedding_model: str
    embedding_dimensions: int
    created_at: str

    @classmethod
    def from_version(cls, version: DocumentVersion) -> "DocumentVersionResponse":
        return cls(
            version_id=version.version_id,
            version_no=version.version_no,
            content_hash=version.content_hash,
            loader_version=version.loader_version,
            chunker_version=version.chunker_version,
            embedding_model=version.embedding_model,
            embedding_dimensions=version.embedding_dimensions,
            created_at=version.created_at,
        )


class IngestionJobResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    document_id: str
    version_id: str
    status: IngestionStatus
    attempt_count: int
    error_code: str | None
    error_message: str | None
    started_at: str | None
    finished_at: str | None
    created_at: str

    @classmethod
    def from_job(cls, job: IngestionJob) -> "IngestionJobResponse":
        return cls(
            job_id=job.job_id,
            document_id=job.document_id,
            version_id=job.version_id,
            status=job.status,
            attempt_count=job.attempt_count,
            error_code=job.error_code,
            error_message=job.error_message,
            started_at=job.started_at,
            finished_at=job.finished_at,
            created_at=job.created_at,
        )


class KnowledgeDocumentSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str
    title: str
    source_type: DocumentSourceType
    status: DocumentStatus
    active_version_id: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_document(
        cls, document: KnowledgeDocument
    ) -> "KnowledgeDocumentSummaryResponse":
        return cls(
            document_id=document.document_id,
            title=document.title,
            source_type=document.source_type,
            status=document.status,
            active_version_id=document.active_version_id,
            created_at=document.created_at,
            updated_at=document.updated_at,
        )


class KnowledgeDocumentDetailResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str
    title: str
    source_type: DocumentSourceType
    status: DocumentStatus
    active_version_id: str | None
    created_at: str
    updated_at: str
    versions: list[DocumentVersionResponse]
    latest_job: IngestionJobResponse | None

    @classmethod
    def from_document_components(
        cls,
        document: KnowledgeDocument,
        versions: list[DocumentVersion],
        latest_job: IngestionJob | None,
    ) -> "KnowledgeDocumentDetailResponse":
        return cls(
            document_id=document.document_id,
            title=document.title,
            source_type=document.source_type,
            status=document.status,
            active_version_id=document.active_version_id,
            created_at=document.created_at,
            updated_at=document.updated_at,
            versions=[
                DocumentVersionResponse.from_version(version) for version in versions
            ],
            latest_job=(
                IngestionJobResponse.from_job(latest_job) if latest_job is not None else None
            ),
        )


class DocumentOutlineItemResponse(BaseModel):
    """正文目录项：一个标题及其在正文中的字符偏移。"""

    model_config = ConfigDict(extra="forbid")

    # 标题层级：1-6，对应 Markdown '#' 的个数
    level: int
    # 标题文本
    title: str
    # 完整标题路径（含本标题自身），与知识块 heading_path 同一口径
    heading_path: str
    # 该标题行首在正文中的绝对字符偏移（Python 字符计数）
    char_offset: int

    @classmethod
    def from_outline_item(
        cls, item: DocumentOutlineItem
    ) -> "DocumentOutlineItemResponse":
        return cls(
            level=item.level,
            title=item.title,
            heading_path=item.heading_path,
            char_offset=item.char_offset,
        )


class DocumentContentResponse(BaseModel):
    """单个文档版本的归一化 Markdown 正文。

    ``version_id`` 是客户端显式请求的版本：知识块的 ``start_offset``/``end_offset``
    相对某一个版本的正文，版本一变偏移就失效，因此正文接口必须按版本读取，
    不能只按「当前有效版本」隐式解析。

    这个模型是整套知识库接口里**唯一**允许携带正文的响应；``text`` 就是
    ``document_versions.raw_text``（PDF/Word 也已经是入库时转换好的 Markdown）。
    """

    model_config = ConfigDict(extra="forbid")

    # 文档标识
    document_id: str
    # 版本标识（本次返回正文的确切版本）
    version_id: str
    # 版本序号，前端用于「该引用来自历史版本 vN」这类提示
    version_no: int
    # 文档标题（服务端可信来源，取自 documents 表）
    title: str
    # 来源类型：markdown/text 本地解码，word/pdf 由解析服务提取后转成 Markdown
    source_type: DocumentSourceType
    # 文档当前状态；文档被停用（disabled）时正文仍可读（历史引用可能指向它）
    status: DocumentStatus
    # 文档当前有效版本；与 version_id 不同即说明本次读的是历史版本
    active_version_id: str | None
    # 入库时使用的解析器版本标识（可追溯性）
    loader_version: str
    # 入库时使用的分块器版本标识；偏移语义属于该版本
    chunker_version: str
    # 解析正文的 sha256；尚未解析的版本不会走到本响应（接口返回 404）
    content_hash: str | None
    # 归一化 Markdown 正文（UTF-8 解码后、行尾已统一为 \n）
    text: str
    # 正文字符数（Python 字符计数，不是 utf-8 字节数），前端据此做偏移越界自检
    text_length: int
    # 标题目录；没有标题的文档为空数组
    outline: list[DocumentOutlineItemResponse]

    @classmethod
    def from_version_components(
        cls,
        document: KnowledgeDocument,
        version: DocumentVersion,
        text: str,
        outline: tuple[DocumentOutlineItem, ...],
    ) -> "DocumentContentResponse":
        return cls(
            document_id=document.document_id,
            version_id=version.version_id,
            version_no=version.version_no,
            title=document.title,
            source_type=document.source_type,
            status=document.status,
            active_version_id=document.active_version_id,
            loader_version=version.loader_version,
            chunker_version=version.chunker_version,
            content_hash=version.content_hash,
            text=text,
            text_length=len(text),
            outline=[
                DocumentOutlineItemResponse.from_outline_item(item)
                for item in outline
            ],
        )
