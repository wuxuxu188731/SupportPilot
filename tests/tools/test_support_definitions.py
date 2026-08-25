import json

from app.application.organization_service import TenantContext
from app.organizations.base import MembershipRole
from app.tools.support_definitions import (
    get_support_tool_definitions,
)
from app.tools.support_gateway import CustomerSupportToolGateway


class EmptyService:
    pass


def tool_names(definitions):
    return {
        item["function"]["name"]
        for item in definitions
    }


def test_definitions_have_the_four_gateway_tool_names():
    gateway = CustomerSupportToolGateway(
        service=EmptyService()
    )
    definitions = get_support_tool_definitions()
    functions = gateway.bind(
        context=TenantContext(
            user_id="user-a",
            organization_id="org-a",
            role=MembershipRole.AGENT,
        )
    )

    assert tool_names(definitions) == set(functions) == {
        "get_order",
        "get_logistics",
        "create_ticket",
        "add_ticket_note",
    }


def test_definitions_never_expose_trusted_context_fields():
    encoded = json.dumps(
        get_support_tool_definitions(),
        ensure_ascii=False,
    )

    for forbidden_name in (
        "organization_id",
        "user_id",
        "actor_user_id",
        "role",
        "context",
    ):
        assert forbidden_name not in encoded


def test_definitions_forbid_additional_properties():
    definitions = get_support_tool_definitions()

    for definition in definitions:
        parameters = definition["function"]["parameters"]
        assert parameters["type"] == "object"
        assert parameters["additionalProperties"] is False


def test_create_ticket_definition_describes_target_rule():
    definitions = get_support_tool_definitions()
    create_ticket = next(
        item
        for item in definitions
        if item["function"]["name"] == "create_ticket"
    )

    description = create_ticket["function"]["description"]
    assert "customer_no" in description
    assert "order_no" in description
    assert "至少" in description

def test_gateway_definitions_returns_fresh_value():
    gateway = CustomerSupportToolGateway(
        service=EmptyService()
    )

    first = gateway.definitions
    second = gateway.definitions
    first.clear()

    assert len(second) == 4