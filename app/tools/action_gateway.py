"""Action Tool Gateway：提案与只读状态工具（设计 12.2 / 12.3 / 12.4 / 12.6）。

本 Gateway 位于 ``CompositeToolGateway`` 之内、``ActionWorkflowService``
之前：

- ``bind`` 严格要求 ``AgentInvocationContext``：提案工具必须获得服务端
  验证过的 ``conversation_id`` 与 ``turn_id``，不接受纯 ``TenantContext``；
- 模型可见的参数只有业务字段，租户、会话、回合与 ``thread_id`` 均不可见、
  不可构造；
- 只暴露 ``propose_refund`` / ``propose_compensation`` / ``get_action_status``，
  不暴露审批、恢复与执行工具（设计 12.6）；
- 领域错误统一映射为带稳定错误码的 ``tool_failure``，不泄露 SQL、路径
  或堆栈。
"""

from typing import Any

from pydantic import ValidationError

from app.actions.base import (
    ActionError,
    ActionType,
    RefundScope,
)
from app.actions.service import (
    ActionWorkflowService,
    ProposalCreationOutcome,
    RunStatusView,
)
from app.agent.invocation_context import AgentInvocationContext
from app.orders.base import OrderNotFoundError, OrderStore
from app.tools.action_arguments import ACTION_ARGUMENT_MODELS
from app.tools.action_definitions import get_action_tool_definitions
from app.tools.support_results import JsonObject, tool_failure, tool_success

# 提案工具的固定参数：补偿券有效期本阶段固定为 30 天（设计 8.3）
COMPENSATION_COUPON_VALID_DAYS = 30


