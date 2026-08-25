import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from app.db.migrations import upgrade_database
from app.organizations.base import (
    Membership,
    MembershipAlreadyExistsError,
    MembershipNotFoundError,
    MembershipRole,
    Organization,
    OrganizationAccess,
    OrganizationStore,
)


class SQLiteOrganizationStore(OrganizationStore):
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
    def _to_membership(row: sqlite3.Row) -> Membership:
        return Membership(
            organization_id=row["organization_id"],
            user_id=row["user_id"],
            role=MembershipRole(row["role"]),
            created_at=row["created_at"],
        )

    def create_with_admin(
        self,
        *,
        name: str,
        admin_user_id: str,
    ) -> Organization:
        organization_id = str(uuid4())
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO organizations(id, name)
                VALUES (?, ?)
                """,
                (organization_id, name),
            )
            connection.execute(
                """
                INSERT INTO memberships(organization_id, user_id, role)
                VALUES (?, ?, ?)
                """,
                (
                    organization_id,
                    admin_user_id,
                    MembershipRole.ADMIN.value,
                ),
            )
            row = connection.execute(
                """
                SELECT id, name, created_at
                FROM organizations
                WHERE id = ?
                """,
                (organization_id,),
            ).fetchone()

        if row is None:
            raise RuntimeError("created organization could not be reloaded")
        return Organization(
            organization_id=row["id"],
            name=row["name"],
            created_at=row["created_at"],
        )

    def list_for_user(self, *, user_id: str) -> list[OrganizationAccess]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT o.id, o.name, m.role
                FROM memberships AS m
                JOIN organizations AS o ON o.id = m.organization_id
                WHERE m.user_id = ?
                ORDER BY o.created_at, o.id
                """,
                (user_id,),
            ).fetchall()
        return [
            OrganizationAccess(
                organization_id=row["id"],
                name=row["name"],
                role=MembershipRole(row["role"]),
            )
            for row in rows
        ]

    def get_membership(
        self,
        *,
        organization_id: str,
        user_id: str,
    ) -> Membership:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT organization_id, user_id, role, created_at
                FROM memberships
                WHERE organization_id = ? AND user_id = ?
                """,
                (organization_id, user_id),
            ).fetchone()
        if row is None:
            raise MembershipNotFoundError("membership not found")
        return self._to_membership(row)

    def add_membership(
        self,
        *,
        organization_id: str,
        user_id: str,
        role: MembershipRole,
    ) -> Membership:
        try:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO memberships(organization_id, user_id, role)
                    VALUES (?, ?, ?)
                    """,
                    (organization_id, user_id, role.value),
                )
        except sqlite3.IntegrityError as exc:
            raise MembershipAlreadyExistsError(
                "membership already exists"
            ) from exc
        return self.get_membership(
            organization_id=organization_id,
            user_id=user_id,
        )
