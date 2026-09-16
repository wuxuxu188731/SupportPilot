from dataclasses import dataclass
from enum import Enum
from typing import Protocol, Sequence


class DocumentSourceType(str, Enum):
    """知识文档的来源格式。

    取值与 ``documents.source_type`` 的 CHECK 约束一一对应，新增取值
    必须同时补一个放开约束的迁移（见 migrations/versions/0012）。
    """

    MARKDOWN = "markdown"   # 项目原生 Markdown，本地 UTF-8 解码
    TEXT = "text"           # 纯文本，本地 UTF-8 解码
    WORD = "word"           # DOCX，经文档提取服务解析
    PDF = "pdf"             # PDF，经文档提取服务解析


class DocumentStatus(str, Enum):
    PROCESSING = "processing"
    ACTIVE = "active"
    DISABLED = "disabled"
    FAILED = "failed"


class IngestionStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True)
class KnowledgeDocument:
    document_id: str
    organization_id: str
    uploaded_by_user_id: str
    title: str
    source_type: DocumentSourceType
    status: DocumentStatus
    active_version_id: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class DocumentVersion:
    version_id: str
    organization_id: str
    document_id: str
    version_no: int
    # 解析正文的 sha256（形如 ``sha256:<hex>``）。入库异步化后版本行会先在
    # 上传接口里「预留」，此时正文还没解析出来，因此该字段在解析完成前为 None。
    content_hash: str | None
    # 上传文件的原始字节 sha256，上传时即可算出；承担上传期去重与 worker 取字节。
    # 历史版本行（0013 之前写入的）没有这个指纹，因此可能是 None。
    source_hash: str | None
    # 解析后的正文。版本行刚预留、尚未解析时为 None——它与 content_hash 同生同灭，
    # 因此「是否已解析」只需看 content_hash 一个字段。
    raw_text: str | None
    loader_version: str
    chunker_version: str
    embedding_model: str
    embedding_dimensions: int
    created_at: str


@dataclass(frozen=True)
class DocumentChunk:
    chunk_id: str
    organization_id: str
    document_id: str
    version_id: str
    ordinal: int
    heading_path: str | None
    content: str
    token_count: int
    start_offset: int
    end_offset: int
    created_at: str | None = None


@dataclass(frozen=True)
class ChunkWithDocumentTitle:
    """A chunk joined with its active document's title.

    Produced by ``KnowledgeStore.resolve_active_citations`` so retrieval can
    build structured citations whose ``title`` comes from SQLite (never from
    the vector-store payload) after the four-level id and document status/version
    have been re-validated.
    """

    chunk_id: str
    organization_id: str
    document_id: str
    version_id: str
    ordinal: int
    heading_path: str | None
    content: str
    token_count: int
    document_title: str
    # 该块在「所属版本正文」（document_versions.raw_text）中的精确字符区间，
    # 满足 content == raw_text[start_offset:end_offset]。它是引用跳转到正文
    # 对应位置的唯一依据：正文里重复出现的文本无法靠搜索定位，只能靠偏移。
    # 字符数按 Python str 计数（非 utf-8 字节数，也不是行号）。
    start_offset: int
    # 区间结束位置（不含），恒有 end_offset > start_offset（数据库 CHECK 约束）。
    end_offset: int


@dataclass(frozen=True)
class IngestionJob:
    job_id: str
    organization_id: str
    document_id: str
    version_id: str
    status: IngestionStatus
    attempt_count: int
    error_code: str | None
    error_message: str | None
    started_at: str | None
    finished_at: str | None
    created_at: str


@dataclass(frozen=True)
class RetrievalEvent:
    event_id: str
    organization_id: str
    conversation_id: str | None
    strategy: str
    original_query: str
    planned_queries_json: str
    round_count: int
    candidate_json: str
    selected_chunk_ids_json: str
    outcome: str
    latency_ms: int
    model_calls: int
    estimated_tokens: int
    created_at: str


class KnowledgeError(Exception):
    """Base class for stable, tenant-safe knowledge domain errors.

    ``code`` is a stable machine-readable string (e.g. ``DOCUMENT_NOT_FOUND``).
    ``safe_message`` is a human-readable message that never leaks cross-tenant
    detail (e.g. whether an id belongs to another organization).
    """

    code = "KNOWLEDGE_ERROR"

    def __init__(self, code: str, safe_message: str) -> None:
        self.code = code
        self.safe_message = safe_message
        super().__init__(safe_message)


