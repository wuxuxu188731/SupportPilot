from dataclasses import dataclass,field
import json
import pytest

from app.application.customer_support_service import (
    LogisticsAvailability,
    LogisticsDetails,
    OrderDetails,
    OrderCustomerMismatchError,
    SupportCustomerNotFoundError,
    SupportOperationRejectedError,
    SupportTicketNotFoundError,
    TicketNumberGenerationError,
)
from app.application.organization_service import TenantContext
from app.customers.base import Customer
from app.orders.base import Order, OrderStatus
from app.organizations.base import MembershipRole
from app.shipments.base import Shipment, ShipmentStatus
from app.tickets.base import (
    Ticket,
    TicketCategory,
    TicketComment,
    TicketCommentVisibility,
    TicketPriority,
    TicketStatus,
)
from app.tools.support_gateway import CustomerSupportToolGateway
from app.tools.support_results import (
    serialize_logistics_details,
    serialize_order_details,
    serialize_ticket,
    serialize_ticket_note,
    tool_failure,
    tool_success,
)



CUSTOMER = Customer(
    customer_id="internal-customer-id",
    organization_id="internal-organization-id",
    customer_no="CUST-001",
    name="林晓",
    email="linxiao@example.test",
    phone="+86-000-0000-0001",
    created_at="2026-07-20T08:00:00+00:00",
)

ORDER = Order(
    order_id="internal-order-id",
    organization_id="internal-organization-id",
    order_no="ORD-DELAY-001",
    customer_id=CUSTOMER.customer_id,
    status=OrderStatus.PROCESSING,
    item_summary="无线耳机 x1",
    total_amount_cents=39900,
    currency="CNY",
    placed_at="2026-07-23T08:00:00+00:00",
    promised_ship_at="2026-07-25T08:00:00+00:00",
    created_at="2026-07-23T08:00:00+00:00",
    updated_at="2026-07-23T08:00:00+00:00",
)

CONTEXT = TenantContext(
    user_id="user-a",
    organization_id="org-a",
    role=MembershipRole.AGENT,
)


@dataclass
class FakeSupportService:
    calls: list[tuple] = field(default_factory=list)
    fail_with: Exception | None = None

    def get_order(self, *, context, order_no):
        self.calls.append(("get_order", context, order_no))
        if self.fail_with is not None:
            raise self.fail_with
        return OrderDetails(order=ORDER, customer=CUSTOMER)

    def get_logistics(self, *, context, order_no):
        self.calls.append(
            ("get_logistics", context, order_no)
        )
        if self.fail_with is not None:
            raise self.fail_with
        return LogisticsDetails(
            order=ORDER,
            customer=CUSTOMER,
            availability=LogisticsAvailability.NOT_CREATED,
            shipment=None,
        )

    def create_ticket(self, **arguments):
        self.calls.append(("create_ticket", arguments))
        if self.fail_with is not None:
            raise self.fail_with
        return TICKET

    def add_ticket_note(self, **arguments):
        self.calls.append(("add_ticket_note", arguments))
        if self.fail_with is not None:
            raise self.fail_with
        return NOTE

TICKET = Ticket(
    ticket_id="ticket-id",
    organization_id="org-a",
    ticket_no="TKT-NEW-001",
    customer_id=CUSTOMER.customer_id,
    order_id=ORDER.order_id,
    created_by_user_id=CONTEXT.user_id,
    assigned_to_user_id=CONTEXT.user_id,
    summary="订单超过承诺时间仍未发货",
    category=TicketCategory.LOGISTICS,
    priority=TicketPriority.HIGH,
    status=TicketStatus.OPEN,
    created_at="2026-07-31T08:00:00+00:00",
    updated_at="2026-07-31T08:00:00+00:00",
)

