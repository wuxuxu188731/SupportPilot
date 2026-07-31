import logging
from typing import Any, Protocol

from pydantic import ValidationError

from app.application.customer_support_service import (
    CustomerSupportService,
    InvalidSupportRequestError,
    SupportDataIntegrityError,
    SupportOrderNotFoundError,
)
from app.application.organization_service import TenantContext
from app.tools.support_arguments import SUPPORT_ARGUMENT_MODELS
from app.tools.support_results import (
    JsonObject,
    serialize_logistics_details,
    serialize_order_details,
    tool_failure,
    tool_success,
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
            "get_order": self._bind_one(
                context=context,
                tool_name="get_order",
            ),
            "get_logistics": self._bind_one(
                context=context,
                tool_name="get_logistics",
            ),
        }

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
        if argument_model is None or tool_name not in {
            "get_order",
            "get_logistics",
        }:
            return tool_failure(
                code="UNKNOWN_TOOL",
                message="tool is not available",
            )

        try:
            parsed = argument_model.model_validate(arguments)
        except ValidationError as exc:
            return self._validation_failure(exc)

        try:
            if tool_name == "get_order":
                details = self._service.get_order(
                    context=context,
                    order_no=parsed.order_no,
                )
                return tool_success(
                    serialize_order_details(details)
                )

            details = self._service.get_logistics(
                context=context,
                order_no=parsed.order_no,
            )
            return tool_success(
                serialize_logistics_details(details)
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
        except Exception:
            logger.exception(
                "support tool failed",
                extra={"tool_name": tool_name},
            )
            return tool_failure(
                code="INTERNAL_ERROR",
                message="tool execution failed",
            )
