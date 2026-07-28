import pytest

from app.customers.base import (
    CustomerAlreadyExistsError,
    CustomerNotFoundError,
    InvalidCustomerReferenceError,
)
from app.customers.sqlite_store import SQLiteCustomerStore
from app.organizations.sqlite_store import SQLiteOrganizationStore
from app.users.sqlite_store import SQLiteUserStore


def build_two_tenants(tmp_path):
    database_path = tmp_path / "app.db"
    users = SQLiteUserStore(database_path)
    organizations = SQLiteOrganizationStore(database_path)
    customers = SQLiteCustomerStore(database_path)
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
    return customers, org_a, org_b


def test_customer_round_trip_is_tenant_scoped(tmp_path):
    store, org_a, org_b = build_two_tenants(tmp_path)
    created = store.create_customer(
        organization_id=org_a.organization_id,
        customer_no="CUST-001",
        name="林晓",
        email="linxiao@example.test",
        phone="+86-000-0000-0001",
    )

    assert store.get_by_id(
        organization_id=org_a.organization_id,
        customer_id=created.customer_id,
    ) == created
    assert store.get_by_no(
        organization_id=org_a.organization_id,
        customer_no="CUST-001",
    ) == created
    with pytest.raises(CustomerNotFoundError):
        store.get_by_id(
            organization_id=org_b.organization_id,
            customer_id=created.customer_id,
        )


def test_customer_number_is_unique_only_inside_tenant(tmp_path):
    store, org_a, org_b = build_two_tenants(tmp_path)
    first = store.create_customer(
        organization_id=org_a.organization_id,
        customer_no="CUST-001",
        name="Company A Customer",
    )
    second = store.create_customer(
        organization_id=org_b.organization_id,
        customer_no="CUST-001",
        name="Company B Customer",
    )

    assert first.organization_id != second.organization_id
    with pytest.raises(CustomerAlreadyExistsError):
        store.create_customer(
            organization_id=org_a.organization_id,
            customer_no="CUST-001",
            name="Duplicate",
        )


def test_customer_rejects_unknown_organization(tmp_path):
    database_path = tmp_path / "app.db"
    store = SQLiteCustomerStore(database_path)

    with pytest.raises(InvalidCustomerReferenceError):
        store.create_customer(
            organization_id="missing-organization",
            customer_no="CUST-001",
            name="Invalid",
        )
