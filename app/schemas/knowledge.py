"""HTTP response models for the tenant-scoped knowledge management API.

All models use ``extra="forbid"`` so an unexpected payload key is rejected
instead of silently dropped. Raw document bodies and raw log/traceback detail
are deliberately excluded: ``DocumentVersionResponse`` never carries
``raw_text`` and ``IngestionJobResponse`` only exposes the stable safe
``error_code``/``error_message`` pinned by the ingestion service (never the
underlying traceback or provider transport detail).
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
