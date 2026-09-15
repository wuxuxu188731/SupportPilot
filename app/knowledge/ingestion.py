"""Knowledge-ingestion orchestration: queue-time bookkeeping + execution.

Ties together the store, loader, chunker, embedding client and vector store
into one ingestion pipeline: validate -> version/job -> chunk -> embed ->
SQLite chunks -> Qdrant points -> activate. The version is only activated
after the vector points are written, so a document is never searchable before
its vectors are durable.

Two entry points exist, split by *who waits for the parsing*:

* :meth:`KnowledgeIngestionService.ingest_new_document` /
  :meth:`KnowledgeIngestionService.ingest_new_version` run the whole pipeline
  inline and return a terminal receipt. They are the synchronous API used by
  tests, the evaluation harness and the stage C fixtures.
* :meth:`KnowledgeIngestionService.queue_new_document` /
  :meth:`KnowledgeIngestionService.queue_new_version` only persist the upload
  (document + reserved version + queued job + staged raw bytes) and return
  ``QUEUED`` immediately. A background worker then calls
  :meth:`KnowledgeIngestionService.run_job` to execute it. This is what the
  HTTP upload endpoints use, because a DOCX/PDF parse takes ~27s and would
  otherwise block the request (and the event loop) until the client times out.

The constructor depends solely on Protocols so any concrete
store/loader/chunker/embedding/vector-store can be injected; no concrete class
is instantiated here.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Protocol

from app.knowledge.base import (
    DocumentNotFoundError,
    DocumentSourceType,
    DuplicateDocumentVersionError,
    EmbeddingUnavailableError,
    IngestionJob,
    IngestionSourceMissingError,
    IngestionStatus,
    InvalidDocumentError,
    KnowledgeDocument,
    KnowledgeStore,
    ParsingUnavailableError,
    VectorStoreUnavailableError,
)
from app.knowledge.chunking import CHUNKER_VERSION, KnowledgeChunker
from app.knowledge.document_loader import (
    LOADER_VERSION,
    MAX_DOCUMENT_BYTES,
    DocumentLoader,
    LoadedDocument,
)
from app.knowledge.embeddings import EmbeddingClient
from app.knowledge.vector_store import VectorPoint, VectorStore

MAX_TITLE_LENGTH = 200
MIN_TITLE_LENGTH = 1

# Stable machine-readable error codes produced once document/version/job rows
# exist. Only these domain exceptions carry their own code; everything else
# is folded into INGESTION_FAILED with a safe generic message.
ERROR_CODE_BY_EXCEPTION = {
    EmbeddingUnavailableError: "EMBEDDING_UNAVAILABLE",
    VectorStoreUnavailableError: "VECTOR_STORE_UNAVAILABLE",
    InvalidDocumentError: "INVALID_DOCUMENT",
    ParsingUnavailableError: "PARSING_UNAVAILABLE",
    IngestionSourceMissingError: "INGESTION_SOURCE_MISSING",
}
INGESTION_FAILED_MESSAGE = "knowledge ingestion failed"
# Safe, stable error messages forever pinned by code. These never interpolate
# the exception ``reason``/``safe_message``, so API keys, raw document bodies or
# provider transport detail can never leak into the persisted job error.
SAFE_ERROR_MESSAGE_BY_CODE = {
    "EMBEDDING_UNAVAILABLE": "embedding service unavailable",
    "VECTOR_STORE_UNAVAILABLE": "vector store unavailable",
    "INVALID_DOCUMENT": "invalid document",
    "PARSING_UNAVAILABLE": "document parsing service unavailable",
    # 上传已经落库、但版本行里没有留下任何可解析的字节（例如数据被外部清理）。
    # 这不是「用户传了坏文件」，因此单独给一个稳定编码。
    "INGESTION_SOURCE_MISSING": "the uploaded document payload is no longer available",
}


# 需要外部解析服务提取的格式：它们的字节是二进制（docx 是 zip 包、pdf 含二进制流），
# 既不能做 UTF-8 校验，重新装载时也要按「已归一化的 Markdown」处理。
_EXTRACTED_SOURCE_TYPES = frozenset(
    {DocumentSourceType.WORD, DocumentSourceType.PDF}
)


class IngestionDispatcher(Protocol):
    """Hands a queued ingestion job to whatever actually executes it.

    Implemented by the in-process worker. The service depends only on this
    one-method shape, so queuing works in tests (and in any future out-of-process
    worker) without the service knowing how the job is executed.
    """

    def enqueue(self, organization_id: str, job_id: str) -> None:
        """Schedule ``job_id`` for execution; must never block on the work itself."""
        raise NotImplementedError


def _lookup_error_code(exc: Exception) -> str | None:
    """Resolve a stable error code for an exception via isinstance checks.

    A plain dict ``.get(type(exc))`` matches on exact type only, so a
    subclass (e.g. a Qdrant transport error subclass of
    ``VectorStoreUnavailableError``) would silently fall through to
    ``INGESTION_FAILED``. Iterating the mapping in order with ``isinstance``
    lets subclasses inherit their base's stable code while the most specific
    key wins.
    """
    for exc_type, code in ERROR_CODE_BY_EXCEPTION.items():
        if isinstance(exc, exc_type):
            return code
    return None


# Internal diagnostics only. Explicitly carries the traceback (with the
# exception chain) so operators can root-cause, while the job's error_message
# stays safe and never exposes keys, raw bodies or stack details.
_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IngestionReceipt:
    document_id: str
    version_id: str
    job_id: str
    status: IngestionStatus
    deduplicated: bool


@dataclass(frozen=True)
class IngestionMetadata:
    """Public, content-free version metadata stamped by this service."""

    loader_version: str
    chunker_version: str
    embedding_model: str
    embedding_dimensions: int


class KnowledgeIngestionService:
    """Orchestrates the synchronous, happy-path ingestion of a knowledge
    document (or a new version of an existing document) into SQLite + Qdrant.

    ``embedding_model`` / ``embedding_dimensions`` are scalar metadata stamped
    onto every new ``DocumentVersion`` so retrieval can be reproduced; they are
    not classes and are independent of the injected Protocol dependencies.
    """

    def __init__(
        self,
        *,
        store: KnowledgeStore,
        loader: DocumentLoader,
        chunker: KnowledgeChunker,
        embedding: EmbeddingClient,
        vector_store: VectorStore,
        embedding_model: str = "text-embedding-v4",
        embedding_dimensions: int = 1024,
        dispatcher: IngestionDispatcher | None = None,
    ) -> None:
        self._store = store
        self._loader = loader
        self._chunker = chunker
        self._embedding = embedding
        self._vector_store = vector_store
        self._embedding_model = embedding_model
        self._embedding_dimensions = embedding_dimensions
        # 未注入 dispatcher 时 queue_* 只会创建记录、不执行任务（返回 queued 回执），
        # 这正是单测与离线脚本想要的行为；生产装配注入进程内 worker。
        self._dispatcher = dispatcher

    # -------------------------------------------------------------- public

    @property
    def metadata(self) -> IngestionMetadata:
        """Expose the exact persisted version configuration without internals."""
        return IngestionMetadata(
            loader_version=LOADER_VERSION,
            chunker_version=CHUNKER_VERSION,
            embedding_model=self._embedding_model,
            embedding_dimensions=self._embedding_dimensions,
        )

    def set_dispatcher(self, dispatcher: IngestionDispatcher | None) -> None:
        """Install the component that executes queued jobs.

        Needed because the worker and the service reference each other: the worker
        needs the service to run jobs, and the service needs the worker to enqueue
        them. ``create_knowledge_services`` builds the service first, then the
        worker, then calls this — which keeps both constructors free of cycles.
        """
        self._dispatcher = dispatcher

    def ingest_new_document(
        self,
        *,
        organization_id: str,
        uploaded_by_user_id: str,
        title: str,
        source_type: DocumentSourceType,
        content: bytes,
    ) -> IngestionReceipt:
        """Create a brand-new document and ingest its first version.

        The full version pipeline runs synchronously; on the happy path the
        document ends ``ACTIVE`` only after its vectors are in Qdrant. The
        upload is loaded before the document row is created (load first in the
        strict event order).
        """
        clean_title = self._validate_title(title)
        loaded = self._loader.load(content, source_type)
        content_hash = self._hash(loaded.text)
        document = self._store.create_document(
            organization_id=organization_id,
            uploaded_by_user_id=uploaded_by_user_id,
            title=clean_title,
            source_type=source_type,
        )
        return self._ingest_version(
            organization_id=organization_id,
            document=document,
            source_type=source_type,
            loaded=loaded,
            content_hash=content_hash,
        )

    def ingest_new_version(
        self,
        *,
        organization_id: str,
        uploaded_by_user_id: str,
        document_id: str,
        source_type: DocumentSourceType,
        content: bytes,
    ) -> IngestionReceipt:
        """Ingest a new version of an existing document.

        The document is looked up by org+id (``DOCUMENT_NOT_FOUND`` if absent)
        and keeps its original ``source_type``; an upload whose declared type
        differs is rejected with ``INVALID_DOCUMENT``.
        """
        document = self._store.get_document(
            organization_id=organization_id,
            document_id=document_id,
        )
        self._ensure_source_type_matches(
            document=document, source_type=source_type
        )
        # 原始字节去重先跑：命中已成功的同字节版本时根本不需要解析，
        # 这是省下一次 LlamaParse 计费的关键（解析按页计费）。
        short_circuit = self._resolve_source_duplicate(
            organization_id=organization_id,
            document=document,
            content=content,
        )
        if short_circuit is not None:
            return short_circuit
        loaded = self._loader.load(content, document.source_type)
        content_hash = self._hash(loaded.text)
        duplicate = self._resolve_duplicate(
            organization_id=organization_id,
            document=document,
            loaded=loaded,
            content_hash=content_hash,
        )
        if duplicate is not None:
            return duplicate
        return self._ingest_version(
            organization_id=organization_id,
            document=document,
            source_type=document.source_type,
            loaded=loaded,
            content_hash=content_hash,
        )

    # ---------------------------------------------------- queue (async path)

    def queue_new_document(
        self,
        *,
        organization_id: str,
        uploaded_by_user_id: str,
        title: str,
        source_type: DocumentSourceType,
        content: bytes,
    ) -> IngestionReceipt:
        """Persist a new document and queue its first version; return ``QUEUED``.

        Nothing is parsed here: the upload endpoint must return before the ~27s
        DOCX/PDF extraction runs. What is persisted up front is everything a
        retry or a restart needs — the document row, a reserved version row
        holding the upload bytes, and a queued job. Parsing, chunking, embedding
        and activation all happen later in :meth:`run_job`.

        Cheap guards that would make a document unusable for good (blank/over-long
        title, over-size payload, non-UTF-8 text) still run now, so a client error
        stays a 422 instead of becoming a failed background job.
        """
        clean_title = self._validate_title(title)
        source_hash = self._validate_upload(content, source_type=source_type)
        document = self._store.create_document(
            organization_id=organization_id,
            uploaded_by_user_id=uploaded_by_user_id,
            title=clean_title,
            source_type=source_type,
        )
        version = self._store.create_version(
            organization_id=organization_id,
            document_id=document.document_id,
            source_hash=source_hash,
            raw_bytes=content,
            loader_version=LOADER_VERSION,
            chunker_version=CHUNKER_VERSION,
            embedding_model=self._embedding_model,
            embedding_dimensions=self._embedding_dimensions,
        )
        return self._queue_job(organization_id=organization_id, version=version)

    def queue_new_version(
        self,
        *,
        organization_id: str,
        uploaded_by_user_id: str,
        document_id: str,
        source_type: DocumentSourceType,
        content: bytes,
    ) -> IngestionReceipt:
        """Reserve a new version of an existing document and queue it.

        Same contract as :meth:`queue_new_document`, plus the two version-specific
        rules: the declared type must match the document's original type, and
        re-uploading bytes that are already being or have been ingested is
        idempotent (no second version, no second parse).
        """
        document = self._store.get_document(
            organization_id=organization_id,
            document_id=document_id,
        )
        self._ensure_source_type_matches(
            document=document, source_type=source_type
        )
        source_hash = self._validate_upload(content, source_type=document.source_type)
        duplicate = self._resolve_source_duplicate(
            organization_id=organization_id,
            document=document,
            content=content,
            source_hash=source_hash,
        )
        if duplicate is not None:
            return duplicate
        try:
            version = self._store.create_version(
                organization_id=organization_id,
                document_id=document.document_id,
                source_hash=source_hash,
                raw_bytes=content,
                loader_version=LOADER_VERSION,
                chunker_version=CHUNKER_VERSION,
                embedding_model=self._embedding_model,
                embedding_dimensions=self._embedding_dimensions,
            )
        except DuplicateDocumentVersionError as exc:
            # Lost a race against a concurrent upload of the same bytes: adopt
            # whatever that upload reserved instead of creating a second task.
            return self._adopt_duplicate_version(
                organization_id=organization_id,
                document=document,
                existing_version_id=exc.existing_version_id,
            )
        return self._queue_job(organization_id=organization_id, version=version)

    def run_job(
        self,
        *,
        organization_id: str,
        job_id: str,
    ) -> IngestionReceipt | None:
        """Execute one queued job end to end. Safe to call for any job id.

        Returns the terminal receipt, or ``None`` when the job could not be
        claimed — already running elsewhere, already in a terminal state, or
        simply unknown. Returning ``None`` instead of raising is what makes
        redelivery (worker restart, duplicate enqueue) harmless.

        The parse happens here rather than at queue time, so failures such as an
        unreachable parsing service are recorded on the job with their stable
        code (``PARSING_UNAVAILABLE``) instead of being returned to the client.
        """
        job = self._store.claim_job(
            organization_id=organization_id,
            job_id=job_id,
        )
        if job is None:
            return None
        version = self._store.get_version_by_id(
            organization_id=organization_id,
            document_id=job.document_id,
            version_id=job.version_id,
        )
        document = self._store.get_document(
            organization_id=organization_id,
            document_id=job.document_id,
        )
        try:
            loaded, content_hash = self._load_version_content(
                organization_id=organization_id,
                document=document,
                version=version,
            )
            return self._pipeline_version(
                organization_id=organization_id,
                document=document,
                version=version,
                job=job,
                loaded=loaded,
                content_hash=content_hash,
            )
        except Exception as exc:
            self._fail_reserved(
                organization_id=organization_id,
                document=document,
                version=version,
                job_id=job.job_id,
                exc=exc,
            )
            raise

    # ------------------------------------------------------------- private

    def _load_version_content(
        self,
        *,
        organization_id: str,
        document: KnowledgeDocument,
        version,
    ) -> tuple[LoadedDocument, str]:
        """Obtain the parsed document for a reserved version.

        A version that already carries its parsed text (a re-run after a
        post-parse failure such as the vector store being down) skips parsing
        entirely and is rebuilt from the stored text, so a retry never pays for
        a second external parse.

        Anything raised here is an ``InvalidDocumentError`` or a
        ``ParsingUnavailableError``, both of which already carry a stable code.
        """
        if version.content_hash is not None:
            loaded = self._reload_from_stored_text(version.raw_text, document.source_type)
            return loaded, version.content_hash

        content = self._store.read_version_raw_bytes(
            organization_id=organization_id,
            document_id=document.document_id,
            version_id=version.version_id,
        )
        if content is None:
            raise IngestionSourceMissingError(
                reason="the reserved version has no staged upload bytes"
            )

        loaded = self._loader.load(content, document.source_type)
        content_hash = self._hash(loaded.text)
        version = self._store.record_version_content(
            organization_id=organization_id,
            document_id=document.document_id,
            version_id=version.version_id,
            content_hash=content_hash,
            raw_text=loaded.text,
        )
        return loaded, version.content_hash

    def _reload_from_stored_text(
        self,
        raw_text: str,
        source_type: DocumentSourceType,
    ) -> LoadedDocument:
        """Rebuild sections from an already-parsed version without any parse.

        ``DocumentLoader`` owns the heading-stack rules, so the text is re-run
        through its local (non-extracting) branch rather than re-deriving
        sections here — that keeps one single source of truth for section
        boundaries. The text is already normalised Markdown, which is exactly
        what the MARKDOWN branch expects.
        """
        return self._loader.load(
            raw_text.encode("utf-8"),
            (
                DocumentSourceType.MARKDOWN
                if source_type in _EXTRACTED_SOURCE_TYPES
                else source_type
            ),
        )

    @staticmethod
    def _ensure_source_type_matches(
        *,
        document: KnowledgeDocument,
        source_type: DocumentSourceType,
    ) -> None:
        if source_type != document.source_type:
            raise InvalidDocumentError(
                reason=(
                    f"source type {source_type.value} does not match document "
                    f"type {document.source_type.value}"
                )
            )

    def _validate_upload(
        self,
        content: bytes,
        *,
        source_type: DocumentSourceType,
    ) -> str:
        """Cheap, parse-free upload guards; returns the raw-bytes hash.

        The encoding check is deliberately limited to the text source types:
        real DOCX (a zip container) and PDF bytes are not valid UTF-8, and
        applying the text check to them would reject every binary document.
        """
        if len(content) > MAX_DOCUMENT_BYTES:
            raise InvalidDocumentError(reason="document exceeds maximum size")
        if source_type not in _EXTRACTED_SOURCE_TYPES:
            try:
                text = content.decode("utf-8")
            except (UnicodeDecodeError, UnicodeError):
                raise InvalidDocumentError(
                    reason="document is not valid utf-8"
                ) from None
            if not text.strip():
                raise InvalidDocumentError(reason="document is empty")
        elif not content.strip():
            raise InvalidDocumentError(reason="document is empty")
        return self._source_hash(content)

    def _resolve_source_duplicate(
        self,
        *,
        organization_id: str,
        document: KnowledgeDocument,
        content: bytes,
        source_hash: str | None = None,
    ) -> IngestionReceipt | None:
        """Short-circuit an upload whose bytes were already reserved.

        Called before any parsing happens, which is the whole point: the same
        file uploaded twice must not cost a second external parse. A version
        whose latest job FAILED is not short-circuited — the bytes are still
        staged, so re-uploading must actually retry the pipeline.
        """
        digest = source_hash if source_hash is not None else self._source_hash(content)
        existing = self._store.get_version_by_source_hash(
            organization_id=organization_id,
            document_id=document.document_id,
            source_hash=digest,
        )
        if existing is None:
            return None
        job = self._store.get_latest_job_for_version(
            organization_id=organization_id,
            document_id=document.document_id,
            version_id=existing.version_id,
        )
        if job is not None and job.status is IngestionStatus.FAILED:
            return None
        return self._receipt_for_existing(
            document_id=document.document_id,
            version_id=existing.version_id,
            job=job,
        )

    def _adopt_duplicate_version(
        self,
        *,
        organization_id: str,
        document: KnowledgeDocument,
        existing_version_id: str,
    ) -> IngestionReceipt:
        """Adopt the version a concurrent upload of identical bytes reserved."""
        job = self._store.get_latest_job_for_version(
            organization_id=organization_id,
            document_id=document.document_id,
            version_id=existing_version_id,
        )
        return self._receipt_for_existing(
            document_id=document.document_id,
            version_id=existing_version_id,
            job=job,
        )

    def _receipt_for_existing(
        self,
        *,
        document_id: str,
        version_id: str,
        job: IngestionJob | None,
    ) -> IngestionReceipt:
        """Build the receipt for a version that already exists.

        ``SUCCEEDED`` when its latest job already finished (a true no-op), and
        the job's own status otherwise so the client keeps polling the task that
        is genuinely in flight instead of being told a new one started.
        """
        status = job.status if job is not None else IngestionStatus.QUEUED
        return IngestionReceipt(
            document_id=document_id,
            version_id=version_id,
            job_id=job.job_id if job is not None else "",
            status=status,
            deduplicated=True,
        )

    def _queue_job(
        self,
        *,
        organization_id: str,
        version,
    ) -> IngestionReceipt:
        """Create the queued job for a reserved version and hand it to the worker."""
        job = self._store.create_job(
            organization_id=organization_id,
            document_id=version.document_id,
            version_id=version.version_id,
        )
        if self._dispatcher is not None:
            self._dispatcher.enqueue(organization_id, job.job_id)
        return IngestionReceipt(
            document_id=version.document_id,
            version_id=version.version_id,
            job_id=job.job_id,
            status=IngestionStatus.QUEUED,
            deduplicated=False,
        )

    @staticmethod
    def _validate_title(title: str) -> str:
        clean = title.strip()
        if not (MIN_TITLE_LENGTH <= len(clean) <= MAX_TITLE_LENGTH):
            raise InvalidDocumentError(
                reason=(
                    f"title must be {MIN_TITLE_LENGTH}-{MAX_TITLE_LENGTH} "
                    f"characters after trimming"
                )
            )
        return clean

    @staticmethod
    def _hash(normalized_text: str) -> str:
        return "sha256:" + hashlib.sha256(
            normalized_text.encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _source_hash(content: bytes) -> str:
        """指纹上传的原始字节，用于解析前的去重。"""
        return "sha256:" + hashlib.sha256(content).hexdigest()

    def _resolve_duplicate(
        self,
        *,
        organization_id: str,
        document: KnowledgeDocument,
        loaded: LoadedDocument,
        content_hash: str,
    ) -> IngestionReceipt | None:
        """Return a deduplicated receipt for an identical, ALREADY-ACTIVATED
        version, or None if we must run the (re-)ingestion pipeline.

        This short-circuits idempotent re-uploads BEFORE any new version/job is
        reserved and before any embedding/upsert work, so re-uploading the same
        normalized content that already SUCCEEDED touches the store/embedding/
        vector store exactly once. The (org, document, hash) uniqueness protects
        against concurrent duplicates via the store's unique constraint instead.

        A version whose latest job FAILED is deliberately NOT short-circuited:
        it was never activated, so re-uploading that content must fall through
        to the pipeline and actually make it searchable (or fail truthfully).
        """
        try:
            existing = self._store.get_version_by_hash(
                organization_id=organization_id,
                document_id=document.document_id,
                content_hash=content_hash,
            )
        except DocumentNotFoundError:
            return None
        job = self._store.get_latest_job_for_version(
            organization_id=organization_id,
            document_id=document.document_id,
            version_id=existing.version_id,
        )
        if job is None or job.status is not IngestionStatus.SUCCEEDED:
            # No job yet (mid-race), or the previous attempt failed before
            # activation. Fall through to the normal create/retry path; the
            # store's unique constraint will surface a
            # DuplicateDocumentVersionError that _reuse_duplicate_version
            # turns into a re-run of the pipeline for the same content.
            return None
        return IngestionReceipt(
            document_id=document.document_id,
            version_id=existing.version_id,
            job_id=job.job_id,
            status=IngestionStatus.SUCCEEDED,
            deduplicated=True,
        )

    def _reuse_duplicate_version(
        self,
        *,
        organization_id: str,
        document: KnowledgeDocument,
        existing_version_id: str,
        loaded: LoadedDocument,
    ) -> IngestionReceipt:
        """Handle a ``DuplicateDocumentVersionError`` raised by ``create_version``.

        If the existing version's latest job SUCCEEDED, the content is already
        active, so the upload is idempotent: return its original records with a
        deduplicated receipt and never re-embed/re-upsert.

        Otherwise the existing version was never activated — its job FAILED (or
        no job exists yet, e.g. mid-race). To satisfy the invariant that a new
        version is invisible until Qdrant succeeds, we must NOT report it as
        uploaded. Instead we re-run the full pipeline against the SAME version
        with a fresh job, so re-uploading the content ends SUCCEEDED (and
        activated) or fails truthfully with a stable code recorded on the new job.
        """
        job = self._store.get_latest_job_for_version(
            organization_id=organization_id,
            document_id=document.document_id,
            version_id=existing_version_id,
        )
        if job is not None and job.status is IngestionStatus.SUCCEEDED:
            return IngestionReceipt(
                document_id=document.document_id,
                version_id=existing_version_id,
                job_id=job.job_id,
                status=IngestionStatus.SUCCEEDED,
                deduplicated=True,
            )
        # The earlier attempt was not activated. Re-run the pipeline against the
        # existing version so the same content gets a truthful outcome.
        version = self._store.get_version_by_id(
            organization_id=organization_id,
            document_id=document.document_id,
            version_id=existing_version_id,
        )
        return self._pipeline_version(
            organization_id=organization_id,
            document=document,
            version=version,
            job=self._store.create_job(
                organization_id=organization_id,
                document_id=document.document_id,
                version_id=version.version_id,
            ),
            loaded=loaded,
            content_hash=version.content_hash,
        )

    def _ingest_version(
        self,
        *,
        organization_id: str,
        document: KnowledgeDocument,
        source_type: DocumentSourceType,
        loaded: LoadedDocument,
        content_hash: str,
    ) -> IngestionReceipt:
        # 1. (load + hash already done by the caller, in event order.)
        # 2. version bookkeeping. create_parsed_version raises
        #    DuplicateDocumentVersionError when the same (org, doc, hash) was
        #    already reserved — this is where a concurrent-duplicate race
        #    surfaces, so it MUST be inside the guarded region (it was previously
        #    outside the try, letting the race escape uncaught).
        try:
            version = self._store.create_parsed_version(
                organization_id=organization_id,
                document_id=document.document_id,
                content_hash=content_hash,
                raw_text=loaded.text,
                loader_version=LOADER_VERSION,
                chunker_version=CHUNKER_VERSION,
                embedding_model=self._embedding_model,
                embedding_dimensions=self._embedding_dimensions,
            )
        except DuplicateDocumentVersionError as exc:
            # The store reserved this exact content between our idempotency
            # check (above) and this insert. Route it to the shared duplicate
            # handler: dedup if it already SUCCEEDED, otherwise re-run the
            # pipeline for the existing version.
            return self._reuse_duplicate_version(
                organization_id=organization_id,
                document=document,
                existing_version_id=exc.existing_version_id,
                loaded=loaded,
            )
        return self._pipeline_version(
            organization_id=organization_id,
            document=document,
            version=version,
            job=self._store.create_job(
                organization_id=organization_id,
                document_id=document.document_id,
                version_id=version.version_id,
            ),
            loaded=loaded,
            content_hash=content_hash,
        )

    def _pipeline_version(
        self,
        *,
        organization_id: str,
        document: KnowledgeDocument,
        version,
        job: IngestionJob,
        loaded: LoadedDocument,
        content_hash: str | None,
    ) -> IngestionReceipt:
        """Run the chunk->embed->persist->activate pipeline for a reserved
        version, closing any post-reservation failure into the job/document.

        ``content_hash`` is ``None`` only on the synchronous re-run path
        (:meth:`_reuse_duplicate_version`), where the version row already holds
        the parsed content and therefore already holds the hash.

        Exceptions are only caught here AFTER version+job rows exist. The safe
        ``error_message`` on the job never carries API keys, raw document
        bodies or the underlying traceback; ``KeyboardInterrupt`` /
        ``SystemExit`` are deliberately never swallowed.
        """
        self._store.mark_job_running(
            organization_id=organization_id,
            job_id=job.job_id,
        )
        try:
            # 3. chunk + embed (embedding sees only plain chunk contents).
            chunks = self._chunker.split(
                loaded,
                organization_id=organization_id,
                document_id=document.document_id,
                version_id=version.version_id,
            )
            vectors = self._embedding.embed_documents(
                [chunk.content for chunk in chunks]
            )

            # The embedding response must mirror the chunk set 1:1. If it does
            # not, points would be silently dropped (or mismatched), leaving a
            # searchable version whose Qdrant points don't mirror its SQLite
            # chunks. Fail loudly *before* any upsert and before any activation.
            if len(vectors) != len(chunks):
                raise EmbeddingUnavailableError(
                    reason=(
                        f"embedding provider returned {len(vectors)} vectors for "
                        f"{len(chunks)} chunks; expected a 1:1 match"
                    )
                )

            # 4. persist chunks in SQLite.
            self._store.replace_chunks(
                organization_id=organization_id,
                document_id=document.document_id,
                version_id=version.version_id,
                chunks=chunks,
            )

            # 5. ensure the collection exists, then write vector points. This
            #    must happen before activation so the version is only
            #    searchable once its points are durable.
            self._vector_store.ensure_collection()
            self._vector_store.upsert(
                points=[
                    VectorPoint(
                        chunk_id=chunk.chunk_id,
                        organization_id=organization_id,
                        document_id=document.document_id,
                        version_id=version.version_id,
                        ordinal=chunk.ordinal,
                        embedding=vector,
                    )
                    for chunk, vector in zip(chunks, vectors)
                ]
            )

            # 6. finally activate the version (with the job that belongs to it).
            self._store.activate_version(
                organization_id=organization_id,
                document_id=document.document_id,
                version_id=version.version_id,
                job_id=job.job_id,
            )
        except Exception as exc:
            self._fail_reserved(
                organization_id=organization_id,
                document=document,
                version=version,
                job_id=job.job_id,
                exc=exc,
            )
            raise

        return IngestionReceipt(
            document_id=document.document_id,
            version_id=version.version_id,
            job_id=job.job_id,
            status=IngestionStatus.SUCCEEDED,
            deduplicated=False,
        )

    def _fail_reserved(
        self,
        *,
        organization_id: str,
        document: KnowledgeDocument,
        version,
        job_id: str,
        exc: Exception,
    ) -> None:
        """Persist a stable failure on the job/document without leaking secret
        content, then hand the original exception back to the caller.

        ``error_message`` is a stable per-code safe string that never embeds
        the exception ``reason``/traceback/API key/raw body. The traceback is
        routed only to the internal logger (IDs only) for operators.
        """
        error_code = _lookup_error_code(exc)
        if error_code is None:
            _logger.exception(
                "knowledge ingestion failed; organization_id=%s "
                "document_id=%s version_id=%s job_id=%s exc_type=%s",
                organization_id,
                document.document_id,
                getattr(version, "version_id", ""),
                job_id,
                type(exc).__name__,
            )
            error_code = "INGESTION_FAILED"
            error_message = INGESTION_FAILED_MESSAGE
        else:
            error_message = SAFE_ERROR_MESSAGE_BY_CODE.get(
                error_code, INGESTION_FAILED_MESSAGE
            )
        self._store.fail_ingestion(
            organization_id=organization_id,
            document_id=document.document_id,
            version_id=version.version_id,
            job_id=job_id,
            error_code=error_code,
            error_message=error_message,
        )
