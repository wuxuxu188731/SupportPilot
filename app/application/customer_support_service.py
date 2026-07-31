from collections.abc import Callable
from dataclasses import dataclass
from uuid import uuid4
from enum import Enum


from app.application.organization_service import TenantContext
from app.customers.base import (
  Customer,
  CustomerNotFoundError,
  CustomerStore,
)
from app.orders.base import (
  Order,
  OrderNotFoundError,
  OrderStore,
)
from app.shipments.base import (
    Shipment,
    ShipmentNotFoundError,
    ShipmentStore,
)
from app.tickets.base import (
    InvalidTicketCommentReferenceError,
    InvalidTicketReferenceError,
    Ticket,
    TicketAlreadyExistsError,
    TicketCategory,
    TicketComment,
    TicketCommentVisibility,
    TicketNotFoundError,
    TicketPriority,
    TicketStatus,
    TicketStore,
)

MIN_TICKET_SUMMARY_LENGTH = 5
MAX_TICKET_SUMMARY_LENGTH = 500
MAX_TICKET_NUMBER_ATTEMPTS = 3 
MAX_TICKET_NOTE_LENGTH = 2000

TicketNumberFactory = Callable[[],str]

@dataclass(frozen=True)
class OrderDetails():
  order : Order
  customer : Customer

class LogisticsAvailability(str, Enum):
  NOT_CREATED = "not_created"
  AVAILABLE = "available"

@dataclass(frozen=True)
class LogisticsDetails:
  order: Order
  customer: Customer
  availability: LogisticsAvailability
  shipment: Shipment | None

class InvalidSupportRequestError(ValueError):
  pass

class SupportOrderNotFoundError(LookupError):
  pass

class SupportDataIntegrityError(RuntimeError):
  pass

class SupportCustomerNotFoundError(LookupError):
  pass

class OrderCustomerMismatchError(ValueError):
  pass

class TicketNumberGenerationError(RuntimeError):
  pass

class SupportOperationRejectedError(ValueError):
  pass

class SupportTicketNotFoundError(LookupError):
    pass

def generate_ticket_no() -> str:
  return f"TKT-{uuid4().hex.upper()}"

