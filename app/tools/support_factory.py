from pathlib import Path

from app.application.customer_support_factory import (
    create_customer_support_service,
)
from app.application.customer_support_service import (
    TicketNumberFactory,
)
from app.tools.support_gateway import (
    CustomerSupportToolGateway,
)


def create_customer_support_tool_gateway(
    database_path: str | Path,
    *,
    ticket_no_factory: TicketNumberFactory | None = None,
) -> CustomerSupportToolGateway:
    service = create_customer_support_service(
        database_path,
        ticket_no_factory=ticket_no_factory,
    )
    return CustomerSupportToolGateway(service=service)
