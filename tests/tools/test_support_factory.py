from datetime import datetime, timezone

from app.application.organization_service import TenantContext
from app.customers.sqlite_store import SQLiteCustomerStore
from app.demo_data.seeder import DemoDataSeeder
from app.orders.sqlite_store import SQLiteOrderStore
from app.organizations.base import MembershipRole
from app.organizations.sqlite_store import SQLiteOrganizationStore
from app.shipments.sqlite_store import SQLiteShipmentStore
from app.tickets.sqlite_store import SQLiteTicketStore
from app.tools.support_factory import (
    create_customer_support_tool_gateway,
)
from app.users.sqlite_store import SQLiteUserStore


def test_gateway_factory_completes_four_tool_golden_path(
    tmp_path,
):
    database_path = tmp_path / "support.db"
    users = SQLiteUserStore(database_path)
    organizations = SQLiteOrganizationStore(database_path)
    customers = SQLiteCustomerStore(database_path)
    orders = SQLiteOrderStore(database_path)
    shipments = SQLiteShipmentStore(database_path)
    tickets = SQLiteTicketStore(database_path)

    user = users.create_user(
        username="alice",
        password_hash="hash",
    )
    organization = organizations.create_with_admin(
        name="Company A",
        admin_user_id=user.user_id,
    )
    DemoDataSeeder(
        organization_store=organizations,
        customer_store=customers,
        order_store=orders,
        shipment_store=shipments,
        ticket_store=tickets,
    ).seed(
        organization_id=organization.organization_id,
        actor_user_id=user.user_id,
        reference_time=datetime(
            2026,
            7,
            31,
            8,
            0,
            tzinfo=timezone.utc,
        ),
    )
    context = TenantContext(
        user_id=user.user_id,
        organization_id=organization.organization_id,
        role=MembershipRole.ADMIN,
    )
    gateway = create_customer_support_tool_gateway(
        database_path,
        ticket_no_factory=lambda: "TKT-GATEWAY-001",
    )
    functions = gateway.bind(context=context)

    order_result = functions["get_order"](
        order_no="ORD-DELAY-001",
    )
    logistics_result = functions["get_logistics"](
        order_no="ORD-DELAY-001",
    )
    ticket_result = functions["create_ticket"](
        order_no="ORD-DELAY-001",
        summary="订单超过承诺时间仍未发货",
        category="logistics",
        priority="high",
    )
    note_result = functions["add_ticket_note"](
        ticket_no="TKT-GATEWAY-001",
        content="已联系仓库核查发货状态。",
    )

    assert order_result["ok"] is True
    assert logistics_result["data"]["availability"] == (
        "not_created"
    )
    assert ticket_result["data"]["ticket_no"] == (
        "TKT-GATEWAY-001"
    )
    assert note_result["data"]["visibility"] == "internal"
    assert len(gateway.definitions) == 4
