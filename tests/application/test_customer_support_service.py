from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.application.customer_support_service import (
    CustomerSupportService,
    InvalidSupportRequestError,
    SupportDataIntegrityError,
    SupportOrderNotFoundError,
)
from app.application.customer_support_service import LogisticsAvailability
from app.application.organization_service import TenantContext
from app.customers.base import Customer, CustomerNotFoundError
from app.customers.sqlite_store import SQLiteCustomerStore
from app.application.customer_support_service import (
    OrderCustomerMismatchError,
    SupportCustomerNotFoundError,
    SupportTicketNotFoundError,
)
from app.orders.base import Order, OrderStatus
from app.orders.sqlite_store import SQLiteOrderStore
from app.organizations.base import MembershipRole
from app.organizations.sqlite_store import SQLiteOrganizationStore
from app.shipments.base import ShipmentStatus
from app.shipments.sqlite_store import SQLiteShipmentStore
from app.tickets.sqlite_store import SQLiteTicketStore
from app.tickets.base import (
    TicketCategory,
    TicketCommentVisibility,
    TicketPriority,
    TicketStatus,
)
from app.users.sqlite_store import SQLiteUserStore



@dataclass(frozen=True)
class SupportScope:
    database_path: Path
    service: CustomerSupportService
    context: TenantContext
    customer: Customer
    order: Order
    customer_store: SQLiteCustomerStore
    order_store: SQLiteOrderStore
    shipment_store: SQLiteShipmentStore
    ticket_store: SQLiteTicketStore


def build_support_scope(
    tmp_path,
    *,
    ticket_nos: Sequence[str] = ("TKT-NEW-001",),
) -> SupportScope:
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
    customer = customers.create_customer(
        organization_id=organization.organization_id,
        customer_no="CUST-001",
        name="林晓",
        email="linxiao@example.test",
        phone="+86-000-0000-0001",
    )
    order = orders.create_order(
        organization_id=organization.organization_id,
        order_no="ORD-DELAY-001",
        customer_id=customer.customer_id,
        status=OrderStatus.PROCESSING,
        item_summary="无线耳机 x1",
        total_amount_cents=39900,
        currency="CNY",
        placed_at="2026-07-23T08:00:00+00:00",
        promised_ship_at="2026-07-25T08:00:00+00:00",
    )
    number_iterator = iter(ticket_nos)
    service = CustomerSupportService(
        customer_store=customers,
        order_store=orders,
        shipment_store=shipments,
        ticket_store=tickets,
        ticket_no_factory=number_iterator.__next__,
    )
    return SupportScope(
        database_path=database_path,
        service=service,
        context=TenantContext(
            user_id=user.user_id,
            organization_id=organization.organization_id,
            role=MembershipRole.ADMIN,
        ),
        customer=customer,
        order=order,
        customer_store=customers,
        order_store=orders,
        shipment_store=shipments,
        ticket_store=tickets,
    )


def test_get_order_returns_order_and_customer_from_current_tenant(tmp_path):
    scope = build_support_scope(tmp_path)

    result = scope.service.get_order(
        context=scope.context,
        order_no="  ORD-DELAY-001  ",
    )

    assert result.order == scope.order
    assert result.customer == scope.customer


def test_get_order_rejects_blank_order_number(tmp_path):
    scope = build_support_scope(tmp_path)

    with pytest.raises(
        InvalidSupportRequestError,
        match="order_no must not be blank",
    ):
        scope.service.get_order(
            context=scope.context,
            order_no="   ",
        )


def test_get_order_maps_broken_customer_reference_to_integrity_error(
    tmp_path,
    monkeypatch,
):
    scope = build_support_scope(tmp_path)

    def raise_missing_customer(**kwargs):
        raise CustomerNotFoundError("customer not found")

    monkeypatch.setattr(
        scope.customer_store,
        "get_by_id",
        raise_missing_customer,
    )

    with pytest.raises(
        SupportDataIntegrityError,
        match="order references a missing customer",
    ):
        scope.service.get_order(
            context=scope.context,
            order_no=scope.order.order_no,
        )


@pytest.mark.parametrize(
    "organization_id",
    ["missing-organization", "another-organization"],
)
def test_get_order_hides_missing_or_other_tenant_order(
    tmp_path,
    organization_id,
):
    scope = build_support_scope(tmp_path)
    other_context = TenantContext(
        user_id=scope.context.user_id,
        organization_id=organization_id,
        role=MembershipRole.AGENT,
    )

    with pytest.raises(
        SupportOrderNotFoundError,
        match="order not found",
    ):
        scope.service.get_order(
            context=other_context,
            order_no="ORD-DELAY-001",
        )

