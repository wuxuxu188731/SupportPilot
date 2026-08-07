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
        self.get_document(
            organization_id=organization_id,
            document_id=document_id,
        )
        version_id = str(uuid4())
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                version_no = (
                    connection.execute(
                        """
                        SELECT COALESCE(MAX(version_no), 0) + 1
                        FROM document_versions
                        WHERE organization_id = ? AND document_id = ?
                        """,
                        (organization_id, document_id),
                    ).fetchone()[0]
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
            # A unique conflict on (org, document, content_hash): the same
            # content was already ingested. Return the existing version.
            if "uq_document_versions_org_document_hash" in str(exc) or (
                "UNIQUE constraint failed" in str(exc)
            ):
                existing = self._version_by_hash_without_fk(
                    organization_id=organization_id,
                    document_id=document_id,
                    content_hash=content_hash,
                )
                if existing is not None:
                    raise DuplicateDocumentVersionError(
                        existing_version_id=existing.version_id
                    ) from exc
            raise DocumentNotFoundError(organization_id, document_id) from exc
        return self.get_version_by_id(
            organization_id=organization_id,
            document_id=document_id,
            version_id=version_id,
        )

    def _version_by_hash_without_fk(
        self,
        *,
        organization_id: str,
        document_id: str,
        content_hash: str,
    ) -> DocumentVersion | None:
        """Query a version by hash; returns None instead of raising."""
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM document_versions
                WHERE organization_id = ?
                  AND document_id = ?
                  AND content_hash = ?
                """,
                (organization_id, document_id, content_hash),
            ).fetchone()
        if row is None:
            return None
        return self._to_version(row)

    def get_version_by_hash(
        self,
        *,
        organization_id: str,
        document_id: str,
        content_hash: str,
    ) -> DocumentVersion:
        version = self._version_by_hash_without_fk(
            organization_id=organization_id,
            document_id=document_id,
            content_hash=content_hash,
        )
        if version is None:
            raise DocumentNotFoundError(organization_id, document_id)
        return version

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
                    estimated_tokens
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                ),
            )
        return event

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
