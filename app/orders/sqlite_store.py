import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from app.db.migrations import upgrade_database
from app.orders.base import (
    InvalidOrderReferenceError,
    Order,
    OrderAlreadyExistsError,
    OrderNotFoundError,
    OrderStatus,
    OrderStore,
)


class SQLiteOrderStore(OrderStore):
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
    def _to_order(row: sqlite3.Row) -> Order:
        return Order(
            order_id=row["id"],
            organization_id=row["organization_id"],
            order_no=row["order_no"],
            customer_id=row["customer_id"],
            status=OrderStatus(row["status"]),
            item_summary=row["item_summary"],
            total_amount_cents=row["total_amount_cents"],
            currency=row["currency"],
            placed_at=row["placed_at"],
            promised_ship_at=row["promised_ship_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def create_order(
        self,
        *,
        organization_id: str,
        order_no: str,
        customer_id: str,
        status: OrderStatus,
        item_summary: str,
        total_amount_cents: int,
        currency: str,
        placed_at: str,
        promised_ship_at: str | None = None,
    ) -> Order:
        order_id = str(uuid4())
        try:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO orders(
                        id,
                        organization_id,
                        order_no,
                        customer_id,
                        status,
                        item_summary,
                        total_amount_cents,
                        currency,
                        placed_at,
                        promised_ship_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        order_id,
                        organization_id,
                        order_no.strip(),
                        customer_id,
                        status.value,
                        item_summary.strip(),
                        total_amount_cents,
                        currency.strip().upper(),
                        placed_at,
                        promised_ship_at,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            if "UNIQUE constraint failed" in str(exc):
                raise OrderAlreadyExistsError(
                    "order number already exists"
                ) from exc
            raise InvalidOrderReferenceError(
                "invalid order customer or values"
            ) from exc
        return self.get_by_id(
            organization_id=organization_id,
            order_id=order_id,
        )

    def get_by_id(
        self,
        *,
        organization_id: str,
        order_id: str,
    ) -> Order:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM orders
                WHERE organization_id = ? AND id = ?
                """,
                (organization_id, order_id),
            ).fetchone()
        if row is None:
            raise OrderNotFoundError("order not found")
        return self._to_order(row)

    def get_by_no(
        self,
        *,
        organization_id: str,
        order_no: str,
    ) -> Order:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM orders
                WHERE organization_id = ? AND order_no = ?
                """,
                (organization_id, order_no),
            ).fetchone()
        if row is None:
            raise OrderNotFoundError("order not found")
        return self._to_order(row)