def test_get_logistics_returns_not_created_for_order_without_shipment(
    tmp_path,
):
    scope = build_support_scope(tmp_path)

    result = scope.service.get_logistics(
        context=scope.context,
        order_no="ORD-DELAY-001",
    )

    assert result.order == scope.order
    assert result.customer == scope.customer
    assert result.availability is LogisticsAvailability.NOT_CREATED
    assert result.shipment is None


def test_get_logistics_returns_existing_shipment(tmp_path):
    scope = build_support_scope(tmp_path)
    shipment = scope.shipment_store.create_shipment(
        organization_id=scope.context.organization_id,
        shipment_no="SHP-TRANSIT-001",
        order_id=scope.order.order_id,
        carrier="顺丰速运",
        tracking_no="SF-DEMO-TRANSIT-001",
        status=ShipmentStatus.IN_TRANSIT,
        last_event="快件已到达目的地分拨中心",
        shipped_at="2026-07-26T08:00:00+00:00",
        estimated_delivery_at="2026-07-30T08:00:00+00:00",
    )

    result = scope.service.get_logistics(
        context=scope.context,
        order_no="  ORD-DELAY-001  ",
    )

    assert result.availability is LogisticsAvailability.AVAILABLE
    assert result.shipment == shipment

def test_get_logistics_raises_order_error_when_order_does_not_exist(
    tmp_path,
):
    scope = build_support_scope(tmp_path)

    with pytest.raises(SupportOrderNotFoundError):
        scope.service.get_logistics(
            context=scope.context,
            order_no="ORD-MISSING",
        )

def test_create_ticket_from_order_derives_customer_and_actor(tmp_path):
    scope = build_support_scope(tmp_path)

    ticket = scope.service.create_ticket(
        context=scope.context,
        order_no="  ORD-DELAY-001  ",
        customer_no=None,
        summary="  订单超过承诺时间仍未发货  ",
        category=TicketCategory.LOGISTICS,
        priority=TicketPriority.HIGH,
    )

    assert ticket.ticket_no == "TKT-NEW-001"
    assert ticket.organization_id == scope.context.organization_id
    assert ticket.customer_id == scope.customer.customer_id
    assert ticket.order_id == scope.order.order_id
    assert ticket.created_by_user_id == scope.context.user_id
    assert ticket.assigned_to_user_id == scope.context.user_id
    assert ticket.summary == "订单超过承诺时间仍未发货"
    assert ticket.status is TicketStatus.OPEN

def test_create_ticket_for_customer_without_order(tmp_path):
    scope = build_support_scope(tmp_path)

    ticket = scope.service.create_ticket(
        context=scope.context,
        customer_no="CUST-001",
        order_no=None,
        summary="客户咨询商品使用方法",
        category=TicketCategory.PRODUCT_QUESTION,
        priority=TicketPriority.LOW,
    )

    assert ticket.customer_id == scope.customer.customer_id
    assert ticket.order_id is None
    assert ticket.category is TicketCategory.PRODUCT_QUESTION

@pytest.mark.parametrize(
    ("summary", "customer_no", "order_no"),
    [
        ("   ", "CUST-001", None),
        ("abcd", "CUST-001", None),
        ("x" * 501, "CUST-001", None),
        ("有效工单摘要", None, None),
    ],
)
def test_create_ticket_rejects_invalid_request(
    tmp_path,
    summary,
    customer_no,
    order_no,
):
    scope = build_support_scope(tmp_path)

    with pytest.raises(InvalidSupportRequestError):
        scope.service.create_ticket(
            context=scope.context,
            customer_no=customer_no,
            order_no=order_no,
            summary=summary,
            category=TicketCategory.OTHER,
            priority=TicketPriority.MEDIUM,
        )


def test_create_ticket_rejects_missing_customer(tmp_path):
    scope = build_support_scope(tmp_path)

    with pytest.raises(
        SupportCustomerNotFoundError,
        match="customer not found",
    ):
        scope.service.create_ticket(
            context=scope.context,
            customer_no="CUST-MISSING",
            order_no=None,
            summary="客户咨询商品使用方法",
            category=TicketCategory.PRODUCT_QUESTION,
            priority=TicketPriority.LOW,
        )


