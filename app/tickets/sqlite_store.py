import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from app.db.migrations import upgrade_database
from app.tickets.base import (
    InvalidTicketCommentReferenceError,
    InvalidTicketReferenceError,
    Ticket,
    TicketAlreadyExistsError,
    TicketCategory,
    TicketComment,
    TicketCommentVisibility,
    TicketNotFoundError,
    TicketPriority,
    TicketStatus,
    TicketStore,
)


class SQLiteTicketStore(TicketStore):
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
    def _to_ticket(row: sqlite3.Row) -> Ticket:
        return Ticket(
            ticket_id=row["id"],
            organization_id=row["organization_id"],
            ticket_no=row["ticket_no"],
            customer_id=row["customer_id"],
            order_id=row["order_id"],
            created_by_user_id=row["created_by_user_id"],
            assigned_to_user_id=row["assigned_to_user_id"],
            summary=row["summary"],
            category=TicketCategory(row["category"]),
            priority=TicketPriority(row["priority"]),
            status=TicketStatus(row["status"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _to_comment(row: sqlite3.Row) -> TicketComment:
        return TicketComment(
            comment_id=row["id"],
            organization_id=row["organization_id"],
            ticket_id=row["ticket_id"],
            seq=row["seq"],
            author_user_id=row["author_user_id"],
            visibility=TicketCommentVisibility(row["visibility"]),
            content=row["content"],
            created_at=row["created_at"],
        )

    def create_ticket(
        self,
        *,
        organization_id: str,
        ticket_no: str,
        customer_id: str,
        order_id: str | None,
        created_by_user_id: str,
        assigned_to_user_id: str | None,
        summary: str,
        category: TicketCategory,
        priority: TicketPriority,
        status: TicketStatus,
    ) -> Ticket:
        ticket_id = str(uuid4())
        try:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO tickets(
                        id,
                        organization_id,
                        ticket_no,
                        customer_id,
                        order_id,
                        created_by_user_id,
                        assigned_to_user_id,
                        summary,
                        category,
                        priority,
                        status
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ticket_id,
                        organization_id,
                        ticket_no.strip(),
                        customer_id,
                        order_id,
                        created_by_user_id,
                        assigned_to_user_id,
                        summary.strip(),
                        category.value,
                        priority.value,
                        status.value,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            if "UNIQUE constraint failed" in str(exc):
                raise TicketAlreadyExistsError(
                    "ticket number already exists"
                ) from exc
            raise InvalidTicketReferenceError(
                "invalid ticket references or values"
            ) from exc
        return self.get_by_id(
            organization_id=organization_id,
            ticket_id=ticket_id,
        )

    def get_by_id(
        self,
        *,
        organization_id: str,
        ticket_id: str,
    ) -> Ticket:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM tickets
                WHERE organization_id = ? AND id = ?
                """,
                (organization_id, ticket_id),
            ).fetchone()
        if row is None:
            raise TicketNotFoundError("ticket not found")
        return self._to_ticket(row)

    def get_by_no(
        self,
        *,
        organization_id: str,
        ticket_no: str,
    ) -> Ticket:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM tickets
                WHERE organization_id = ? AND ticket_no = ?
                """,
                (organization_id, ticket_no),
            ).fetchone()
        if row is None:
            raise TicketNotFoundError("ticket not found")
        return self._to_ticket(row)

    def add_comment(
        self,
        *,
        organization_id: str,
        ticket_id: str,
        author_user_id: str,
        visibility: TicketCommentVisibility,
        content: str,
    ) -> TicketComment:
        self.get_by_id(
            organization_id=organization_id,
            ticket_id=ticket_id,
        )
        comment_id = str(uuid4())
        created_at = datetime.now(timezone.utc).isoformat()
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                last_seq = connection.execute(
                    """
                    SELECT COALESCE(MAX(seq), 0)
                    FROM ticket_comments
                    WHERE organization_id = ? AND ticket_id = ?
                    """,
                    (organization_id, ticket_id),
                ).fetchone()[0]
                next_seq = last_seq + 1
                connection.execute(
                    """
                    INSERT INTO ticket_comments(
                        id,
                        organization_id,
                        ticket_id,
                        seq,
                        author_user_id,
                        visibility,
                        content,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        comment_id,
                        organization_id,
                        ticket_id,
                        next_seq,
                        author_user_id,
                        visibility.value,
                        content.strip(),
                        created_at,
                    ),
                )
                row = connection.execute(
                    """
                    SELECT *
                    FROM ticket_comments
                    WHERE organization_id = ? AND id = ?
                    """,
                    (organization_id, comment_id),
                ).fetchone()
        except sqlite3.IntegrityError as exc:
            raise InvalidTicketCommentReferenceError(
                "invalid ticket comment references or content"
            ) from exc
        if row is None:
            raise RuntimeError("created ticket comment could not be reloaded")
        return self._to_comment(row)

    def list_comments(
        self,
        *,
        organization_id: str,
        ticket_id: str,
    ) -> list[TicketComment]:
        self.get_by_id(
            organization_id=organization_id,
            ticket_id=ticket_id,
        )
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM ticket_comments
                WHERE organization_id = ? AND ticket_id = ?
                ORDER BY seq ASC
                """,
                (organization_id, ticket_id),
            ).fetchall()
        return [self._to_comment(row) for row in rows]
