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

    def update_demo_times(
        self,
        *,
        organization_id: str,
        order_no: str,
        placed_at: str,
        promised_ship_at: str | None,
    ) -> int:
        """只更新演示数据的时间列，返回被更新的行数。

        供演示数据初始化把「会过期的日期」重新对齐到当前时间基准；不得
        修改状态、金额、客户等业务字段，也不得用于业务写路径。
        """
        raise NotImplementedError