def test_create_ticket_rejects_customer_that_does_not_own_order(tmp_path):
    scope = build_support_scope(tmp_path)
    another_customer = scope.customer_store.create_customer(
        organization_id=scope.context.organization_id,
        customer_no="CUST-002",
        name="陈晨",
    )

    with pytest.raises(
        OrderCustomerMismatchError,
        match="order does not belong to customer",
    ):
        scope.service.create_ticket(
            context=scope.context,
            customer_no=another_customer.customer_no,
            order_no=scope.order.order_no,
            summary="订单与客户不匹配",
            category=TicketCategory.OTHER,
            priority=TicketPriority.MEDIUM,
        )

def test_create_ticket_retries_generated_number_conflict(tmp_path):
    scope = build_support_scope(
        tmp_path,
        ticket_nos=("TKT-DUPLICATE", "TKT-RETRY-001"),
    )
    scope.ticket_store.create_ticket(
        organization_id=scope.context.organization_id,
        ticket_no="TKT-DUPLICATE",
        customer_id=scope.customer.customer_id,
        order_id=scope.order.order_id,
        created_by_user_id=scope.context.user_id,
        assigned_to_user_id=scope.context.user_id,
        summary="已经存在的工单",
        category=TicketCategory.LOGISTICS,
        priority=TicketPriority.MEDIUM,
        status=TicketStatus.OPEN,
    )

    ticket = scope.service.create_ticket(
        context=scope.context,
        order_no=scope.order.order_no,
        customer_no=None,
        summary="新创建的物流工单",
        category=TicketCategory.LOGISTICS,
        priority=TicketPriority.HIGH,
    )

    assert ticket.ticket_no == "TKT-RETRY-001"


def test_add_ticket_note_uses_current_user_and_internal_visibility(
    tmp_path,
):
    scope = build_support_scope(tmp_path)
    ticket = scope.service.create_ticket(
        context=scope.context,
        order_no=scope.order.order_no,
        customer_no=None,
        summary="订单超过承诺时间仍未发货",
        category=TicketCategory.LOGISTICS,
        priority=TicketPriority.HIGH,
    )

    note = scope.service.add_ticket_note(
        context=scope.context,
        ticket_no=f"  {ticket.ticket_no}  ",
        content="  已联系仓库核查发货状态。  ",
    )

    assert note.ticket_id == ticket.ticket_id
    assert note.author_user_id == scope.context.user_id
    assert note.visibility is TicketCommentVisibility.INTERNAL
    assert note.content == "已联系仓库核查发货状态。"
    assert note.seq == 1

@pytest.mark.parametrize(
    "content",
    ["   ", "x" * 2001],
)
def test_add_ticket_note_rejects_invalid_content(tmp_path, content):
    scope = build_support_scope(tmp_path)
    ticket = scope.service.create_ticket(
        context=scope.context,
        order_no=scope.order.order_no,
        customer_no=None,
        summary="订单超过承诺时间仍未发货",
        category=TicketCategory.LOGISTICS,
        priority=TicketPriority.HIGH,
    )

    with pytest.raises(InvalidSupportRequestError):
        scope.service.add_ticket_note(
            context=scope.context,
            ticket_no=ticket.ticket_no,
            content=content,
        )

def test_add_ticket_note_maps_missing_ticket_to_application_error(
    tmp_path,
):
    scope = build_support_scope(tmp_path)

    with pytest.raises(
        SupportTicketNotFoundError,
        match="ticket not found",
    ):
        scope.service.add_ticket_note(
            context=scope.context,
            ticket_no="TKT-MISSING",
            content="核查记录",
        )

def test_add_ticket_note_hides_ticket_from_other_tenant(tmp_path):
    scope = build_support_scope(tmp_path)
    ticket = scope.service.create_ticket(
        context=scope.context,
        order_no=scope.order.order_no,
        customer_no=None,
        summary="订单超过承诺时间仍未发货",
        category=TicketCategory.LOGISTICS,
        priority=TicketPriority.HIGH,
    )
    other_context = TenantContext(
        user_id=scope.context.user_id,
        organization_id="another-organization",
        role=MembershipRole.AGENT,
    )

    with pytest.raises(SupportTicketNotFoundError):
        scope.service.add_ticket_note(
            context=other_context,
            ticket_no=ticket.ticket_no,
            content="不应写入成功",
        )

    assert scope.ticket_store.list_comments(
        organization_id=scope.context.organization_id,
        ticket_id=ticket.ticket_id,
    ) == []