class ActionToolGateway:
    """动作工具 Gateway：创建退款/补偿提案与查询 Run 状态。"""

    def __init__(
        self,
        *,
        service: ActionWorkflowService,
        order_store: OrderStore,
    ):
        self._service = service
        self._order_store = order_store

    @property
    def definitions(self) -> list[dict]:
        """返回模型可见的工具定义（不含任何租户/会话/回合字段）。"""
        return get_action_tool_definitions()

    def bind(
        self,
        *,
        context: AgentInvocationContext,
    ) -> dict[str, Any]:
        """把工具函数绑定到请求级调用上下文。

        动作工具必须携带可信租户、会话与回合标识；直接传入
        ``TenantContext`` 属于编程错误，立即失败而不是静默丢失会话信息。
        """
        if not isinstance(context, AgentInvocationContext):
            raise TypeError(
                "动作工具必须绑定 AgentInvocationContext"
            )

        return {
            tool_name: self._bind_one(
                context=context,
                tool_name=tool_name,
            )
            for tool_name in ACTION_ARGUMENT_MODELS
        }

    def _bind_one(
        self,
        *,
        context: AgentInvocationContext,
        tool_name: str,
    ) -> Any:
        """构造单个工具的请求级闭包。"""

        def invoke(**arguments: Any) -> JsonObject:
            return self.execute(
                context=context,
                tool_name=tool_name,
                arguments=arguments,
            )

        return invoke

    @staticmethod
    def _validation_failure(exc: ValidationError) -> JsonObject:
        """把参数校验失败归一化为稳定的工具错误。"""
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
        context: AgentInvocationContext,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> JsonObject:
        """校验并执行单个工具调用，把领域错误映射为稳定错误码。"""
        argument_model = ACTION_ARGUMENT_MODELS.get(tool_name)
        if argument_model is None:
            return tool_failure(
                code="UNKNOWN_TOOL",
                message="INVALID_TOOL_NAME",
            )
        try:
            parsed = argument_model.model_validate(arguments)
        except ValidationError as exc:
            return self._validation_failure(exc)

        try:
            if tool_name == "propose_refund":
                return self._propose_refund(
                    context=context,
                    order_no=parsed.order_no,
                    amount_cents=parsed.amount_cents,
                    currency=parsed.currency,
                    refund_scope=parsed.refund_scope,
                    reason_code=parsed.reason_code,
                    reason_text=parsed.reason_text,
                )
            if tool_name == "propose_compensation":
                return self._propose_compensation(
                    context=context,
                    order_no=parsed.order_no,
                    amount_cents=parsed.amount_cents,
                    currency=parsed.currency,
                    reason_code=parsed.reason_code,
                    reason_text=parsed.reason_text,
                )
            return self._get_action_status(
                context=context,
                run_id=parsed.run_id,
            )
        except ActionError as exc:
            return tool_failure(code=exc.code, message=str(exc))
        except OrderNotFoundError:
            return tool_failure(
                code="ACTION_ORDER_NOT_FOUND",
                message="订单不存在或不属于当前企业",
            )

    # —— 工具实现 ——

    def _resolve_order(
        self,
        *,
        organization_id: str,
        order_no: str,
    ):
        """按企业解析订单号，跨租户订单统一表现为不存在。"""
        return self._order_store.get_by_no(
            organization_id=organization_id,
            order_no=order_no,
        )

    def _serialize_proposal(
        self,
        *,
        outcome: ProposalCreationOutcome,
    ) -> JsonObject:
        """按设计 12.5 组装提案创建结果。"""
        return tool_success(
            {
                "run_id": outcome.creation.run.run_id,
                "proposal_id": outcome.creation.proposal.proposal_id,
                "approval_id": outcome.creation.approval.approval_id,
                "action_type": outcome.creation.proposal.action_type.value,
                "status": outcome.creation.run.status.value,
                "amount_cents": outcome.creation.version.amount_cents,
                "currency": outcome.creation.version.currency,
            }
        )

    def _propose_refund(
        self,
        *,
        context: AgentInvocationContext,
        order_no: str,
        amount_cents: int,
        currency: str,
        refund_scope: RefundScope,
        reason_code: str,
        reason_text: str,
    ) -> JsonObject:
        """创建退款提案：订单号解析后交给应用服务，会话与回合来自上下文。"""
        order = self._resolve_order(
            organization_id=context.tenant.organization_id,
            order_no=order_no,
        )
        outcome = self._service.create_proposal(
            organization_id=context.tenant.organization_id,
            user_id=context.tenant.user_id,
            conversation_id=context.conversation_id,
            turn_id=context.turn_id,
            order_id=order.order_id,
            action_type=ActionType.REFUND,
            amount_cents=amount_cents,
            currency=currency,
            reason_code=reason_code,
            reason_text=reason_text,
            refund_scope=refund_scope,
        )
        return self._serialize_proposal(outcome=outcome)

    def _propose_compensation(
        self,
        *,
        context: AgentInvocationContext,
        order_no: str,
        amount_cents: int,
        currency: str,
        reason_code: str,
        reason_text: str,
    ) -> JsonObject:
        """创建补偿提案：券有效期固定为 30 天，不允许模型指定。"""
        order = self._resolve_order(
            organization_id=context.tenant.organization_id,
            order_no=order_no,
        )
        outcome = self._service.create_proposal(
            organization_id=context.tenant.organization_id,
            user_id=context.tenant.user_id,
            conversation_id=context.conversation_id,
            turn_id=context.turn_id,
            order_id=order.order_id,
            action_type=ActionType.COMPENSATION,
            amount_cents=amount_cents,
            currency=currency,
            reason_code=reason_code,
            reason_text=reason_text,
            coupon_valid_days=COMPENSATION_COUPON_VALID_DAYS,
        )
        return self._serialize_proposal(outcome=outcome)

    def _get_action_status(
        self,
        *,
        context: AgentInvocationContext,
        run_id: str,
    ) -> JsonObject:
        """只读查询 Run 状态；跨租户 Run 与不存在 Run 一样返回 RUN_NOT_FOUND。"""
        view = self._service.get_run_status(
            organization_id=context.tenant.organization_id,
            run_id=run_id,
        )
        return tool_success(self._serialize_run_status(view))

    def _serialize_run_status(self, view: RunStatusView) -> JsonObject:
        """把 Run 状态视图组装为只读工具结果（不含 thread_id 与幂等键）。"""
        order_no = None
        if view.proposal is not None:
            try:
                order = self._order_store.get_by_id(
                    organization_id=view.run.organization_id,
                    order_id=view.proposal.order_id,
                )
                order_no = order.order_no
            except OrderNotFoundError:
                # 引用链损坏由业务侧数据完整性校验负责，这里只影响展示。
                order_no = None
        return {
            "run_id": view.run.run_id,
            "workflow_type": view.run.workflow_type.value,
            "status": view.run.status.value,
            "order_no": order_no,
            "proposal_status": (
                view.proposal.status.value
                if view.proposal is not None
                else None
            ),
            "approval_status": (
                view.approval.status.value
                if view.approval is not None
                else None
            ),
            "decision": (
                view.decision.decision.value
                if view.decision is not None
                else None
            ),
            "execution_status": (
                view.execution.status.value
                if view.execution is not None
                else None
            ),
            "execution_error_code": (
                view.execution.error_code
                if view.execution is not None
                else None
            ),
            "result": view.result,
        }
