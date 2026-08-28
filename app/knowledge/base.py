from dataclasses import dataclass
from enum import Enum
from typing import Protocol, Sequence


class DocumentSourceType(str, Enum):
    MARKDOWN = "markdown"
    TEXT = "text"
    WORD = "word"


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
    content_hash: str
    raw_text: str
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
        content_hash: str,
        raw_text: str,
        loader_version: str,
        chunker_version: str,
        embedding_model: str,
        embedding_dimensions: int,
    ) -> DocumentVersion:
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
