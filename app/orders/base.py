from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class OrderStatus(str, Enum):
    PENDING_PAYMENT = "pending_payment"
    PAID = "paid"
    PROCESSING = "processing"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"


@dataclass(frozen=True)
class Order:
    order_id: str
    organization_id: str
    order_no: str
    customer_id: str
    status: OrderStatus
    item_summary: str
    total_amount_cents: int
    currency: str
    placed_at: str
    promised_ship_at: str | None
    created_at: str
    updated_at: str


class OrderNotFoundError(LookupError):
    pass


class OrderAlreadyExistsError(ValueError):
    pass


class InvalidOrderReferenceError(ValueError):
    pass


class OrderStore(Protocol):
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
        raise NotImplementedError

    def get_by_id(
        self,
        *,
        organization_id: str,
        order_id: str,
    ) -> Order:
        raise NotImplementedError

    def get_by_no(
        self,
        *,
        organization_id: str,
        order_no: str,
    ) -> Order:
        raise NotImplementedError
