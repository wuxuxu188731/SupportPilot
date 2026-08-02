from typing import Any, Protocol

from app.agent.runner import (
    AgentToolFunction,
    DEFAULT_MAX_TOOL_ROUNDS,
    run_one_turn,
)
from app.application.organization_service import TenantContext
from app.core.config import MODEL_NAME
from app.schemas.chat import LLMResponse
from app.tools.support_gateway import CustomerSupportToolGateway


class RunTurn(Protocol):
    def __call__(
        self,
        *,
        messages: list[dict],
        client: Any,
        tool_definitions: list[dict],
        tool_functions: dict[str, AgentToolFunction],
        on_event: Any,
        model_name: str,
        max_tool_rounds: int,
    ) -> LLMResponse:
        raise NotImplementedError


class CustomerSupportAgentRunner:
    def __init__(
        self,
        *,
        client: Any,
        gateway: CustomerSupportToolGateway,
        model_name: str = MODEL_NAME,
        max_tool_rounds: int = DEFAULT_MAX_TOOL_ROUNDS,
        on_event: Any = None,
        run_turn: RunTurn = run_one_turn,
    ):
        self._client = client
        self._gateway = gateway
        self._model_name = model_name
        self._max_tool_rounds = max_tool_rounds
        self._on_event = on_event
        self._run_turn = run_turn

    def __call__(
        self,
        *,
        messages: list[dict],
        context: TenantContext,
    ) -> LLMResponse:
        return self._run_turn(
            messages=messages,
            client=self._client,
            tool_definitions=self._gateway.definitions,
            tool_functions=self._gateway.bind(context=context),
            on_event=self._on_event,
            model_name=self._model_name,
            max_tool_rounds=self._max_tool_rounds,
        )
