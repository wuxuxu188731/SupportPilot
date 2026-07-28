import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from app.customers.base import (
    Customer,
    CustomerAlreadyExistsError,
    CustomerNotFoundError,
    CustomerStore,
    InvalidCustomerReferenceError,
)
from app.db.migrations import upgrade_database


class SQLiteCustomerStore(CustomerStore):
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
    def _to_customer(row: sqlite3.Row) -> Customer:
        return Customer(
            customer_id=row["id"],
            organization_id=row["organization_id"],
            customer_no=row["customer_no"],
            name=row["name"],
            email=row["email"],
            phone=row["phone"],
            created_at=row["created_at"],
        )

    def create_customer(
        self,
        *,
        organization_id: str,
        customer_no: str,
        name: str,
        email: str | None = None,
        phone: str | None = None,
    ) -> Customer:
        customer_id = str(uuid4())
        try:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO customers(
                        id,
                        organization_id,
                        customer_no,
                        name,
                        email,
                        phone
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        customer_id,
                        organization_id,
                        customer_no.strip(),
                        name.strip(),
                        email,
                        phone,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            if "UNIQUE constraint failed" in str(exc):
                raise CustomerAlreadyExistsError(
                    "customer number already exists"
                ) from exc
            raise InvalidCustomerReferenceError(
                "invalid customer organization"
            ) from exc
        return self.get_by_id(
            organization_id=organization_id,
            customer_id=customer_id,
        )

    def get_by_id(
        self,
        *,
        organization_id: str,
        customer_id: str,
    ) -> Customer:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT
                    id,
                    organization_id,
                    customer_no,
                    name,
                    email,
                    phone,
                    created_at
                FROM customers
                WHERE organization_id = ? AND id = ?
                """,
                (organization_id, customer_id),
            ).fetchone()
        if row is None:
            raise CustomerNotFoundError("customer not found")
        return self._to_customer(row)

    def get_by_no(
        self,
        *,
        organization_id: str,
        customer_no: str,
    ) -> Customer:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT
                    id,
                    organization_id,
                    customer_no,
                    name,
                    email,
                    phone,
                    created_at
                FROM customers
                WHERE organization_id = ? AND customer_no = ?
                """,
                (organization_id, customer_no),
            ).fetchone()
        if row is None:
            raise CustomerNotFoundError("customer not found")
        return self._to_customer(row)
