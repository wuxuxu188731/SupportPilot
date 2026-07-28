from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class ShipmentStatus(str, Enum):
    PENDING = "pending"
    PICKED_UP = "picked_up"
    IN_TRANSIT = "in_transit"
    DELIVERED = "delivered"
    EXCEPTION = "exception"
    RETURNED = "returned"


@dataclass(frozen=True)
class Shipment:
    shipment_id: str
    organization_id: str
    shipment_no: str
    order_id: str
    carrier: str
    tracking_no: str
    status: ShipmentStatus
    last_event: str | None
    shipped_at: str | None
    estimated_delivery_at: str | None
    delivered_at: str | None
    created_at: str
    updated_at: str


class ShipmentNotFoundError(LookupError):
    pass


class ShipmentAlreadyExistsError(ValueError):
    pass


class InvalidShipmentReferenceError(ValueError):
    pass


class ShipmentStore(Protocol):
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
        raise NotImplementedError

    def get_by_id(
        self,
        *,
        organization_id: str,
        shipment_id: str,
    ) -> Shipment:
        raise NotImplementedError

    def get_by_no(
        self,
        *,
        organization_id: str,
        shipment_no: str,
    ) -> Shipment:
        raise NotImplementedError

    def get_by_order_no(
        self,
        *,
        organization_id: str,
        order_no: str,
    ) -> Shipment:
        raise NotImplementedError
