import pytest

from app.customers.sqlite_store import SQLiteCustomerStore
from app.orders.base import (
    InvalidOrderReferenceError,
    OrderAlreadyExistsError,
    OrderNotFoundError,
    OrderStatus,
)
from app.orders.sqlite_store import SQLiteOrderStore
from app.organizations.sqlite_store import SQLiteOrganizationStore
from app.users.sqlite_store import SQLiteUserStore


def build_order_scope(tmp_path):
    database_path = tmp_path / "app.db"
    users = SQLiteUserStore(database_path)
    organizations = SQLiteOrganizationStore(database_path)
    customers = SQLiteCustomerStore(database_path)
    orders = SQLiteOrderStore(database_path)
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
        name="Alice Customer",
    )
    customer_b = customers.create_customer(
        organization_id=org_b.organization_id,
        customer_no="CUST-001",
        name="Bob Customer",
    )
    return orders, org_a, org_b, customer_a, customer_b


def create_order(store, *, organization_id, customer_id, order_no):
    return store.create_order(
        organization_id=organization_id,
        order_no=order_no,
        customer_id=customer_id,
        status=OrderStatus.PROCESSING,
        item_summary="无线耳机 x1",
        total_amount_cents=39900,
        currency="CNY",
        placed_at="2026-07-23T08:00:00+00:00",
        promised_ship_at="2026-07-25T08:00:00+00:00",
    )


def test_order_round_trip_and_tenant_isolation(tmp_path):
    store, org_a, org_b, customer_a, _ = build_order_scope(tmp_path)
    created = create_order(
        store,
        organization_id=org_a.organization_id,
        customer_id=customer_a.customer_id,
        order_no="ORD-001",
    )

    assert store.get_by_id(
        organization_id=org_a.organization_id,
        order_id=created.order_id,
    ) == created
    assert store.get_by_no(
        organization_id=org_a.organization_id,
        order_no="ORD-001",
    ) == created
    with pytest.raises(OrderNotFoundError):
        store.get_by_no(
            organization_id=org_b.organization_id,
            order_no="ORD-001",
        )


def test_order_number_is_unique_only_inside_tenant(tmp_path):
    store, org_a, org_b, customer_a, customer_b = build_order_scope(tmp_path)
    create_order(
        store,
        organization_id=org_a.organization_id,
        customer_id=customer_a.customer_id,
        order_no="ORD-001",
    )
    create_order(
        store,
        organization_id=org_b.organization_id,
        customer_id=customer_b.customer_id,
        order_no="ORD-001",
    )

    with pytest.raises(OrderAlreadyExistsError):
        create_order(
            store,
            organization_id=org_a.organization_id,
            customer_id=customer_a.customer_id,
            order_no="ORD-001",
        )


def test_order_rejects_customer_from_other_tenant(tmp_path):
    store, org_a, _, _, customer_b = build_order_scope(tmp_path)

    with pytest.raises(InvalidOrderReferenceError):
        create_order(
            store,
            organization_id=org_a.organization_id,
            customer_id=customer_b.customer_id,
            order_no="ORD-CROSS-TENANT",
        )


@pytest.mark.parametrize(
    ("status", "amount"),
    [
        (OrderStatus.PROCESSING, -1),
    ],
)
def test_order_rejects_invalid_values(tmp_path, status, amount):
    store, org_a, _, customer_a, _ = build_order_scope(tmp_path)

    with pytest.raises(InvalidOrderReferenceError):
        store.create_order(
            organization_id=org_a.organization_id,
            order_no="ORD-INVALID",
            customer_id=customer_a.customer_id,
            status=status,
            item_summary="invalid amount",
            total_amount_cents=amount,
            currency="CNY",
            placed_at="2026-07-23T08:00:00+00:00",
        )
