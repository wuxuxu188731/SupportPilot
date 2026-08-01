import logging
from typing import Any, Protocol

from pydantic import ValidationError

from app.application.customer_support_service import (
    CustomerSupportService,
    InvalidSupportRequestError,
    SupportDataIntegrityError,
    SupportOrderNotFoundError,
    OrderCustomerMismatchError,
    SupportCustomerNotFoundError,
    SupportOperationRejectedError,
    SupportTicketNotFoundError,
    TicketNumberGenerationError,
)
from app.application.organization_service import TenantContext
from app.tools.support_arguments import SUPPORT_ARGUMENT_MODELS
from app.tools.support_definitions import (
    get_support_tool_definitions,
)
from app.tools.support_results import (
    JsonObject,
    serialize_logistics_details,
    serialize_order_details,
    tool_failure,
    tool_success,
    serialize_ticket,
    serialize_ticket_note,
)



logger = logging.getLogger(__name__)


class ToolFunction(Protocol):
    def __call__(self, **arguments: Any) -> JsonObject:
        raise NotImplementedError


class CustomerSupportToolGateway:
    def __init__(
        self,
        *,
        service: CustomerSupportService,
    ):
        self._service = service

    def _dispatch(
        self,
        *,
        context: TenantContext,
        tool_name: str,
        parsed: Any,
    ) -> JsonObject:
        if tool_name == "get_order":
            details = self._service.get_order(
                context=context,
                order_no=parsed.order_no,
            )
            return tool_success(
                serialize_order_details(details)
            )

        if tool_name == "get_logistics":
            details = self._service.get_logistics(
                context=context,
                order_no=parsed.order_no,
            )
            return tool_success(
                serialize_logistics_details(details)
            )

        if tool_name == "create_ticket":
            ticket = self._service.create_ticket(
                context=context,
                summary=parsed.summary,
                category=parsed.category,
                priority=parsed.priority,
                customer_no=parsed.customer_no,
                order_no=parsed.order_no,
            )
            return tool_success(serialize_ticket(ticket))

        note = self._service.add_ticket_note(
            context=context,
            ticket_no=parsed.ticket_no,
            content=parsed.content,
        )
        return tool_success(
            serialize_ticket_note(
                ticket_no=parsed.ticket_no,
                note=note,
            )
        )

    def _bind_one(
        self,
        *,
        context: TenantContext,
        tool_name: str,
    ) -> ToolFunction:
        def invoke(**arguments: Any) -> JsonObject:
            return self.execute(
                context=context,
                tool_name=tool_name,
                arguments=arguments,
            )

        return invoke

    def bind(
        self,
        *,
        context: TenantContext,
    ) -> dict[str, ToolFunction]:
        return {
            tool_name: self._bind_one(
                context=context,
                tool_name=tool_name,
            )
            for tool_name in SUPPORT_ARGUMENT_MODELS
        }

    @property
    def definitions(self) -> list[dict]:
        return get_support_tool_definitions()

    @staticmethod
    def _validation_failure(
        exc: ValidationError,
    ) -> JsonObject:
        details = [
            {
                "type": item["type"],
                "loc": list(item["loc"]),
                "msg": item["msg"],
            }
            for item in exc.errors(
                include_input=False,
                include_url=False,
            )
        ]
        return tool_failure(
            code="INVALID_ARGUMENTS",
            message="tool arguments are invalid",
            details=details,
        )

    def execute(
        self,
        *,
        context: TenantContext,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> JsonObject:
        argument_model = SUPPORT_ARGUMENT_MODELS.get(
            tool_name
        )
        if argument_model is None:
            return tool_failure(
                code="UNKNOWN_TOOL",
                message="INVALID_TOOL_NAME"
            )

        try:
            parsed = argument_model.model_validate(arguments)
        except ValidationError as exc:
            return self._validation_failure(exc)

        try:
            return self._dispatch(
                context=context,
                tool_name=tool_name,
                parsed=parsed
            )
        except InvalidSupportRequestError:
            return tool_failure(
                code="INVALID_ARGUMENTS",
                message="tool arguments are invalid",
            )
        except SupportOrderNotFoundError:
            return tool_failure(
                code="ORDER_NOT_FOUND",
                message="order not found",
            )
        except SupportDataIntegrityError:
            return tool_failure(
                code="DATA_INTEGRITY_ERROR",
                message="support data is inconsistent",
            )
        except SupportCustomerNotFoundError:
                    return tool_failure(
                        code="CUSTOMER_NOT_FOUND",
                        message="customer not found",
                    )
        except OrderCustomerMismatchError:
            return tool_failure(
                code="ORDER_CUSTOMER_MISMATCH",
                message="order does not belong to customer",
            )
        except SupportTicketNotFoundError:
            return tool_failure(
                code="TICKET_NOT_FOUND",
                message="ticket not found",
            )
        except SupportOperationRejectedError:
            return tool_failure(
                code="OPERATION_REJECTED",
                message="support operation was rejected",
            )
        except TicketNumberGenerationError:
            return tool_failure(
                code="TICKET_NUMBER_GENERATION_FAILED",
                message="ticket number generation failed",
            )
        except Exception:
            logger.exception(
                "support tool failed",
                extra={"tool_name": tool_name},
            )
            return tool_failure(
                code="INTERNAL_ERROR",
                message="tool execution failed",
            )
        
