from datetime import datetime, timezone

from app.application.customer_support_factory import (
    create_customer_support_service,
)
from app.application.customer_support_service import (
    LogisticsAvailability,
)
from app.application.organization_service import TenantContext
from app.customers.sqlite_store import SQLiteCustomerStore
from app.demo_data.seeder import DemoDataSeeder
from app.orders.sqlite_store import SQLiteOrderStore
from app.organizations.base import MembershipRole
from app.organizations.sqlite_store import SQLiteOrganizationStore
from app.shipments.sqlite_store import SQLiteShipmentStore
from app.tickets.base import (
    TicketCategory,
    TicketCommentVisibility,
    TicketPriority,
)
from app.tickets.sqlite_store import SQLiteTicketStore
from app.users.sqlite_store import SQLiteUserStore


def test_factory_service_completes_delayed_order_golden_path(tmp_path):
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
            29,
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
    service = create_customer_support_service(
        database_path,
        ticket_no_factory=lambda: "TKT-GOLDEN-001",
    )

    order_result = service.get_order(
        context=context,
        order_no="ORD-DELAY-001",
    )
    logistics_result = service.get_logistics(
        context=context,
        order_no="ORD-DELAY-001",
    )
    ticket = service.create_ticket(
        context=context,
        order_no=order_result.order.order_no,
        customer_no=None,
        summary="订单超过承诺时间仍未发货",
        category=TicketCategory.LOGISTICS,
        priority=TicketPriority.HIGH,
    )
    note = service.add_ticket_note(
        context=context,
        ticket_no=ticket.ticket_no,
        content="已联系仓库核查发货状态。",
    )

    assert logistics_result.availability is (
        LogisticsAvailability.NOT_CREATED
    )
    assert logistics_result.shipment is None
    assert ticket.ticket_no == "TKT-GOLDEN-001"
    assert ticket.order_id == order_result.order.order_id
    assert note.ticket_id == ticket.ticket_id
    assert note.visibility is TicketCommentVisibility.INTERNAL
