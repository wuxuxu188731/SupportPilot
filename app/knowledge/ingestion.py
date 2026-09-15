"""Synchronous knowledge-ingestion orchestration.

Ties together the store, loader, chunker, embedding client and vector store
into a single in-request ingestion pipeline: validate -> version/job -> chunk
-> embed -> SQLite chunks -> Qdrant points -> activate. The version is only
activated after the vector points are written, so a document is never
searchable before its vectors are durable.

The constructor depends solely on Protocols so any concrete
store/loader/chunker/embedding/vector-store can be injected; no concrete class
is instantiated here.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

from app.knowledge.base import (
    DocumentNotFoundError,
    DocumentSourceType,
    DuplicateDocumentVersionError,
    EmbeddingUnavailableError,
    IngestionJob,
    IngestionStatus,
    InvalidDocumentError,
    KnowledgeDocument,
    KnowledgeStore,
    ParsingUnavailableError,
    VectorStoreUnavailableError,
)
from app.knowledge.chunking import CHUNKER_VERSION, KnowledgeChunker
from app.knowledge.document_loader import LOADER_VERSION, DocumentLoader, LoadedDocument
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
}

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
    ) -> None:
        self._store = store
        self._loader = loader
        self._chunker = chunker
        self._embedding = embedding
        self._vector_store = vector_store
        self._embedding_model = embedding_model
        self._embedding_dimensions = embedding_dimensions

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
        if source_type != document.source_type:
            raise InvalidDocumentError(
                reason=(
                    f"source type {source_type.value} does not match document "
                    f"type {document.source_type.value}"
                )
            )
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

    # ------------------------------------------------------------- private

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
            loaded=loaded,
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
        # 2. version bookkeeping. create_version raises
        #    DuplicateDocumentVersionError when the same (org, doc, hash) was
        #    already reserved — this is where a concurrent-duplicate race
        #    surfaces, so it MUST be inside the guarded region (it was previously
        #    outside the try, letting the race escape uncaught).
        try:
            version = self._store.create_version(
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
            loaded=loaded,
        )

    def _pipeline_version(
        self,
        *,
        organization_id: str,
        document: KnowledgeDocument,
        version,
        loaded: LoadedDocument,
    ) -> IngestionReceipt:
        """Run the chunk->embed->persist->activate pipeline for a reserved
        version, closing any post-reservation failure into the job/document.

        Exceptions are only caught here AFTER version+job rows exist. The safe
        ``error_message`` on the job never carries API keys, raw document
        bodies or the underlying traceback; ``KeyboardInterrupt`` /
        ``SystemExit`` are deliberately never swallowed.
        """
        job: IngestionJob = self._store.create_job(
            organization_id=organization_id,
            document_id=document.document_id,
            version_id=version.version_id,
        )
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
