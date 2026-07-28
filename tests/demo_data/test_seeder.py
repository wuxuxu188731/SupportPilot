import sqlite3
from datetime import datetime, timezone

import pytest

from app.customers.sqlite_store import SQLiteCustomerStore
from app.demo_data.seeder import (
    DemoDataConflictError,
    DemoDataSeeder,
    DemoSeedAdminRequiredError,
)
from app.orders.base import OrderStatus
from app.orders.sqlite_store import SQLiteOrderStore
from app.organizations.base import MembershipRole
from app.organizations.sqlite_store import SQLiteOrganizationStore
from app.shipments.base import ShipmentNotFoundError, ShipmentStatus
from app.shipments.sqlite_store import SQLiteShipmentStore
from app.tickets.base import (
    TicketCategory,
    TicketCommentVisibility,
    TicketStatus,
)
from app.tickets.sqlite_store import SQLiteTicketStore
from app.users.sqlite_store import SQLiteUserStore


REFERENCE_TIME = datetime(
    2026,
    7,
    28,
    8,
    0,
    tzinfo=timezone.utc,
)


def build_seeder(tmp_path):
    database_path = tmp_path / "app.db"
    users = SQLiteUserStore(database_path)
    organizations = SQLiteOrganizationStore(database_path)
    customers = SQLiteCustomerStore(database_path)
    orders = SQLiteOrderStore(database_path)
    shipments = SQLiteShipmentStore(database_path)
    tickets = SQLiteTicketStore(database_path)
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
    organizations.add_membership(
        organization_id=org_a.organization_id,
        user_id=bob.user_id,
        role=MembershipRole.AGENT,
    )
    seeder = DemoDataSeeder(
        organization_store=organizations,
        customer_store=customers,
        order_store=orders,
        shipment_store=shipments,
        ticket_store=tickets,
    )
    return (
        database_path,
        seeder,
        customers,
        orders,
        shipments,
        tickets,
        alice,
        bob,
        org_a,
        org_b,
    )


def test_seed_creates_three_support_scenarios(tmp_path):
    (
        _,
        seeder,
        _,
        orders,
        shipments,
        tickets,
        alice,
        _,
        org_a,
        _,
    ) = build_seeder(tmp_path)

    result = seeder.seed(
        organization_id=org_a.organization_id,
        actor_user_id=alice.user_id,
        reference_time=REFERENCE_TIME,
    )

    delayed = orders.get_by_no(
        organization_id=org_a.organization_id,
        order_no="ORD-DELAY-001",
    )
    transit = orders.get_by_no(
        organization_id=org_a.organization_id,
        order_no="ORD-TRANSIT-001",
    )
    transit_shipment = shipments.get_by_order_no(
        organization_id=org_a.organization_id,
        order_no=transit.order_no,
    )
    damage_ticket = tickets.get_by_no(
        organization_id=org_a.organization_id,
        ticket_no="TKT-DAMAGE-001",
    )
    damage_comments = tickets.list_comments(
        organization_id=org_a.organization_id,
        ticket_id=damage_ticket.ticket_id,
    )

    assert result.order_nos == (
        "ORD-DELAY-001",
        "ORD-TRANSIT-001",
        "ORD-DELIVERED-001",
    )
    assert delayed.status is OrderStatus.PROCESSING
    assert datetime.fromisoformat(delayed.promised_ship_at) < REFERENCE_TIME
    with pytest.raises(ShipmentNotFoundError):
        shipments.get_by_order_no(
            organization_id=org_a.organization_id,
            order_no=delayed.order_no,
        )
    assert transit_shipment.status is ShipmentStatus.IN_TRANSIT
    assert damage_ticket.category is TicketCategory.DAMAGED_ITEM
    assert damage_ticket.status is TicketStatus.PENDING_CUSTOMER
    assert any(
        comment.visibility is TicketCommentVisibility.PUBLIC
        and "照片" in comment.content
        for comment in damage_comments
    )


def test_seed_is_idempotent(tmp_path):
    (
        database_path,
        seeder,
        _,
        _,
        _,
        _,
        alice,
        _,
        org_a,
        _,
    ) = build_seeder(tmp_path)

    first = seeder.seed(
        organization_id=org_a.organization_id,
        actor_user_id=alice.user_id,
        reference_time=REFERENCE_TIME,
    )
    second = seeder.seed(
        organization_id=org_a.organization_id,
        actor_user_id=alice.user_id,
        reference_time=REFERENCE_TIME,
    )

    with sqlite3.connect(database_path) as connection:
        counts = {
            table: connection.execute(
                f"""
                SELECT COUNT(*)
                FROM {table}
                WHERE organization_id = ?
                """,
                (org_a.organization_id,),
            ).fetchone()[0]
            for table in (
                "customers",
                "orders",
                "shipments",
                "tickets",
                "ticket_comments",
            )
        }

    assert first == second
    assert counts == {
        "customers": 2,
        "orders": 3,
        "shipments": 2,
        "tickets": 2,
        "ticket_comments": 3,
    }


def test_two_tenants_receive_isolated_copies(tmp_path):
    (
        _,
        seeder,
        _,
        orders,
        _,
        _,
        alice,
        bob,
        org_a,
        org_b,
    ) = build_seeder(tmp_path)

    seeder.seed(
        organization_id=org_a.organization_id,
        actor_user_id=alice.user_id,
        reference_time=REFERENCE_TIME,
    )
    seeder.seed(
        organization_id=org_b.organization_id,
        actor_user_id=bob.user_id,
        reference_time=REFERENCE_TIME,
    )

    order_a = orders.get_by_no(
        organization_id=org_a.organization_id,
        order_no="ORD-DELAY-001",
    )
    order_b = orders.get_by_no(
        organization_id=org_b.organization_id,
        order_no="ORD-DELAY-001",
    )

    assert order_a.order_id != order_b.order_id
    assert order_a.organization_id == org_a.organization_id
    assert order_b.organization_id == org_b.organization_id


def test_agent_cannot_seed_demo_data(tmp_path):
    (
        _,
        seeder,
        _,
        _,
        _,
        _,
        _,
        bob,
        org_a,
        _,
    ) = build_seeder(tmp_path)

    with pytest.raises(DemoSeedAdminRequiredError):
        seeder.seed(
            organization_id=org_a.organization_id,
            actor_user_id=bob.user_id,
            reference_time=REFERENCE_TIME,
        )


def test_seed_rejects_conflicting_business_number(tmp_path):
    (
        _,
        seeder,
        customers,
        _,
        _,
        _,
        alice,
        _,
        org_a,
        _,
    ) = build_seeder(tmp_path)
    customers.create_customer(
        organization_id=org_a.organization_id,
        customer_no="CUST-001",
        name="Conflicting Customer",
    )

    with pytest.raises(DemoDataConflictError):
        seeder.seed(
            organization_id=org_a.organization_id,
            actor_user_id=alice.user_id,
            reference_time=REFERENCE_TIME,
        )


def test_seed_rejects_naive_reference_time(tmp_path):
    (
        _,
        seeder,
        _,
        _,
        _,
        _,
        alice,
        _,
        org_a,
        _,
    ) = build_seeder(tmp_path)

    with pytest.raises(ValueError, match="timezone-aware"):
        seeder.seed(
            organization_id=org_a.organization_id,
            actor_user_id=alice.user_id,
            reference_time=datetime(2026, 7, 28, 8, 0),
        )