NOTE = TicketComment(
    comment_id="comment-id",
    organization_id="org-a",
    ticket_id=TICKET.ticket_id,
    seq=1,
    author_user_id=CONTEXT.user_id,
    visibility=TicketCommentVisibility.INTERNAL,
    content="已联系仓库核查。",
    created_at="2026-07-31T08:01:00+00:00",
)


def assert_json_safe_without_internal_ids(value):
    encoded = json.dumps(value, ensure_ascii=False)
    assert "internal-" not in encoded
    for forbidden_name in (
        "organization_id",
        "customer_id",
        "order_id",
        "shipment_id",
        "ticket_id",
        "comment_id",
    ):
        assert forbidden_name not in encoded


def test_serialize_order_details_uses_business_identifiers():
    result = serialize_order_details(
        OrderDetails(order=ORDER, customer=CUSTOMER)
    )

    assert result["order"]["order_no"] == "ORD-DELAY-001"
    assert result["order"]["status"] == "processing"
    assert result["customer"]["customer_no"] == "CUST-001"
    assert_json_safe_without_internal_ids(result)


def test_serialize_logistics_not_created_is_successful_data():
    result = serialize_logistics_details(
        LogisticsDetails(
            order=ORDER,
            customer=CUSTOMER,
            availability=LogisticsAvailability.NOT_CREATED,
            shipment=None,
        )
    )

    assert result["availability"] == "not_created"
    assert result["shipment"] is None
    assert_json_safe_without_internal_ids(result)


def test_serialize_existing_logistics_uses_tracking_number():
    shipment = Shipment(
        shipment_id="internal-shipment-id",
        organization_id="internal-organization-id",
        shipment_no="SHP-TRANSIT-001",
        order_id=ORDER.order_id,
        carrier="顺丰速运",
        tracking_no="SF-DEMO-TRANSIT-001",
        status=ShipmentStatus.IN_TRANSIT,
        last_event="快件已到达目的地分拨中心",
        shipped_at="2026-07-26T08:00:00+00:00",
        estimated_delivery_at="2026-08-01T08:00:00+00:00",
        delivered_at=None,
        created_at="2026-07-26T08:00:00+00:00",
        updated_at="2026-07-26T08:00:00+00:00",
    )

    result = serialize_logistics_details(
        LogisticsDetails(
            order=ORDER,
            customer=CUSTOMER,
            availability=LogisticsAvailability.AVAILABLE,
            shipment=shipment,
        )
    )

    assert result["shipment"]["tracking_no"] == (
        "SF-DEMO-TRANSIT-001"
    )
    assert result["shipment"]["status"] == "in_transit"
    assert_json_safe_without_internal_ids(result)

def test_serialize_ticket_and_note_hide_internal_ids():
    ticket = Ticket(
        ticket_id="internal-ticket-id",
        organization_id="internal-organization-id",
        ticket_no="TKT-001",
        customer_id="internal-customer-id",
        order_id="internal-order-id",
        created_by_user_id="internal-user-id",
        assigned_to_user_id="internal-user-id",
        summary="订单超过承诺时间仍未发货",
        category=TicketCategory.LOGISTICS,
        priority=TicketPriority.HIGH,
        status=TicketStatus.OPEN,
        created_at="2026-07-31T08:00:00+00:00",
        updated_at="2026-07-31T08:00:00+00:00",
    )
    note = TicketComment(
        comment_id="internal-comment-id",
        organization_id="internal-organization-id",
        ticket_id=ticket.ticket_id,
        seq=1,
        author_user_id="internal-user-id",
        visibility=TicketCommentVisibility.INTERNAL,
        content="已联系仓库核查。",
        created_at="2026-07-31T08:01:00+00:00",
    )

    ticket_result = serialize_ticket(ticket)
    note_result = serialize_ticket_note(
        ticket_no=ticket.ticket_no,
        note=note,
    )

    assert ticket_result["ticket_no"] == "TKT-001"
    assert ticket_result["status"] == "open"
    assert note_result["ticket_no"] == "TKT-001"
    assert note_result["visibility"] == "internal"
    assert_json_safe_without_internal_ids(ticket_result)
    assert_json_safe_without_internal_ids(note_result)


