import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Sequence
from uuid import uuid4

from app.db.migrations import upgrade_database
from app.knowledge.base import (
    ChunkWithDocumentTitle,
    DocumentChunk,
    DocumentNotFoundError,
    DocumentSourceType,
    DocumentStatus,
    DocumentVersion,
    DuplicateDocumentVersionError,
    IngestionJob,
    IngestionStatus,
    KnowledgeDocument,
    KnowledgeStore,
    RetrievalEvent,
)

# 进程在入库途中退出时留下的任务无法继续执行（原始字节已不在），
# 用一个稳定的错误码如实标记失败，避免出现永远 running 的僵尸任务。
INTERRUPTED_ERROR_CODE = "INGESTION_INTERRUPTED"
INTERRUPTED_ERROR_MESSAGE = "ingestion was interrupted by a process restart"


class SQLiteKnowledgeStore(KnowledgeStore):
    """Tenant-scoped SQLite persistence for knowledge documents and versions.

    Every read/write explicitly carries ``organization_id`` and the entity id,
    so a caller can never address another enterprise's rows by id alone. All
    state transitions (version creation, activation, failure) run in a single
    transaction. Runtime foreign-key enforcement is enabled in ``_connect()``.
    """

    def __init__(self, database_path: str | Path):
        self._database_path = database_path
        upgrade_database(database_path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    # ------------------------------------------------------------------ maps
    @staticmethod
    def _to_document(row: sqlite3.Row) -> KnowledgeDocument:
        return KnowledgeDocument(
            document_id=row["id"],
            organization_id=row["organization_id"],
            uploaded_by_user_id=row["uploaded_by_user_id"],
            title=row["title"],
            source_type=DocumentSourceType(row["source_type"]),
            status=DocumentStatus(row["status"]),
            active_version_id=row["active_version_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _to_version(row: sqlite3.Row) -> DocumentVersion:
        return DocumentVersion(
            version_id=row["id"],
            organization_id=row["organization_id"],
            document_id=row["document_id"],
            version_no=row["version_no"],
            content_hash=row["content_hash"],
            source_hash=row["source_hash"],
            raw_text=row["raw_text"],
            loader_version=row["loader_version"],
            chunker_version=row["chunker_version"],
            embedding_model=row["embedding_model"],
            embedding_dimensions=row["embedding_dimensions"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _to_chunk(row: sqlite3.Row) -> DocumentChunk:
        return DocumentChunk(
            chunk_id=row["id"],
            organization_id=row["organization_id"],
            document_id=row["document_id"],
            version_id=row["version_id"],
            ordinal=row["ordinal"],
            heading_path=row["heading_path"],
            content=row["content"],
            token_count=row["token_count"],
            start_offset=row["start_offset"],
            end_offset=row["end_offset"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _to_chunk_with_title(row: sqlite3.Row) -> ChunkWithDocumentTitle:
        return ChunkWithDocumentTitle(
            chunk_id=row["id"],
            organization_id=row["organization_id"],
            document_id=row["document_id"],
            version_id=row["version_id"],
            ordinal=row["ordinal"],
            heading_path=row["heading_path"],
            content=row["content"],
            token_count=row["token_count"],
            document_title=row["title"],
        )

    @staticmethod
    def _to_job(row: sqlite3.Row) -> IngestionJob:
        return IngestionJob(
            job_id=row["id"],
            organization_id=row["organization_id"],
            document_id=row["document_id"],
            version_id=row["version_id"],
            status=IngestionStatus(row["status"]),
            attempt_count=row["attempt_count"],
            error_code=row["error_code"],
            error_message=row["error_message"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _to_retrieval_event(row: sqlite3.Row) -> RetrievalEvent:
        return RetrievalEvent(
            event_id=row["id"],
            organization_id=row["organization_id"],
            conversation_id=row["conversation_id"],
            strategy=row["strategy"],
            original_query=row["original_query"],
            planned_queries_json=row["planned_queries_json"],
            round_count=row["round_count"],
            candidate_json=row["candidate_json"],
            selected_chunk_ids_json=row["selected_chunk_ids_json"],
            outcome=row["outcome"],
            latency_ms=row["latency_ms"],
            model_calls=row["model_calls"],
            estimated_tokens=row["estimated_tokens"],
            created_at=row["created_at"],
        )

    # ------------------------------------------------------------ documents
    def create_document(
        self,
        *,
        organization_id: str,
        uploaded_by_user_id: str,
        title: str,
        source_type: DocumentSourceType,
    ) -> KnowledgeDocument:
        document_id = str(uuid4())
        created_at = self._now()
        try:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO documents(
                        id,
                        organization_id,
                        uploaded_by_user_id,
                        title,
                        source_type,
                        status,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        document_id,
                        organization_id,
                        uploaded_by_user_id,
                        title.strip(),
                        source_type.value,
                        DocumentStatus.PROCESSING.value,
                        created_at,
                        created_at,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise DocumentNotFoundError(organization_id, document_id) from exc
        return self.get_document(
            organization_id=organization_id,
            document_id=document_id,
        )

    def get_document(
        self,
        *,
        organization_id: str,
        document_id: str,
    ) -> KnowledgeDocument:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM documents
                WHERE organization_id = ? AND id = ?
                """,
                (organization_id, document_id),
            ).fetchone()
        if row is None:
            raise DocumentNotFoundError(organization_id, document_id)
        return self._to_document(row)

    def list_documents(
        self,
        *,
        organization_id: str,
    ) -> list[KnowledgeDocument]:
        """List the caller's documents, tenant-scoped and deterministically
        ordered (by ``created_at`` then ``id``). Another organization's rows
        can never appear."""
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM documents
                WHERE organization_id = ?
                ORDER BY created_at, id
                """,
                (organization_id,),
            ).fetchall()
        return [self._to_document(row) for row in rows]

    def _get_version_row(
        self,
        connection: sqlite3.Connection,
        *,
        organization_id: str,
        document_id: str,
        version_id: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT *
            FROM document_versions
            WHERE organization_id = ? AND document_id = ? AND id = ?
            """,
            (organization_id, document_id, version_id),
        ).fetchone()

    def get_version_by_id(
        self,
        *,
        organization_id: str,
        document_id: str,
        version_id: str,
    ) -> DocumentVersion:
        with self._connection() as connection:
            row = self._get_version_row(
                connection,
                organization_id=organization_id,
                document_id=document_id,
                version_id=version_id,
            )
        if row is None:
            raise DocumentNotFoundError(organization_id, document_id)
        return self._to_version(row)

    def set_document_status(
        self,
        *,
        organization_id: str,
        document_id: str,
        status: DocumentStatus,
    ) -> KnowledgeDocument:
        self.get_document(
            organization_id=organization_id,
            document_id=document_id,
        )
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE documents
                SET status = ?, updated_at = ?
                WHERE organization_id = ? AND id = ?
                """,
                (
                    status.value,
                    self._now(),
                    organization_id,
                    document_id,
                ),
            )
        return self.get_document(
            organization_id=organization_id,
            document_id=document_id,
        )

    def list_active_version_ids(
        self,
        *,
        organization_id: str,
    ) -> list[str]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT active_version_id
                FROM documents
                WHERE organization_id = ?
                  AND status = 'active'
                  AND active_version_id IS NOT NULL
                ORDER BY updated_at, id
                """,
                (organization_id,),
            ).fetchall()
        return [row["active_version_id"] for row in rows]

    # -------------------------------------------------------------- versions
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
        """Write a fully-parsed version row (synchronous ingestion path)."""
        version_id = str(uuid4())
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                version_no = self._next_version_no(
                    connection,
                    organization_id=organization_id,
                    document_id=document_id,
                )
                connection.execute(
                    """
                    INSERT INTO document_versions(
                        id,
                        organization_id,
                        document_id,
                        version_no,
                        content_hash,
                        raw_text,
                        loader_version,
                        chunker_version,
                        embedding_model,
                        embedding_dimensions
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        version_id,
                        organization_id,
                        document_id,
                        version_no,
                        content_hash,
                        raw_text,
                        loader_version,
                        chunker_version,
                        embedding_model,
                        embedding_dimensions,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            self._raise_duplicate_or_missing(
                exc,
                organization_id=organization_id,
                document_id=document_id,
                content_hash=content_hash,
            )
        return self.get_version_by_id(
            organization_id=organization_id,
            document_id=document_id,
            version_id=version_id,
        )

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
        """Reserve a version row before its content has been parsed.

        ``raw_bytes`` is staged in the row so a background worker can parse it later
        (and re-run after a process restart). ``content_hash``/``raw_text`` stay NULL
        until :meth:`record_version_content` backfills them and clears the payload,
        so ``content_hash IS NULL`` is the single test for "not parsed yet".
        """
        version_id = str(uuid4())
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                version_no = self._next_version_no(
                    connection,
                    organization_id=organization_id,
                    document_id=document_id,
                )
                connection.execute(
                    """
                    INSERT INTO document_versions(
                        id,
                        organization_id,
                        document_id,
                        version_no,
                        source_hash,
                        raw_bytes,
                        loader_version,
                        chunker_version,
                        embedding_model,
                        embedding_dimensions
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        version_id,
                        organization_id,
                        document_id,
                        version_no,
                        source_hash,
                        raw_bytes,
                        loader_version,
                        chunker_version,
                        embedding_model,
                        embedding_dimensions,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            self._raise_duplicate_or_missing(
                exc,
                organization_id=organization_id,
                document_id=document_id,
                source_hash=source_hash,
            )
        return self.get_version_by_id(
            organization_id=organization_id,
            document_id=document_id,
            version_id=version_id,
        )

    @staticmethod
    def _next_version_no(
        connection: sqlite3.Connection,
        *,
        organization_id: str,
        document_id: str,
    ) -> int:
        """Compute the next version number inside the caller's write transaction."""
        return connection.execute(
            """
            SELECT COALESCE(MAX(version_no), 0) + 1
            FROM document_versions
            WHERE organization_id = ? AND document_id = ?
            """,
            (organization_id, document_id),
        ).fetchone()[0]

    def _raise_duplicate_or_missing(
        self,
        exc: sqlite3.IntegrityError,
        *,
        organization_id: str,
        document_id: str,
        content_hash: str | None = None,
        source_hash: str | None = None,
    ) -> None:
        """Translate the unique-hash IntegrityError into a domain error.

        Two unique constraints can fire here: ``(org, document, content_hash)`` for
        already-ingested content and ``(org, document, source_hash)`` for the same
        uploaded bytes. Either way the caller must be able to treat the upload as
        idempotent, so both resolve to :class:`DuplicateDocumentVersionError`.
        """
        existing = None
        if content_hash is not None:
            existing = self._version_by_column_without_fk(
                organization_id=organization_id,
                document_id=document_id,
                column="content_hash",
                value=content_hash,
            )
        if existing is None and source_hash is not None:
            existing = self._version_by_column_without_fk(
                organization_id=organization_id,
                document_id=document_id,
                column="source_hash",
                value=source_hash,
            )
        if existing is not None:
            raise DuplicateDocumentVersionError(
                existing_version_id=existing.version_id
            ) from exc
        raise DocumentNotFoundError(organization_id, document_id) from exc

    def _version_by_column_without_fk(
        self,
        *,
        organization_id: str,
        document_id: str,
        column: str,
        value: str,
    ) -> DocumentVersion | None:
        """Query a version by one hash column; returns None instead of raising."""
        if column not in {"content_hash", "source_hash"}:  # pragma: no cover
            raise ValueError(f"unsupported version column: {column}")
        with self._connection() as connection:
            row = connection.execute(
                f"""
                SELECT *
                FROM document_versions
                WHERE organization_id = ?
                  AND document_id = ?
                  AND {column} = ?
                """,
                (organization_id, document_id, value),
            ).fetchone()
        if row is None:
            return None
        return self._to_version(row)

    def get_version_by_source_hash(
        self,
        *,
        organization_id: str,
        document_id: str,
        source_hash: str,
    ) -> DocumentVersion | None:
        """Return the version reserved for these exact uploaded bytes, if any."""
        return self._version_by_column_without_fk(
            organization_id=organization_id,
            document_id=document_id,
            column="source_hash",
            value=source_hash,
        )

    def record_version_content(
        self,
        *,
        organization_id: str,
        document_id: str,
        version_id: str,
        content_hash: str,
        raw_text: str,
    ) -> DocumentVersion:
        """Backfill the parsed text/hash and drop the staged upload bytes.

        Both happen in one statement so a version can never be observed as parsed
        while its (up to 2 MiB) upload payload is still being retained.
        """
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE document_versions
                SET content_hash = ?,
                    raw_text = ?,
                    raw_bytes = NULL
                WHERE organization_id = ? AND document_id = ? AND id = ?
                """,
                (content_hash, raw_text, organization_id, document_id, version_id),
            )
            if cursor.rowcount == 0:
                raise DocumentNotFoundError(organization_id, document_id)
        return self.get_version_by_id(
            organization_id=organization_id,
            document_id=document_id,
            version_id=version_id,
        )

    def read_version_raw_bytes(
        self,
        *,
        organization_id: str,
        document_id: str,
        version_id: str,
    ) -> bytes | None:
        """Read the staged upload bytes; None once parsing consumed them."""
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT raw_bytes
                FROM document_versions
                WHERE organization_id = ? AND document_id = ? AND id = ?
                """,
                (organization_id, document_id, version_id),
            ).fetchone()
        if row is None:
            raise DocumentNotFoundError(organization_id, document_id)
        stored = row["raw_bytes"]
        return None if stored is None else bytes(stored)

    def get_version_by_hash(
        self,
        *,
        organization_id: str,
        document_id: str,
        content_hash: str,
    ) -> DocumentVersion:
        """Look up a version by parsed-content hash for the caller's org+document.

        Unlike :meth:`get_version_by_source_hash` (an optional pre-parse dedup
        probe), a miss here is a lookup failure and raises
        :class:`DocumentNotFoundError` — callers rely on that to tell "no such
        content" from "found it".
        """
        version = self._version_by_column_without_fk(
            organization_id=organization_id,
            document_id=document_id,
            column="content_hash",
            value=content_hash,
        )
        if version is None:
            raise DocumentNotFoundError(organization_id, document_id)
        return version

    def list_versions(
        self,
        *,
        organization_id: str,
        document_id: str,
    ) -> list[DocumentVersion]:
        """List the versions of one document for the caller's organization,
        ordered by version number ascending. A document id that does not exist
        (or belongs to another org) yields an empty list, never a cross-tenant
        leak."""
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM document_versions
                WHERE organization_id = ? AND document_id = ?
                ORDER BY version_no, id
                """,
                (organization_id, document_id),
            ).fetchall()
        return [self._to_version(row) for row in rows]

    # ----------------------------------------------------------------- jobs
    def create_job(
        self,
        *,
        organization_id: str,
        document_id: str,
        version_id: str,
    ) -> IngestionJob:
        self.get_document(
            organization_id=organization_id,
            document_id=document_id,
        )
        self.get_version_by_id(
            organization_id=organization_id,
            document_id=document_id,
            version_id=version_id,
        )
        job_id = str(uuid4())
        try:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO ingestion_jobs(
                        id,
                        organization_id,
                        document_id,
                        version_id,
                        status,
                        attempt_count
                    )
                    VALUES (?, ?, ?, ?, ?, 0)
                    """,
                    (
                        job_id,
                        organization_id,
                        document_id,
                        version_id,
                        IngestionStatus.QUEUED.value,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise DocumentNotFoundError(organization_id, document_id) from exc
        return self.get_latest_job_for_version(
            organization_id=organization_id,
            document_id=document_id,
            version_id=version_id,
            job_id=job_id,
        )

    def get_latest_job_for_version(
        self,
        *,
        organization_id: str,
        document_id: str,
        version_id: str,
        job_id: str | None = None,
    ) -> IngestionJob | None:
        with self._connection() as connection:
            if job_id is not None:
                row = connection.execute(
                    """
                    SELECT *
                    FROM ingestion_jobs
                    WHERE organization_id = ? AND id = ?
                    """,
                    (organization_id, job_id),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT *
                    FROM ingestion_jobs
                    WHERE organization_id = ?
                      AND document_id = ?
                      AND version_id = ?
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                    """,
                    (organization_id, document_id, version_id),
                ).fetchone()
        if row is None:
            return None
        return self._to_job(row)

    def mark_job_running(
        self,
        *,
        organization_id: str,
        job_id: str,
    ) -> IngestionJob | None:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE ingestion_jobs
                SET status = 'running',
                    started_at = ?,
                    attempt_count = attempt_count + 1
                WHERE organization_id = ? AND id = ?
                """,
                (self._now(), organization_id, job_id),
            )
            if cursor.rowcount == 0:
                return None
            row = connection.execute(
                """
                SELECT *
                FROM ingestion_jobs
                WHERE organization_id = ? AND id = ?
                """,
                (organization_id, job_id),
            ).fetchone()
        return self._to_job(row)

    def claim_job(
        self,
        *,
        organization_id: str,
        job_id: str,
    ) -> IngestionJob | None:
        """Claim a QUEUED job for execution, or None when someone already did.

        The transition is guarded by ``status = 'queued'`` in the UPDATE itself, so
        two workers racing on the same job cannot both start it: SQLite serialises
        the writes and the loser sees ``rowcount == 0``. Every terminal state is
        therefore left untouched by a late or duplicated delivery.
        """
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE ingestion_jobs
                SET status = 'running',
                    started_at = ?,
                    attempt_count = attempt_count + 1
                WHERE organization_id = ?
                  AND id = ?
                  AND status = 'queued'
                """,
                (self._now(), organization_id, job_id),
            )
            if cursor.rowcount == 0:
                return None
            row = connection.execute(
                """
                SELECT *
                FROM ingestion_jobs
                WHERE organization_id = ? AND id = ?
                """,
                (organization_id, job_id),
            ).fetchone()
        return self._to_job(row)

    def recover_stale_jobs(self) -> list[tuple[str, str]]:
        """Reconcile jobs left behind by a previous process, returning work to redo.

        A ``running`` job whose version still holds its staged upload bytes is reset
        to ``queued`` and handed back to the caller for redelivery. A ``running`` job
        whose bytes are gone cannot be resumed at all (the process died before the
        version was parsed), so it is failed truthfully with ``INGESTION_INTERRUPTED``
        instead of being left as a zombie that never reaches a terminal state.
        """
        requeued: list[tuple[str, str]] = []
        interrupted_at = self._now()
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT j.id,
                       j.organization_id,
                       j.document_id,
                       j.version_id,
                       j.status,
                       v.raw_bytes IS NOT NULL AS has_bytes,
                       v.content_hash IS NOT NULL AS is_parsed
                FROM ingestion_jobs AS j
                JOIN document_versions AS v
                  ON v.organization_id = j.organization_id
                 AND v.document_id = j.document_id
                 AND v.id = j.version_id
                WHERE j.status IN ('queued', 'running')
                ORDER BY j.created_at, j.id
                """
            ).fetchall()

            for row in rows:
                recoverable = row["is_parsed"] or row["has_bytes"]
                if row["status"] == "queued" or recoverable:
                    connection.execute(
                        """
                        UPDATE ingestion_jobs
                        SET status = 'queued',
                            started_at = NULL,
                            finished_at = NULL
                        WHERE organization_id = ? AND id = ?
                        """,
                        (row["organization_id"], row["id"]),
                    )
                    requeued.append((row["organization_id"], row["id"]))
                    continue
                connection.execute(
                    """
                    UPDATE ingestion_jobs
                    SET status = 'failed',
                        error_code = ?,
                        error_message = ?,
                        finished_at = ?
                    WHERE organization_id = ? AND id = ?
                    """,
                    (
                        INTERRUPTED_ERROR_CODE,
                        INTERRUPTED_ERROR_MESSAGE,
                        interrupted_at,
                        row["organization_id"],
                        row["id"],
                    ),
                )
                connection.execute(
                    """
                    UPDATE documents
                    SET status = CASE
                            WHEN active_version_id IS NULL THEN 'failed'
                            ELSE status
                        END,
                        updated_at = ?
                    WHERE organization_id = ? AND id = ?
                    """,
                    (
                        interrupted_at,
                        row["organization_id"],
                        row["document_id"],
                    ),
                )
        return requeued

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
        """Atomically mark a job failed and reflect it on the document.

        If the document has no previously active version it becomes
        ``failed``; otherwise the old active version and ``active`` status are
        kept so prior content keeps serving retrieval.
        """
        self.get_document(
            organization_id=organization_id,
            document_id=document_id,
        )
        finished_at = self._now()
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE ingestion_jobs
                SET status = 'failed',
                    error_code = ?,
                    error_message = ?,
                    finished_at = ?
                WHERE organization_id = ? AND id = ?
                """,
                (error_code, error_message, finished_at, organization_id, job_id),
            )
            connection.execute(
                """
                UPDATE documents
                SET status = CASE
                        WHEN active_version_id IS NULL THEN 'failed'
                        ELSE status
                    END,
                    updated_at = ?
                WHERE organization_id = ? AND id = ?
                """,
                (finished_at, organization_id, document_id),
            )
        return self.get_document(
            organization_id=organization_id,
            document_id=document_id,
        )

    # ---------------------------------------------------------------- chunks
    def replace_chunks(
        self,
        *,
        organization_id: str,
        document_id: str,
        version_id: str,
        chunks: Sequence[DocumentChunk],
    ) -> None:
        # Validate the version belongs to the current org+document first.
        self.get_version_by_id(
            organization_id=organization_id,
            document_id=document_id,
            version_id=version_id,
        )
        with self._connection() as connection:
            connection.execute(
                """
                DELETE FROM document_chunks
                WHERE organization_id = ?
                  AND document_id = ?
                  AND version_id = ?
                """,
                (organization_id, document_id, version_id),
            )
            connection.executemany(
                """
                INSERT INTO document_chunks(
                    id,
                    organization_id,
                    document_id,
                    version_id,
                    ordinal,
                    heading_path,
                    content,
                    token_count,
                    start_offset,
                    end_offset
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        chunk.chunk_id,
                        organization_id,
                        document_id,
                        version_id,
                        chunk.ordinal,
                        chunk.heading_path,
                        chunk.content,
                        chunk.token_count,
                        chunk.start_offset,
                        chunk.end_offset,
                    )
                    for chunk in chunks
                ],
            )

    def list_active_chunks(
        self,
        *,
        organization_id: str,
        candidate_ids: Sequence[str],
    ) -> list[DocumentChunk]:
        if not candidate_ids:
            return []
        placeholders = ", ".join("?" for _ in candidate_ids)
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT c.*
                FROM document_chunks AS c
                JOIN documents AS d
                  ON d.organization_id = c.organization_id
                 AND d.id = c.document_id
                WHERE c.organization_id = ?
                  AND c.id IN ({placeholders})
                  AND d.status = 'active'
                  AND d.active_version_id = c.version_id
                """,
                (organization_id, *candidate_ids),
            ).fetchall()
        chunk_by_id = {row["id"]: self._to_chunk(row) for row in rows}
        return [chunk_by_id[cid] for cid in candidate_ids if cid in chunk_by_id]

    def list_version_chunks(
        self,
        *,
        organization_id: str,
        document_id: str,
        version_id: str,
    ) -> list[DocumentChunk]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM document_chunks
                WHERE organization_id = ?
                  AND document_id = ?
                  AND version_id = ?
                ORDER BY ordinal, id
                """,
                (organization_id, document_id, version_id),
            ).fetchall()
        return [self._to_chunk(row) for row in rows]

    def resolve_active_citations(
        self,
        *,
        organization_id: str,
        candidate_ids: Sequence[str],
    ) -> list[ChunkWithDocumentTitle]:
        """Second-pass citation validation joining chunks to their active doc.

        A candidate is accepted only when all four ids line up for the caller's
        organization AND the document is currently ``status='active'`` with
        ``active_version_id`` equal to the chunk's ``version_id``. Citation
        content (and the document ``title``) come only from SQLite here, never
        from a vector-store payload.
        """
        if not candidate_ids:
            return []
        placeholders = ", ".join("?" for _ in candidate_ids)
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT c.*, d.title
                FROM document_chunks AS c
                JOIN documents AS d
                  ON d.organization_id = c.organization_id
                 AND d.id = c.document_id
                 AND d.status = 'active'
                 AND d.active_version_id = c.version_id
                WHERE c.organization_id = ?
                  AND c.id IN ({placeholders})
                """,
                (organization_id, *candidate_ids),
            ).fetchall()
        return [self._to_chunk_with_title(row) for row in rows]

    # ----------------------------------------------------- retrieval events
    def record_retrieval_event(
        self,
        *,
        organization_id: str,
        event: RetrievalEvent,
    ) -> RetrievalEvent:
        """Persist a retrieval event whose JSON fields are pre-serialized.

        Only diagnostic metadata is stored; full document bodies are never
        recorded by the store.
        """
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO retrieval_events(
                    id,
                    organization_id,
                    conversation_id,
                    strategy,
                    original_query,
                    planned_queries_json,
                    round_count,
                    candidate_json,
                    selected_chunk_ids_json,
                    outcome,
                    latency_ms,
                    model_calls,
                    estimated_tokens,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    organization_id,
                    event.conversation_id,
                    event.strategy,
                    event.original_query,
                    event.planned_queries_json,
                    event.round_count,
                    event.candidate_json,
                    event.selected_chunk_ids_json,
                    event.outcome,
                    event.latency_ms,
                    event.model_calls,
                    event.estimated_tokens,
                    event.created_at,
                ),
            )
        return event

    def get_retrieval_event(
        self,
        *,
        organization_id: str,
        conversation_id: str,
    ) -> RetrievalEvent | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM retrieval_events
                WHERE organization_id = ? AND conversation_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (organization_id, conversation_id),
            ).fetchone()
        return None if row is None else self._to_retrieval_event(row)

    # ------------------------------------------------------------ activate
    def activate_version(
        self,
        *,
        organization_id: str,
        document_id: str,
        version_id: str,
        job_id: str,
    ) -> KnowledgeDocument:
        """Atomically switch the document to a version and complete its job.

        Validates the version belongs to the current org+document, updates the
        document's ``active_version_id``/status and the job's succeeded state
        in one SQLite transaction so the new version is only visible once the
        whole activation is committed.
        """
        self.get_document(
            organization_id=organization_id,
            document_id=document_id,
        )
        finished_at = self._now()
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                version_row = self._get_version_row(
                    connection,
                    organization_id=organization_id,
                    document_id=document_id,
                    version_id=version_id,
                )
                if version_row is None:
                    raise DocumentNotFoundError(organization_id, document_id)
                connection.execute(
                    """
                    UPDATE documents
                    SET active_version_id = ?,
                        status = 'active',
                        updated_at = ?
                    WHERE organization_id = ? AND id = ?
                    """,
                    (
                        version_id,
                        finished_at,
                        organization_id,
                        document_id,
                    ),
                )
                job_cursor = connection.execute(
                    """
                    UPDATE ingestion_jobs
                    SET status = 'succeeded',
                        finished_at = ?,
                        error_code = NULL,
                        error_message = NULL
                    WHERE organization_id = ?
                      AND id = ?
                      AND version_id = ?
                    """,
                    (finished_at, organization_id, job_id, version_id),
                )
                if job_cursor.rowcount == 0:
                    # The job does not belong to the version being activated;
                    # do not silently mark another version's job succeeded.
                    raise DocumentNotFoundError(organization_id, document_id)
        except sqlite3.IntegrityError as exc:
            raise DocumentNotFoundError(organization_id, document_id) from exc
        return self.get_document(
            organization_id=organization_id,
            document_id=document_id,
        )