class CustomerSupportService:
  def __init__(
    self, 
    customer_store: CustomerStore,
    order_store: OrderStore,
    shipment_store: ShipmentStore,
    ticket_store: TicketStore,
    ticket_no_factory: TicketNumberFactory | None = None
  ):
    self._customer_store = customer_store
    self._order_store = order_store
    self._shipment_store = shipment_store
    self._ticket_store = ticket_store
    self._ticket_no_factory = ticket_no_factory or generate_ticket_no

  @staticmethod
  def _required_text(*, value: str, field_name: str)->str:
    normalized = value.strip()
    if not normalized:
      raise InvalidSupportRequestError(f"{field_name} must not be blank")
    return normalized

  @staticmethod
  def _validate_summary(*, summary: str)->str:
    normalized = summary.strip()
    if not (MIN_TICKET_SUMMARY_LENGTH <= len(normalized) <= MAX_TICKET_SUMMARY_LENGTH):
      raise InvalidSupportRequestError("summary must contain 5-500 characters")
    return normalized

  def get_order(
    self,
    *,
    context: TenantContext,
    order_no: str
  ) -> OrderDetails:
    normalize_order_no = self._required_text(value=order_no, field_name="order_no")
    try:
      order = self._order_store.get_by_no(
        organization_id=context.organization_id,
        order_no=normalize_order_no
      )
    except OrderNotFoundError as exc:
      raise SupportOrderNotFoundError("order not found") from exc

    try:
      customer = self._customer_store.get_by_id(
        organization_id=context.organization_id,
        customer_id=order.customer_id
      )
    except CustomerNotFoundError as exc:
      raise SupportDataIntegrityError ("order references a missing customer")

    return OrderDetails(
      order = order,
      customer = customer
    )

  def get_logistics(
    self,
    *,
    context : TenantContext,
    order_no : str
  ) -> LogisticsDetails:
    #不能相信传进来的参数一定是合法的，先过一遍校验，看看订单是否存在，订单和客户之间的关系是否正确
    order_details = self.get_order(
      context=context,
      order_no=order_no
    )
    #ShipmentNotFoundError出现这个error一般有两种情况：1.订单不存在，所以没有物流信息  2.订单存在，但是还没有发货，
    #所以没有物流信息.由于先过了一遍校验，所以能进入下面try块里面的代码一定是订单存在的情况
    try:
      shipment = self._shipment_store.get_by_order_no(
        organization_id=context.organization_id,
        order_no=order_details.order.order_no
      )
    except ShipmentNotFoundError as exc:
      return LogisticsDetails(
        order=order_details.order,
        customer=order_details.customer,
        availability=LogisticsAvailability.NOT_CREATED, #表示物流还没有创建，也就是未发货
        shipment=None,
      )
    return LogisticsDetails(
      order=order_details.order,
      customer=order_details.customer,
      availability=LogisticsAvailability.AVAILABLE,
      shipment=shipment
    )

  def get_customer_by_no(
    self,
    *,
    context: TenantContext,
    customer_no: str
  )->Customer:
    normalized_customer_no=self._required_text(
      value=customer_no,
      field_name="customer_no"
    )
    try:
      return self._customer_store.get_by_no(
        organization_id=context.organization_id,
        customer_no=normalized_customer_no
      )
    except CustomerNotFoundError as exc:
      raise SupportCustomerNotFoundError("customer not found") from exc

  def _new_ticket_no(self)-> str:
    try:
      ticket_no = self._ticket_no_factory()
    except Exception as exc:
      raise TicketNumberGenerationError("ticket number generate failed") from exc
    if not isinstance(ticket_no,str) or not ticket_no.strip():
      raise TicketNumberGenerationError("ticket number generation return an invalid number")
    return ticket_no.strip()

  def create_ticket(
    self,
    *,
    context: TenantContext,
    summary: str,
    category: TicketCategory,
    priority: TicketPriority,
    customer_no: str | None = None,
    order_no: str | None = None,
  ) -> Ticket:
    normalized_summary = self._validate_summary(summary=summary)
    if not isinstance(category, TicketCategory):
      raise InvalidSupportRequestError("category must be TicketCategory")
    if not isinstance(priority, TicketPriority):
      raise InvalidSupportRequestError("priority must be TicketPriority")
    if customer_no is None and order_no is None:
      raise InvalidSupportRequestError(
          "customer_no or order_no is required"
      )

    order_details: OrderDetails | None = None
    if order_no is not None:
      order_details = self.get_order(
        context=context,
        order_no=order_no,
      )

    explicit_customer: Customer | None = None
    if customer_no is not None:
      explicit_customer = self.get_customer_by_no(
        context=context,
        customer_no=customer_no,
      )

    if (
      order_details is not None
      and explicit_customer is not None
      and order_details.customer.customer_id
      != explicit_customer.customer_id
    ):
      raise OrderCustomerMismatchError(
        "order does not belong to customer"
      )

    if explicit_customer is not None:
      customer = explicit_customer
    elif order_details is not None:
      customer = order_details.customer
    else:
      raise InvalidSupportRequestError(
        "unable to determine customer"
      )

    order_id = (
      order_details.order.order_id
      if order_details is not None
      else None
    )
    for _ in range(MAX_TICKET_NUMBER_ATTEMPTS):
      try:
        return self._ticket_store.create_ticket(
          organization_id=context.organization_id,
          ticket_no=self._new_ticket_no(),
          customer_id=customer.customer_id,
          order_id=order_id,
          created_by_user_id=context.user_id,
          assigned_to_user_id=context.user_id,
          summary=normalized_summary,
          category=category,
          priority=priority,
          status=TicketStatus.OPEN,
        )
      except TicketAlreadyExistsError:
        continue
      except InvalidTicketReferenceError as exc:
        raise SupportOperationRejectedError(
          "ticket creation was rejected"
        ) from exc

    raise TicketNumberGenerationError(
      "could not allocate a unique ticket number"
    )

  def add_ticket_note(
    self,
    *,
    context : TenantContext,
    ticket_no : str,
    content : str
  )->TicketComment:
    normalized_ticket_no = self._required_text(
      value=ticket_no,
      field_name="ticket_no",
    )
    normalized_content = self._required_text(
      value=content,
      field_name="content",
    )

    if len(normalized_content) > MAX_TICKET_NOTE_LENGTH:
      raise InvalidSupportRequestError(
        "content must contain at most 2000 characters"
      )

    try:
      ticket = self._ticket_store.get_by_no(
        organization_id=context.organization_id,
        ticket_no=normalized_ticket_no,
      )
    except TicketNotFoundError as exc:
      raise SupportTicketNotFoundError("ticket not found") from exc

    try:
      return self._ticket_store.add_comment(
        organization_id=context.organization_id,
        ticket_id=ticket.ticket_id,
        author_user_id=context.user_id,
        visibility=TicketCommentVisibility.INTERNAL,
        content=normalized_content
      )
    except InvalidTicketCommentReferenceError as exc:
      raise SupportOperationRejectedError("ticket note was rejected") from exc