from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class TicketCategory(str, Enum):
    LOGISTICS = "logistics"
    REFUND = "refund"
    DAMAGED_ITEM = "damaged_item"
    PRODUCT_QUESTION = "product_question"
    OTHER = "other"


class TicketPriority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


class TicketStatus(str, Enum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    PENDING_CUSTOMER = "pending_customer"
    RESOLVED = "resolved"
    CLOSED = "closed"


class TicketCommentVisibility(str, Enum):
    INTERNAL = "internal"
    PUBLIC = "public"


@dataclass(frozen=True)
class TicketComment:
    comment_id: str
    organization_id: str
    ticket_id: str
    seq: int
    author_user_id: str
    visibility: TicketCommentVisibility
    content: str
    created_at: str


@dataclass(frozen=True)
class Ticket:
    ticket_id: str
    organization_id: str
    ticket_no: str
    customer_id: str
    order_id: str | None
    created_by_user_id: str
    assigned_to_user_id: str | None
    summary: str
    category: TicketCategory
    priority: TicketPriority
    status: TicketStatus
    created_at: str
    updated_at: str


class TicketNotFoundError(LookupError):
    pass


class TicketAlreadyExistsError(ValueError):
    pass


class InvalidTicketReferenceError(ValueError):
    pass


class InvalidTicketCommentReferenceError(ValueError):
    pass


class TicketStore(Protocol):
    def create_ticket(
        self,
        *,
        organization_id: str,
        ticket_no: str,
        customer_id: str,
        order_id: str | None,
        created_by_user_id: str,
        assigned_to_user_id: str | None,
        summary: str,
        category: TicketCategory,
        priority: TicketPriority,
        status: TicketStatus,
    ) -> Ticket:
        raise NotImplementedError

    def get_by_id(
        self,
        *,
        organization_id: str,
        ticket_id: str,
    ) -> Ticket:
        raise NotImplementedError

    def get_by_no(
        self,
        *,
        organization_id: str,
        ticket_no: str,
    ) -> Ticket:
        raise NotImplementedError

    def add_comment(
        self,
        *,
        organization_id: str,
        ticket_id: str,
        author_user_id: str,
        visibility: TicketCommentVisibility,
        content: str,
    ) -> TicketComment:
        raise NotImplementedError

    def list_comments(
        self,
        *,
        organization_id: str,
        ticket_id: str,
    ) -> list[TicketComment]:
        raise NotImplementedError
