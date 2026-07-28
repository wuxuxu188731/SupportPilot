import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from app.db.migrations import upgrade_database
from app.shipments.base import (
    InvalidShipmentReferenceError,
    Shipment,
    ShipmentAlreadyExistsError,
    ShipmentNotFoundError,
    ShipmentStatus,
    ShipmentStore,
)


class SQLiteShipmentStore(ShipmentStore):
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
    def _to_shipment(row: sqlite3.Row) -> Shipment:
        return Shipment(
            shipment_id=row["id"],
            organization_id=row["organization_id"],
            shipment_no=row["shipment_no"],
            order_id=row["order_id"],
            carrier=row["carrier"],
            tracking_no=row["tracking_no"],
            status=ShipmentStatus(row["status"]),
            last_event=row["last_event"],
            shipped_at=row["shipped_at"],
            estimated_delivery_at=row["estimated_delivery_at"],
            delivered_at=row["delivered_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def create_shipment(
        self,
        *,
        organization_id: str,
        shipment_no: str,
        order_id: str,
        carrier: str,
        tracking_no: str,
        status: ShipmentStatus,
        last_event: str | None = None,
        shipped_at: str | None = None,
        estimated_delivery_at: str | None = None,
        delivered_at: str | None = None,
    ) -> Shipment:
        shipment_id = str(uuid4())
        try:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO shipments(
                        id,
                        organization_id,
                        shipment_no,
                        order_id,
                        carrier,
                        tracking_no,
                        status,
                        last_event,
                        shipped_at,
                        estimated_delivery_at,
                        delivered_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        shipment_id,
                        organization_id,
                        shipment_no.strip(),
                        order_id,
                        carrier.strip(),
                        tracking_no.strip(),
                        status.value,
                        last_event,
                        shipped_at,
                        estimated_delivery_at,
                        delivered_at,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            if "UNIQUE constraint failed" in str(exc):
                raise ShipmentAlreadyExistsError(
                    "shipment already exists"
                ) from exc
            raise InvalidShipmentReferenceError(
                "invalid shipment order or values"
            ) from exc
        return self.get_by_id(
            organization_id=organization_id,
            shipment_id=shipment_id,
        )

    def get_by_id(
        self,
        *,
        organization_id: str,
        shipment_id: str,
    ) -> Shipment:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM shipments
                WHERE organization_id = ? AND id = ?
                """,
                (organization_id, shipment_id),
            ).fetchone()
        if row is None:
            raise ShipmentNotFoundError("shipment not found")
        return self._to_shipment(row)

    def get_by_no(
        self,
        *,
        organization_id: str,
        shipment_no: str,
    ) -> Shipment:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM shipments
                WHERE organization_id = ? AND shipment_no = ?
                """,
                (organization_id, shipment_no),
            ).fetchone()
        if row is None:
            raise ShipmentNotFoundError("shipment not found")
        return self._to_shipment(row)

    def get_by_order_no(
        self,
        *,
        organization_id: str,
        order_no: str,
    ) -> Shipment:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT s.*
                FROM shipments AS s
                JOIN orders AS o
                  ON o.organization_id = s.organization_id
                 AND o.id = s.order_id
                WHERE s.organization_id = ? AND o.order_no = ?
                """,
                (organization_id, order_no),
            ).fetchone()
        if row is None:
            raise ShipmentNotFoundError("shipment not found")
        return self._to_shipment(row)