class DocumentNotFoundError(KnowledgeError, LookupError):
    """Raised when the current organization has no such document.

    A missing id is treated identically to an id that belongs to another
    organization: the caller only ever sees ``DOCUMENT_NOT_FOUND``.
    """

    code = "DOCUMENT_NOT_FOUND"

    def __init__(self, organization_id: str, document_id: str) -> None:
        super().__init__(
            "DOCUMENT_NOT_FOUND",
            f"document {document_id} not found for organization {organization_id}",
        )


class DuplicateDocumentVersionError(KnowledgeError):
    """Raised when a version with the same content hash already exists.

    The store catches the unique-hash IntegrityError and queries the
    existing version so the caller can treat the upload as idempotent
    instead of creating a duplicate version.
    """

    code = "DUPLICATE_DOCUMENT_VERSION"

    def __init__(self, existing_version_id: str) -> None:
        self.existing_version_id = existing_version_id
        super().__init__(
            "DUPLICATE_DOCUMENT_VERSION",
            f"a version with this content already exists ({existing_version_id})",
        )


class DocumentDisabledError(KnowledgeError):
    code = "DOCUMENT_DISABLED"

    def __init__(self, organization_id: str, document_id: str) -> None:
        super().__init__(
            "DOCUMENT_DISABLED",
            f"document {document_id} is disabled for organization {organization_id}",
        )


class InvalidDocumentError(KnowledgeError, ValueError):
    code = "INVALID_DOCUMENT"

    def __init__(self, *, reason: str) -> None:
        super().__init__("INVALID_DOCUMENT", f"invalid document: {reason}")


class IngestionFailedError(KnowledgeError):
    code = "INGESTION_FAILED"

    def __init__(self, *, reason: str) -> None:
        super().__init__("INGESTION_FAILED", f"ingestion failed: {reason}")


class IngestionSourceMissingError(KnowledgeError):
    """Raised when a queued version has no usable upload payload left.

    入库异步化后，解析所需的原始字节在上传时暂存在版本行上，解析完成即清空。
    若 worker 执行某个任务时既没有已解析正文、也读不到暂存字节，说明这份上传
    已经无法完成——它不是「文档非法」（用户没传错东西），也不是「解析服务不可用」
    （服务本身是好的），因此单独给一个稳定编码，避免被误报成前两者。
    """

    code = "INGESTION_SOURCE_MISSING"

    def __init__(self, *, reason: str) -> None:
        super().__init__(
            "INGESTION_SOURCE_MISSING",
            "the uploaded document payload is no longer available",
        )
        self.reason = reason


class InsufficientEvidenceError(KnowledgeError):
    """Returned when retrieval succeeded but evidence is insufficient.

    This is a normal business result, not a server error: the agent must
    answer honestly (or abstain) without inventing citations.
    """

    code = "INSUFFICIENT_EVIDENCE"

    def __init__(self, *, missing_aspects: Sequence[str]) -> None:
        details = ", ".join(missing_aspects) if missing_aspects else "no evidence found"
        super().__init__(
            "INSUFFICIENT_EVIDENCE",
            f"insufficient evidence: {details}",
        )


class EmbeddingUnavailableError(KnowledgeError):
    """Raised when the embedding provider cannot serve a request.

    Covers temporary failures (retries exhausted: 429/5xx/connection/timeout)
    and permanent contract violations (bad count, wrong dimension, non-finite
    values, malformed sparse vectors, non-temporary 4xx). Callers should not
    retry after this is raised.
    """

    code = "EMBEDDING_UNAVAILABLE"

    def __init__(self, *, reason: str) -> None:
        super().__init__("EMBEDDING_UNAVAILABLE", f"embedding unavailable: {reason}")


class ParsingUnavailableError(KnowledgeError):
    """Raised when the external document-parsing service cannot serve a request.

    PDF/DOCX 的正文提取依赖外部解析服务（LlamaParse）。该服务不可达、
    超时、限流或额度耗尽时抛本异常。它与
    :class:`InvalidDocumentError` 是两类不同的失败：前者是「文档本身没问题，
    但解析能力暂时不可用」，不应让调用方以为用户上传了坏文件，
    更不能降级成空文档——那会让一份从未被解析的文档静默进入知识库。

    与 :class:`EmbeddingUnavailableError` 一致：``reason`` 只用于进程内
    诊断，对外暴露的 ``safe_message`` 不含密钥与上游响应体。
    """

    code = "PARSING_UNAVAILABLE"

    def __init__(self, *, reason: str) -> None:
        self.reason = reason
        super().__init__(
            "PARSING_UNAVAILABLE",
            "document parsing service unavailable",
        )