def test_success_and_failure_envelopes_are_json_safe():
    success = tool_success({"value": "结果"})
    failure = tool_failure(
        code="INVALID_ARGUMENTS",
        message="tool arguments are invalid",
        details=[
            {
                "type": "missing",
                "loc": ["order_no"],
                "msg": "Field required",
            }
        ],
    )

    assert success == {
        "ok": True,
        "data": {"value": "结果"},
    }
    assert failure["ok"] is False
    assert failure["error"]["code"] == "INVALID_ARGUMENTS"
    json.dumps(success, ensure_ascii=False)
    json.dumps(failure, ensure_ascii=False)


def test_bound_create_ticket_injects_context_and_converts_enums():
    service = FakeSupportService()
    functions = CustomerSupportToolGateway(
        service=service
    ).bind(context=CONTEXT)

    result = functions["create_ticket"](
        order_no="ORD-DELAY-001",
        summary="订单超过承诺时间仍未发货",
        category="logistics",
        priority="high",
    )

    assert result["ok"] is True
    assert result["data"]["ticket_no"] == "TKT-NEW-001"
    call_name, arguments = service.calls[0]
    assert call_name == "create_ticket"
    assert arguments == {
        "context": CONTEXT,
        "summary": "订单超过承诺时间仍未发货",
        "category": TicketCategory.LOGISTICS,
        "priority": TicketPriority.HIGH,
        "customer_no": None,
        "order_no": "ORD-DELAY-001",
    }

def test_bound_add_ticket_note_uses_context_and_hides_ids():
    service = FakeSupportService()
    functions = CustomerSupportToolGateway(
        service=service
    ).bind(context=CONTEXT)

    result = functions["add_ticket_note"](
        ticket_no="TKT-NEW-001",
        content="  已联系仓库核查。  ",
    )

    assert result["ok"] is True
    assert result["data"]["ticket_no"] == "TKT-NEW-001"
    assert result["data"]["visibility"] == "internal"
    assert "comment_id" not in result["data"]
    assert service.calls == [
        (
            "add_ticket_note",
            {
                "context": CONTEXT,
                "ticket_no": "TKT-NEW-001",
                "content": "已联系仓库核查。",
            },
        )
    ]


def test_invalid_create_ticket_result_is_json_safe():
    service = FakeSupportService()
    functions = CustomerSupportToolGateway(
        service=service
    ).bind(context=CONTEXT)

    result = functions["create_ticket"](
        summary="客户问题需要创建工单",
        category="other",
        priority="medium",
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_ARGUMENTS"
    json.dumps(result, ensure_ascii=False)
    assert service.calls == []

@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (
            SupportCustomerNotFoundError("private detail"),
            "CUSTOMER_NOT_FOUND",
        ),
        (
            OrderCustomerMismatchError("private detail"),
            "ORDER_CUSTOMER_MISMATCH",
        ),
        (
            SupportTicketNotFoundError("private detail"),
            "TICKET_NOT_FOUND",
        ),
        (
            SupportOperationRejectedError("private detail"),
            "OPERATION_REJECTED",
        ),
        (
            TicketNumberGenerationError("private detail"),
            "TICKET_NUMBER_GENERATION_FAILED",
        ),
    ],
)
def test_gateway_maps_write_errors_without_private_detail(
    error,
    expected_code,
):
    service = FakeSupportService(fail_with=error)
    functions = CustomerSupportToolGateway(
        service=service
    ).bind(context=CONTEXT)

    result = functions["create_ticket"](
        order_no="ORD-DELAY-001",
        summary="订单超过承诺时间仍未发货",
        category="logistics",
        priority="high",
    )

    assert result["ok"] is False
    assert result["error"]["code"] == expected_code
    assert "private detail" not in str(result)