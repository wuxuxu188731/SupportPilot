from typing import Any 

from app.application.customer_support_service import(
  LogisticsDetails,
  OrderDetails,
)
from app.customers.base import Customer
from app.orders.base import Order
from app.shipments.base import Shipment
from app.tickets.base import Ticket,TicketComment

JsonObject = dict[str,Any]

def tool_success(data: JsonObject) -> JsonObject:
  return {
    "ok": True,
    "data": data,
  }

def tool_failure(
    *, 
    code: str, 
    message: str, 
    details: list[JsonObject]|None = None
  ) -> JsonObject:
    error: JsonObject = {
      "code": code,
      "message": message,
    }
    if details is not None:
      error["details"] = details
    return {
      "ok": False,
      "error": error,
    }

def serialize_customer(customer: Customer) -> JsonObject:
  return {
    "customer_no": customer.customer_no,
    "name": customer.name,
    "email": customer.email,
    "phone": customer.phone,
  }


def serialize_order(order: Order) -> JsonObject:
  return {
    "order_no": order.order_no,
    "status": order.status.value,
    "item_summary": order.item_summary,
    "total_amount_cents": order.total_amount_cents,
    "currency": order.currency,
    "placed_at": order.placed_at,
    "promised_ship_at": order.promised_ship_at,
  }


def serialize_shipment(
    shipment: Shipment,
) -> JsonObject:
  return {
    "shipment_no": shipment.shipment_no,
    "carrier": shipment.carrier,
    "tracking_no": shipment.tracking_no,
    "status": shipment.status.value,
    "last_event": shipment.last_event,
    "shipped_at": shipment.shipped_at,
    "estimated_delivery_at": (
        shipment.estimated_delivery_at
    ),
    "delivered_at": shipment.delivered_at,
  }


def serialize_order_details(
    details: OrderDetails,
) -> JsonObject:
  return {
    "order": serialize_order(details.order),
    "customer": serialize_customer(details.customer),
  }


def serialize_logistics_details(
    details: LogisticsDetails,
) -> JsonObject:
  return {
    "order": serialize_order(details.order),
    "customer": serialize_customer(details.customer),
    "availability": details.availability.value,
    "shipment": (
        serialize_shipment(details.shipment)
        if details.shipment is not None
        else None
    ),
  }

def serialize_ticket(ticket: Ticket) -> JsonObject:
    return {
        "ticket_no": ticket.ticket_no,
        "summary": ticket.summary,
        "category": ticket.category.value,
        "priority": ticket.priority.value,
        "status": ticket.status.value,
        "created_at": ticket.created_at,
        "updated_at": ticket.updated_at,
    }


def serialize_ticket_note(
    *,
    ticket_no: str,
    note: TicketComment,
) -> JsonObject:
  return {
    "ticket_no": ticket_no,
    "seq": note.seq,
    "visibility": note.visibility.value,
    "content": note.content,
    "created_at": note.created_at,
  }