class VectorStoreUnavailableError(KnowledgeError):
    """Raised when a vector store (Qdrant) request cannot be served.

    Covers connection/timeout failures and ``UnexpectedResponse`` from the
    Qdrant transport. Never converted to an empty result: the caller must
    treat inference as failed rather than silently dropping evidence.
    """

    code = "VECTOR_STORE_UNAVAILABLE"

    def __init__(self, *, reason: str) -> None:
        super().__init__("VECTOR_STORE_UNAVAILABLE", f"vector store unavailable: {reason}")


class SearchBudgetExceededError(KnowledgeError):
    code = "SEARCH_BUDGET_EXCEEDED"

    def __init__(self) -> None:
        super().__init__(
            "SEARCH_BUDGET_EXCEEDED",
            "knowledge search budget exceeded",
        )


class RerankUnavailableError(KnowledgeError):
    """Raised when recalled knowledge candidates cannot be reranked safely."""

    code = "RERANK_UNAVAILABLE"

    def __init__(self, *, reason: str) -> None:
        self.reason = reason
        super().__init__(
            "RERANK_UNAVAILABLE",
            "knowledge reranking is temporarily unavailable",
        )


class SearchInternalError(KnowledgeError):
    code = "SEARCH_INTERNAL_ERROR"

    def __init__(self, *, internal_reason: str | None = None) -> None:
        self.internal_reason = internal_reason
        super().__init__(
            "SEARCH_INTERNAL_ERROR",
            "knowledge search could not be completed",
        )


