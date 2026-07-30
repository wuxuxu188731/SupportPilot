from pathlib import Path

from app.application.customer_support_service import (
    CustomerSupportService,
    TicketNumberFactory,
)
from app.customers.sqlite_store import SQLiteCustomerStore
from app.orders.sqlite_store import SQLiteOrderStore
from app.shipments.sqlite_store import SQLiteShipmentStore
from app.tickets.sqlite_store import SQLiteTicketStore


def create_customer_support_service(
    database_path: str | Path,
    *,
    ticket_no_factory: TicketNumberFactory | None = None,
) -> CustomerSupportService:
    return CustomerSupportService(
        customer_store=SQLiteCustomerStore(database_path),
        order_store=SQLiteOrderStore(database_path),
        shipment_store=SQLiteShipmentStore(database_path),
        ticket_store=SQLiteTicketStore(database_path),
        ticket_no_factory=ticket_no_factory,
    )
