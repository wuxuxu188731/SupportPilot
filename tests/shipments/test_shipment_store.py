import pytest

from app.customers.sqlite_store import SQLiteCustomerStore
from app.orders.base import OrderStatus
from app.orders.sqlite_store import SQLiteOrderStore
from app.organizations.sqlite_store import SQLiteOrganizationStore
from app.shipments.base import (
    InvalidShipmentReferenceError,
    ShipmentAlreadyExistsError,
    ShipmentNotFoundError,
    ShipmentStatus,
)
from app.shipments.sqlite_store import SQLiteShipmentStore
from app.users.sqlite_store import SQLiteUserStore


def build_shipment_scope(tmp_path):
    database_path = tmp_path / "app.db"
    users = SQLiteUserStore(database_path)
    organizations = SQLiteOrganizationStore(database_path)
    customers = SQLiteCustomerStore(database_path)
    orders = SQLiteOrderStore(database_path)
    shipments = SQLiteShipmentStore(database_path)
    alice = users.create_user(username="alice", password_hash="hash")
    bob = users.create_user(username="bob", password_hash="hash")
    org_a = organizations.create_with_admin(
        name="Company A",
        admin_user_id=alice.user_id,
    )
    org_b = organizations.create_with_admin(
        name="Company B",
        admin_user_id=bob.user_id,
    )
    customer_a = customers.create_customer(
        organization_id=org_a.organization_id,
        customer_no="CUST-001",
        name="A Customer",
    )
    customer_b = customers.create_customer(
        organization_id=org_b.organization_id,
        customer_no="CUST-001",
        name="B Customer",
    )
    order_a = orders.create_order(
        organization_id=org_a.organization_id,
        order_no="ORD-001",
        customer_id=customer_a.customer_id,
        status=OrderStatus.SHIPPED,
        item_summary="机械键盘 x1",
        total_amount_cents=69900,
        currency="CNY",
        placed_at="2026-07-25T08:00:00+00:00",
    )
    order_b = orders.create_order(
        organization_id=org_b.organization_id,
        order_no="ORD-001",
        customer_id=customer_b.customer_id,
        status=OrderStatus.SHIPPED,
        item_summary="机械键盘 x1",
        total_amount_cents=69900,
        currency="CNY",
        placed_at="2026-07-25T08:00:00+00:00",
    )
    return shipments, org_a, org_b, order_a, order_b


def create_shipment(store, *, organization_id, order_id):
    return store.create_shipment(
        organization_id=organization_id,
        shipment_no="SHP-001",
        order_id=order_id,
        carrier="顺丰速运",
        tracking_no="SF-DEMO-001",
        status=ShipmentStatus.IN_TRANSIT,
        last_event="运输途中",
        shipped_at="2026-07-26T08:00:00+00:00",
        estimated_delivery_at="2026-07-29T08:00:00+00:00",
    )


def test_shipment_round_trip_and_order_lookup(tmp_path):
    store, org_a, _, order_a, _ = build_shipment_scope(tmp_path)
    created = create_shipment(
        store,
        organization_id=org_a.organization_id,
        order_id=order_a.order_id,
    )

    assert store.get_by_id(
        organization_id=org_a.organization_id,
        shipment_id=created.shipment_id,
    ) == created
    assert store.get_by_no(
        organization_id=org_a.organization_id,
        shipment_no="SHP-001",
    ) == created
    assert store.get_by_order_no(
        organization_id=org_a.organization_id,
        order_no=order_a.order_no,
    ) == created


def test_shipment_is_hidden_from_other_tenant(tmp_path):
    store, org_a, org_b, order_a, _ = build_shipment_scope(tmp_path)
    create_shipment(
        store,
        organization_id=org_a.organization_id,
        order_id=order_a.order_id,
    )

    with pytest.raises(ShipmentNotFoundError):
        store.get_by_order_no(
            organization_id=org_b.organization_id,
            order_no=order_a.order_no,
        )


def test_order_allows_only_one_shipment(tmp_path):
    store, org_a, _, order_a, _ = build_shipment_scope(tmp_path)
    create_shipment(
        store,
        organization_id=org_a.organization_id,
        order_id=order_a.order_id,
    )

    with pytest.raises(ShipmentAlreadyExistsError):
        store.create_shipment(
            organization_id=org_a.organization_id,
            shipment_no="SHP-SECOND",
            order_id=order_a.order_id,
            carrier="圆通速递",
            tracking_no="YT-DEMO-002",
            status=ShipmentStatus.PENDING,
        )


def test_shipment_rejects_order_from_other_tenant(tmp_path):
    store, org_a, _, _, order_b = build_shipment_scope(tmp_path)

    with pytest.raises(InvalidShipmentReferenceError):
        create_shipment(
            store,
            organization_id=org_a.organization_id,
            order_id=order_b.order_id,
        )
