from typing import get_type_hints

import pytest
from pydantic import ValidationError

from app.application.customer_support_service import(
  CustomerSupportService,
)
from app.tickets.base import TicketCategory,TicketPriority
from app.tools.support_arguments import(
  AddTicketNoteArguments,
  CreateTicketArguments,
  GetLogisticsArguments,
  GetOrderArguments,
  SUPPORT_ARGUMENT_MODELS,
)

def test_support_arguments_models_match_public_tool_names():
  assert set(SUPPORT_ARGUMENT_MODELS)=={
    "get_order",
    "get_logistics",
    "create_ticket",
    "add_ticket_note",
  }

def test_query_arguments_strip_whitespace():
  arguments = GetOrderArguments(order_no="  ORDER-DELAY-001 ")
  assert arguments.order_no == "ORDER-DELAY-001"

@pytest.mark.parametrize(
  "forbidden_name",
  [
    "organization_id",
    "user_id",
    "actor_user_id",
    "role",
    "context"
  ]
)
def test_query_arguments_reject_security_context_fields(forbidden_name):
  payload = {
    "order_no":" ORDER-DELAY-001",
    forbidden_name:"attacker-controlled"
  }
  with pytest.raises(ValidationError):
    GetOrderArguments.model_validate(payload)

def test_create_ticket_arguments_convert_enums():
  arguments = CreateTicketArguments(
    order_no="ORD-DELAY-001",
    summary="订单超过承诺时间仍未发货",
    category="logistics",
    priority="high",
  )

  assert arguments.category is TicketCategory.LOGISTICS
  assert arguments.priority is TicketPriority.HIGH
  assert arguments.customer_no is None

def test_create_ticket_requires_customer_or_order():
  with pytest.raises(ValueError,match="customer_no or order_no is required",):
    arguments = CreateTicketArguments(
      summary="订单超过承诺时间发货",
      category="logistics",
      priority="high"
    )

@pytest.mark.parametrize(
  ("model","payload"),
  [
    (GetOrderArguments, {"order_no":"   "}),
    (GetLogisticsArguments, {"order_no","   "}),
    (
      CreateTicketArguments,
      {
        "summary":"abc",
        "customer_no":"CUS-001",
        "category":"other",
        "priority":"high"
      }
    ),
    (
      AddTicketNoteArguments,
      {
        "ticket_no":"TKT-001",
        "content":"x"*2001
      }
    )
  ]
)
def test_argument_models_enforce_boundaries(model, payload):
  with pytest.raises(ValidationError):
    model.model_validate(payload)

def test_add_ticket_note_annotation_is_string():
  hints = get_type_hints(
    CustomerSupportService.add_ticket_note
  )
  assert hints["ticket_no"] is str