from pydantic import(
  BaseModel,
  ConfigDict,
  Field,
  model_validator
)

from app.tickets.base import TicketCategory,TicketPriority

BUSINESS_NUMBER_MAX_LENGTH = 100

class SupportToolArguments(BaseModel):
  model_config = ConfigDict(
    extra="forbid",
    str_strip_whitespace=True
  )

class GetOrderArguments(SupportToolArguments):
  order_no: str = Field(
    min_length=1,
    max_length=BUSINESS_NUMBER_MAX_LENGTH
  )

class GetLogisticsArguments(SupportToolArguments):
  order_no: str = Field(
    min_length=1,
    max_length=BUSINESS_NUMBER_MAX_LENGTH
  )

class CreateTicketArguments(SupportToolArguments):
  summary: str = Field(
    min_length=5,
    max_length=500,
  )
  category: TicketCategory
  priority: TicketPriority
  customer_no: str = Field(
    default=None,
    min_length=1,
    max_length=BUSINESS_NUMBER_MAX_LENGTH,
  )
  order_no: str = Field(
    default=None,
    min_length=1,
    max_length=BUSINESS_NUMBER_MAX_LENGTH,
  )

  @model_validator(mode="after")
  def require_customer_or_order(self) -> "CreateTicketArguments":
    if self.customer_no is None and self.order_no is None:
      raise ValueError("customer_no or order_no is required")
    return self

class AddTicketNoteArguments(SupportToolArguments):
  ticket_no: str = Field(
    min_length=1,
    max_length=BUSINESS_NUMBER_MAX_LENGTH,
  )
  content: str = Field(
    min_length=1,
    max_length=2000,
  )

SUPPORT_ARGUMENT_MODELS = {
  "get_order":GetOrderArguments,
  "get_logistics":GetLogisticsArguments,
  "create_ticket":CreateTicketArguments,
  "add_ticket_note":AddTicketNoteArguments,
}