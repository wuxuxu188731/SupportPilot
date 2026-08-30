from typing import Any, Protocol

from app.agent.invocation_context import (
    AgentInvocationContext,
    tenant_of,
)
from app.agent.runner import (
    AgentToolFunction,
    DEFAULT_MAX_TOOL_ROUNDS,
    run_one_turn,
)
from app.application.organization_service import TenantContext
from app.core.config import MODEL_NAME
from app.schemas.chat import LLMResponse
from app.tools.composite_gateway import ToolGateway


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
        gateway: ToolGateway,
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
        context: AgentInvocationContext | TenantContext,
    ) -> LLMResponse:
        # 设计 12.1：生产链路由 ChatService 传入 AgentInvocationContext；
        # 兼容直接传入 TenantContext 的旧调用方（测试与评估脚本）。
        invocation = (
            context
            if isinstance(context, AgentInvocationContext)
            else AgentInvocationContext(
                tenant=tenant_of(context),
                conversation_id="",
                turn_id="",
            )
        )
        return self._run_turn(
            messages=messages,
            client=self._client,
            tool_definitions=self._gateway.definitions,
            tool_functions=self._gateway.bind(context=invocation),
            on_event=self._on_event,
            model_name=self._model_name,
            max_tool_rounds=self._max_tool_rounds,
        )
