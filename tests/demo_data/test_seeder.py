import sqlite3
from datetime import datetime, timedelta, timezone

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

    # 保护行为：重复初始化不新增任何行，且两次返回的业务数据完全一致；
    # 唯一允许变化的只有 created_counts——它如实反映「这次没新增」。
    assert first.created_counts == {
        "customers": 2,
        "orders": 3,
        "shipments": 2,
        "tickets": 2,
        "ticket_comments": 3,
    }
    assert second.created_counts == {
        "customers": 0,
        "orders": 0,
        "shipments": 0,
        "tickets": 0,
        "ticket_comments": 0,
    }
    for field_name in (
        "organization_id",
        "customer_nos",
        "order_nos",
        "shipment_nos",
        "ticket_nos",
        "reference_at",
    ):
        assert getattr(first, field_name) == getattr(second, field_name)
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


def test_refresh_times_realigns_expired_dates(tmp_path):
    # 保护行为：refresh_times=True 时，已有演示数据的时间列被重新对齐到新的
    # 时间基准（修复「放久后承诺发货时间/预计送达时间过期」），而状态、金额、
    # 客户与商品摘要等业务字段保持不变，且不新增任何行。
    (
        _,
        seeder,
        _,
        orders,
        shipments,
        _,
        alice,
        _,
        org_a,
        _,
    ) = build_seeder(tmp_path)
    later = REFERENCE_TIME + timedelta(days=30)

    first = seeder.seed(
        organization_id=org_a.organization_id,
        actor_user_id=alice.user_id,
        reference_time=REFERENCE_TIME,
    )
    delayed_before = orders.get_by_no(
        organization_id=org_a.organization_id,
        order_no="ORD-DELAY-001",
    )
    transit_before = shipments.get_by_no(
        organization_id=org_a.organization_id,
        shipment_no="SHP-TRANSIT-001",
    )

    second = seeder.seed(
        organization_id=org_a.organization_id,
        actor_user_id=alice.user_id,
        reference_time=later,
        refresh_times=True,
    )

    delayed_after = orders.get_by_no(
        organization_id=org_a.organization_id,
        order_no="ORD-DELAY-001",
    )
    transit_after = shipments.get_by_no(
        organization_id=org_a.organization_id,
        shipment_no="SHP-TRANSIT-001",
    )

    # 时间列已按新的 T 重算：承诺发货时间仍然早于新 T，预计送达仍然晚于新 T。
    assert datetime.fromisoformat(delayed_after.promised_ship_at) == (
        later - timedelta(days=3)
    )
    assert datetime.fromisoformat(delayed_after.promised_ship_at) > (
        datetime.fromisoformat(delayed_before.promised_ship_at)
    )
    assert datetime.fromisoformat(
        transit_after.estimated_delivery_at
    ) == later + timedelta(days=1)
    assert transit_after.delivered_at is None

    # 业务字段一个都没被碰。
    assert delayed_after.status is delayed_before.status
    assert delayed_after.total_amount_cents == delayed_before.total_amount_cents
    assert delayed_after.item_summary == delayed_before.item_summary
    assert delayed_after.customer_id == delayed_before.customer_id
    assert transit_after.status is transit_before.status
    assert transit_after.carrier == transit_before.carrier
    assert transit_after.tracking_no == transit_before.tracking_no

    # 刷新不产生新行，且第二次调用如实报告「新增 0」。
    assert second.created_counts == {
        "customers": 0,
        "orders": 0,
        "shipments": 0,
        "tickets": 0,
        "ticket_comments": 0,
    }
    assert first.ticket_nos == second.ticket_nos
    assert second.reference_at == later.isoformat()


def test_seed_without_refresh_keeps_existing_times(tmp_path):
    # 边界：默认 refresh_times=False 时必须保持原有幂等语义，
    # 即传入更晚的时间基准也不会改写已有演示数据的时间列。
    (
        _,
        seeder,
        _,
        orders,
        _,
        _,
        alice,
        _,
        org_a,
        _,
    ) = build_seeder(tmp_path)

    seeder.seed(
        organization_id=org_a.organization_id,
        actor_user_id=alice.user_id,
        reference_time=REFERENCE_TIME,
    )
    before = orders.get_by_no(
        organization_id=org_a.organization_id,
        order_no="ORD-TRANSIT-001",
    )

    seeder.seed(
        organization_id=org_a.organization_id,
        actor_user_id=alice.user_id,
        reference_time=REFERENCE_TIME + timedelta(days=30),
    )
    after = orders.get_by_no(
        organization_id=org_a.organization_id,
        order_no="ORD-TRANSIT-001",
    )

    assert after.placed_at == before.placed_at
    assert after.promised_ship_at == before.promised_ship_at


def test_refresh_times_only_touches_demo_business_numbers(tmp_path):
    # 边界：刷新只作用于固定演示编号，体验者自建的订单时间不得被改写。
    (
        database_path,
        seeder,
        customers,
        orders,
        _,
        _,
        alice,
        _,
        org_a,
        _,
    ) = build_seeder(tmp_path)
    seeder.seed(
        organization_id=org_a.organization_id,
        actor_user_id=alice.user_id,
        reference_time=REFERENCE_TIME,
    )
    customer = customers.get_by_no(
        organization_id=org_a.organization_id,
        customer_no="CUST-001",
    )
    custom = orders.create_order(
        organization_id=org_a.organization_id,
        order_no="ORD-MANUAL-001",
        customer_id=customer.customer_id,
        status=OrderStatus.PROCESSING,
        item_summary="体验者自建订单 x1",
        total_amount_cents=1000,
        currency="CNY",
        placed_at="2020-01-01T00:00:00+00:00",
        promised_ship_at="2020-01-02T00:00:00+00:00",
    )

    seeder.seed(
        organization_id=org_a.organization_id,
        actor_user_id=alice.user_id,
        reference_time=REFERENCE_TIME + timedelta(days=30),
        refresh_times=True,
    )

    untouched = orders.get_by_no(
        organization_id=org_a.organization_id,
        order_no=custom.order_no,
    )
    assert untouched.placed_at == "2020-01-01T00:00:00+00:00"
    assert untouched.promised_ship_at == "2020-01-02T00:00:00+00:00"
    assert database_path.exists()
