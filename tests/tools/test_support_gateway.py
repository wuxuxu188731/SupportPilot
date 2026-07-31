from dataclasses import dataclass, field
import json
from unittest import mock

import pytest

from app.application.customer_support_service import (
    LogisticsAvailability,
    LogisticsDetails,
    OrderDetails,
    SupportOrderNotFoundError,
)
from app.application.organization_service import TenantContext
from app.customers.base import Customer
from app.orders.base import Order, OrderStatus
from app.organizations.base import MembershipRole
from app.tools.support_gateway import CustomerSupportToolGateway


CUSTOMER = Customer(
    customer_id="customer-id",
    organization_id="org-a",
    customer_no="CUST-001",
    name="林晓",
    email="linxiao@example.test",
    phone=None,
    created_at="2026-07-20T08:00:00+00:00",
)

ORDER = Order(
    order_id="order-id",
    organization_id="org-a",
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


def test_bound_get_order_uses_server_context():
    service = FakeSupportService()
    functions = CustomerSupportToolGateway(
        service=service
    ).bind(context=CONTEXT)

    result = functions["get_order"](
        order_no="  ORD-DELAY-001  ",
    )

    assert result["ok"] is True
    assert result["data"]["order"]["order_no"] == (
        "ORD-DELAY-001"
    )
    assert service.calls == [
        ("get_order", CONTEXT, "ORD-DELAY-001")
    ]


@pytest.mark.parametrize(
    "forbidden_name",
    [
        "organization_id",
        "user_id",
        "actor_user_id",
        "role",
        "context",
    ],
)
def test_bound_tool_rejects_model_controlled_context(
    forbidden_name,
):
    service = FakeSupportService()
    functions = CustomerSupportToolGateway(
        service=service
    ).bind(context=CONTEXT)

    result = functions["get_order"](
        order_no="ORD-DELAY-001",
        **{forbidden_name: "org-b"},
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_ARGUMENTS"
    assert service.calls == []


def test_bound_get_logistics_returns_not_created_as_success():
    service = FakeSupportService()
    functions = CustomerSupportToolGateway(
        service=service
    ).bind(context=CONTEXT)

    result = functions["get_logistics"](
        order_no="ORD-DELAY-001",
    )

    assert result["ok"] is True
    assert result["data"]["availability"] == "not_created"
    assert result["data"]["shipment"] is None


def test_gateway_maps_order_not_found():
    service = FakeSupportService(
        fail_with=SupportOrderNotFoundError(
            "database-specific detail"
        )
    )
    functions = CustomerSupportToolGateway(
        service=service
    ).bind(context=CONTEXT)

    result = functions["get_order"](
        order_no="ORD-MISSING",
    )

    assert result == {
        "ok": False,
        "error": {
            "code": "ORDER_NOT_FOUND",
            "message": "order not found",
        },
    }


def test_gateway_hides_unexpected_exception():
    service = FakeSupportService(
        fail_with=RuntimeError(
            "database password and stack detail"
        )
    )
    functions = CustomerSupportToolGateway(
        service=service
    ).bind(context=CONTEXT)

    with mock.patch(
        "app.tools.support_gateway.logger.exception"
    ) as mock_log:
        result = functions["get_order"](
            order_no="ORD-DELAY-001",
        )

    assert result == {
        "ok": False,
        "error": {
            "code": "INTERNAL_ERROR",
            "message": "tool execution failed",
        },
    }
    assert "database password" not in str(result)
    mock_log.assert_called_once()
    assert mock_log.call_args[0][0] == "support tool failed"


def test_gateway_rejects_unknown_tool_without_service_call():
    service = FakeSupportService()
    gateway = CustomerSupportToolGateway(service=service)

    result = gateway.execute(
        context=CONTEXT,
        tool_name="unknown_tool",
        arguments={},
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "UNKNOWN_TOOL"
    assert service.calls == []