class KnowledgeStore(Protocol):
    """Tenant-scoped document/version/chunk/job persistence boundary.

    Every method explicitly receives ``organization_id``. There is no
    global-id lookup: a caller can never ask for a document, version, chunk
    or job by id alone across tenants.
    """

    def create_document(
        self,
        *,
        organization_id: str,
        uploaded_by_user_id: str,
        title: str,
        source_type: DocumentSourceType,
    ) -> KnowledgeDocument:
        raise NotImplementedError

    def list_documents(
        self,
        *,
        organization_id: str,
    ) -> list[KnowledgeDocument]:
        """List the caller's documents, tenant-scoped and ordered."""
        raise NotImplementedError

    def get_document(
        self,
        *,
        organization_id: str,
        document_id: str,
    ) -> KnowledgeDocument:
        """Return a single document, scoped to the caller's organization.

        A missing id is indistinguishable from one that belongs to another
        organization; both raise :class:`DocumentNotFoundError`.
        """
        raise NotImplementedError

    def set_document_status(
        self,
        *,
        organization_id: str,
        document_id: str,
        status: DocumentStatus,
    ) -> KnowledgeDocument:
        raise NotImplementedError

    def list_versions(
        self,
        *,
        organization_id: str,
        document_id: str,
    ) -> list[DocumentVersion]:
        """List the versions of one document for the caller's organization.

        A document id that does not exist (or belongs to another org) yields an
        empty list, never a cross-tenant leak.
        """
        raise NotImplementedError

    def create_version(
        self,
        *,
        organization_id: str,
        document_id: str,
        source_hash: str,
        raw_bytes: bytes,
        loader_version: str,
        chunker_version: str,
        embedding_model: str,
        embedding_dimensions: int,
    ) -> DocumentVersion:
        """预留一个版本行：只登记原始字节指纹与待解析内容，正文暂为空。

        ``content_hash``/``raw_text`` 由 :meth:`record_version_content` 在解析完成后
        回填。同一 ``(org, document, source_hash)`` 重复预留会抛
        :class:`DuplicateDocumentVersionError`，让「同一份文件重复上传」在解析之前
        就被拦下。

        同步路径（``KnowledgeIngestionService.ingest_new_document``）已解析完正文，
        改用 :meth:`create_parsed_version`。
        """
        raise NotImplementedError

    def create_parsed_version(
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
        """直接写入一个「已解析」的版本行（同步入库路径使用）。

        与 :meth:`create_version` 的区别只在内容是否已经拿到：本方法一次写全
        ``content_hash``/``raw_text``，不经过预留与回填两步。
        """
        raise NotImplementedError

    def get_version_by_source_hash(
        self,
        *,
        organization_id: str,
        document_id: str,
        source_hash: str,
    ) -> DocumentVersion | None:
        """按原始字节指纹查版本；不存在时返回 None（不抛异常）。"""
        raise NotImplementedError

    def record_version_content(
        self,
        *,
        organization_id: str,
        document_id: str,
        version_id: str,
        content_hash: str,
        raw_text: str,
    ) -> DocumentVersion:
        """解析完成后回填正文与内容哈希，并清空暂存的原始字节。

        清空 ``raw_bytes`` 与回填必须同事务完成：原始字节的唯一用途就是这次解析，
        留着只会让数据库持续膨胀。
        """
        raise NotImplementedError

    def read_version_raw_bytes(
        self,
        *,
        organization_id: str,
        document_id: str,
        version_id: str,
    ) -> bytes | None:
        """读取暂存的上传原始字节；已被清空（或从未暂存）时返回 None。"""
        raise NotImplementedError

    def claim_job(
        self,
        *,
        organization_id: str,
        job_id: str,
    ) -> IngestionJob | None:
        """把 ``queued`` 任务原子置为 ``running``；已被抢占或已是终态时返回 None。

        返回 None 让 worker 能安全地重复投递同一个任务（例如重启恢复后重复入队），
        而不必自己维护「这个任务是不是我抢到的」这类状态。
        """
        raise NotImplementedError

    def recover_stale_jobs(self) -> list[tuple[str, str]]:
        """启动恢复：把上次进程遗留的非终态任务处理掉。

        * ``running`` 任务：进程在解析/嵌入途中退出，任务永远不会有结果。若其版本
          仍暂存着原始字节则重置回 ``queued`` 以便重新执行（返回给调用方重新排队）；
          字节已丢失则标记 ``failed``，给出稳定的 ``INGESTION_INTERRUPTED`` 码。
        * ``queued`` 任务：直接返回给调用方重新入队。

        返回值是 ``(organization_id, job_id)`` 列表，正好是可以投递回 worker 的任务。
        本方法是跨租户的运维操作（没有租户上下文），因此不接收 ``organization_id``。
        """
        raise NotImplementedError

    def get_version_by_id(
        self,
        *,
        organization_id: str,
        document_id: str,
        version_id: str,
    ) -> DocumentVersion:
        raise NotImplementedError

    def get_version_by_hash(
        self,
        *,
        organization_id: str,
        document_id: str,
        content_hash: str,
    ) -> DocumentVersion:
        raise NotImplementedError

    def create_job(
        self,
        *,
        organization_id: str,
        document_id: str,
        version_id: str,
    ) -> IngestionJob:
        raise NotImplementedError

    def get_latest_job_for_version(
        self,
        *,
        organization_id: str,
        document_id: str,
        version_id: str,
        job_id: str | None = None,
    ) -> IngestionJob | None:
        """Return the latest job for a document's version (or by id, when
        ``job_id`` is provided). The id-only lookup is scoped SOLELY by
        ``organization_id``; a missing or cross-tenant job is None."""
        raise NotImplementedError

    def mark_job_running(
        self,
        *,
        organization_id: str,
        job_id: str,
    ) -> IngestionJob | None:
        raise NotImplementedError

    def fail_ingestion(
        self,
        *,
        organization_id: str,
        document_id: str,
        version_id: str,
        job_id: str,
        error_code: str,
        error_message: str,
    ) -> KnowledgeDocument:
        raise NotImplementedError

    def replace_chunks(
        self,
        *,
        organization_id: str,
        document_id: str,
        version_id: str,
        chunks: Sequence[DocumentChunk],
    ) -> None:
        raise NotImplementedError

    def list_version_chunks(
        self,
        *,
        organization_id: str,
        document_id: str,
        version_id: str,
    ) -> list[DocumentChunk]:
        """Read one exact version's chunks without requiring it to be active."""
        raise NotImplementedError

    def activate_version(
        self,
        *,
        organization_id: str,
        document_id: str,
        version_id: str,
        job_id: str,
    ) -> KnowledgeDocument:
        raise NotImplementedError

    def list_active_version_ids(
        self,
        *,
        organization_id: str,
    ) -> list[str]:
        raise NotImplementedError

    def list_active_chunks(
        self,
        *,
        organization_id: str,
        candidate_ids: Sequence[str],
    ) -> list[DocumentChunk]:
        raise NotImplementedError

    def resolve_active_citations(
        self,
        *,
        organization_id: str,
        candidate_ids: Sequence[str],
    ) -> list[ChunkWithDocumentTitle]:
        """Second-pass validation of retrieval candidates against SQLite.

        Only candidates whose four-level ids (organization + document + version
        + chunk) match AND whose document is ``status='active'`` with
        ``active_version_id == chunk.version_id`` are returned, each joined
        with the document's title. Citation content comes only from here, never
        from a vector-store payload.
        """
        raise NotImplementedError

    def record_retrieval_event(
        self,
        *,
        organization_id: str,
        event: RetrievalEvent,
    ) -> RetrievalEvent:
        raise NotImplementedError

    def get_retrieval_event(
        self,
        *,
        organization_id: str,
        conversation_id: str,
    ) -> RetrievalEvent | None:
        raise NotImplementedError
