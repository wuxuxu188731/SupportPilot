"""幂等模拟执行器：框架无关执行器接口与退款/补偿模拟实现。

本模块实现设计文档 Task 4：只接受已经持久化并批准的提案版本，执行前
重新加载并验证完整授权链，按版本化幂等键认领执行，调用模拟适配器产生
业务结果，并在同一事务内完成上限重验与结果落库（上限重验与结果落库由
``SQLiteActionStore`` 在事务内完成，执行器负责编排与错误归类）。

职责边界：

- 执行器不判断调用者权限（RBAC 由审批/恢复 API 负责），只验证业务授权链：
  Run -> Proposal -> Approval -> ApprovalDecision -> 批准版本 必须同属一个
  企业且引用完整，决定必须是批准或修改后批准；
- 执行器不连接真实支付或优惠券渠道，模拟适配器只生成稳定的业务结果标识，
  业务记录行由 Store 写入；
- 可重试基础设施失败（含模拟注入的临时故障）保留原幂等键，Run 进入可恢复
  失败，显式恢复后沿用同一幂等键重新认领并递增尝试次数；
- 本模块不依赖 LangGraph，Task 5 的图节点只需调用 ``execute()`` 并映射
  异常到图状态。
"""

import json
from dataclasses import dataclass
from typing import Callable, Protocol
from uuid import uuid4

from app.actions.base import (
    ActionCompensationCapExceededError,
    ActionCompensationDuplicateError,
    ActionError,
    ActionProposal,
    ActionProposalVersion,
    ActionRefundBalanceExceededError,
    ActionRun,
    ActionRunStatus,
    ActionStore,
    ActionType,
    Approval,
    ApprovalDecision,
    ApprovalDecisionType,
    ApprovalStatus,
    ExecutionDataIntegrityError,
    ExecutionNotApprovedError,
    ExecutionRetryableFailureError,
    RefundScope,
    RunStateConflictError,
    ToolExecution,
    ToolExecutionStatus,
)

# 幂等键版本前缀（设计 9.6），后续变更幂等键语义时提升版本号。
IDEMPOTENCY_KEY_PREFIX = "action-execution:v1:"

# 模拟执行成功结果 JSON 中的字段名，与 Store 写入的 result_json 保持一致。
RESULT_FIELD_BUSINESS_RECORD_ID = "business_record_id"  # 业务结果标识字段
RESULT_FIELD_ORDER_MARKED_REFUNDED = "order_marked_refunded"  # 订单 refunded 标记字段


def build_idempotency_key(
    *,
    organization_id: str,
    proposal_id: str,
    proposal_version_id: str,
    action_type: ActionType,
) -> str:
    """生成版本化幂等键。

    格式：``action-execution:v1:{organization_id}:{proposal_id}:
    {proposal_version_id}:{action_type}``。幂等键由服务端生成，调用者不能
    提供；数据库唯一约束是最终防线。
    """
    return (
        f"{IDEMPOTENCY_KEY_PREFIX}{organization_id}:{proposal_id}:"
        f"{proposal_version_id}:{action_type.value}"
    )


@dataclass(frozen=True)
class SimulatedRefundResult:
    """退款模拟适配器的执行结果。"""

    business_record_id: str  # 模拟退款记录标识（真实记录行由 Store 写入）
    mark_order_refunded: bool  # 本次执行是否应把订单主状态置为 refunded


@dataclass(frozen=True)
class SimulatedCompensationResult:
    """补偿模拟适配器的执行结果。"""

    business_record_id: str  # 模拟补偿记录标识（真实记录行由 Store 写入）


@dataclass(frozen=True)
class ExecutionOutcome:
    """一次幂等执行调用的完整结果。"""

    execution: ToolExecution  # 最终执行记录（succeeded 或失败后的最新状态）
    business_record_id: str | None  # 业务结果标识，尚未成功执行为 None
    order_marked_refunded: bool  # 订单是否被标记为 refunded
    replayed: bool  # 是否重放了既有成功结果（True 表示本次未产生副作用）


class RetryableFailureInjector:
    """可重试失败注入器，用于测试与演练「模拟执行器注入的临时故障」。

    设计 16.5：注入的临时故障记录 ``error_retryable=true``，保留同一
    Run、版本和幂等键，允许显式恢复后重试。
    """

    def __init__(self, fail_count: int = 0):
        self._remaining = fail_count  # 剩余注入次数，0 表示不再注入

    def __call__(self) -> None:
        """每次调用检查是否注入一次可重试失败。"""
        if self._remaining > 0:
            self._remaining -= 1
            raise ExecutionRetryableFailureError(
                "模拟执行器注入的临时故障"
            )


class SimulatedRefundAdapter:
    """退款模拟适配器：模拟执行一次退款，不连接真实支付渠道。

    只生成业务结果标识，并根据执行时可退余额计算订单 refunded 标记；
    金额、余额和引用链校验由 Store 在写结果事务内完成，本适配器不重复
    判断业务规则。
    """

    def __init__(self, *, failure_injector: Callable[[], None] | None = None):
        self._failure_injector = failure_injector  # 可选临时故障注入器

    def execute(
        self,
        *,
        order_id: str,
        amount_cents: int,
        currency: str,
        reason_code: str,
        refund_scope: RefundScope,
        remaining_balance_cents: int,
    ) -> SimulatedRefundResult:
        """模拟执行退款并返回业务结果标识与订单 refunded 标记。

        ``order_id`` 等参数当前仅用于语义表达与日志，模拟实现不产生外部
        渠道流水；``remaining_balance_cents`` 由执行器在执行前读取。
        """
        if self._failure_injector is not None:
            self._failure_injector()
        # 设计 8.2：订单是否标记 refunded 以剩余可退余额是否归零判断，
        # 不只看 refund_scope（部分退款恰好等于剩余余额时同样全额退完）。
        mark_order_refunded = amount_cents == remaining_balance_cents
        return SimulatedRefundResult(
            business_record_id=f"refund-sim-{uuid4()}",
            mark_order_refunded=mark_order_refunded,
        )


class SimulatedCompensationAdapter:
    """补偿模拟适配器：模拟发放一张固定 30 天有效期的优惠券。

    只生成业务结果标识；重复原因与 50% 累计上限校验由 Store 在写结果
    事务内完成，本适配器不重复判断业务规则。
    """

    def __init__(self, *, failure_injector: Callable[[], None] | None = None):
        self._failure_injector = failure_injector  # 可选临时故障注入器

    def execute(
        self,
        *,
        order_id: str,
        amount_cents: int,
        currency: str,
        reason_code: str,
        coupon_valid_days: int,
    ) -> SimulatedCompensationResult:
        """模拟发放优惠券并返回业务结果标识。

        ``order_id`` 等参数当前仅用于语义表达与日志，模拟实现不产生外部
        渠道流水；``coupon_valid_days`` 本阶段固定为 30。
        """
        if self._failure_injector is not None:
            self._failure_injector()
        return SimulatedCompensationResult(
            business_record_id=f"compensation-sim-{uuid4()}",
        )


class ActionExecutor(Protocol):
    """框架无关的动作执行器接口。

    Task 5 的 LangGraph 图节点通过本接口执行动作；实现方负责授权链验证、
    幂等认领、模拟执行与结果/失败落库，调用方只需要按 Run 标识调用。
    """

    def execute(self, *, organization_id: str, run_id: str) -> ExecutionOutcome:
        """执行 Run 对应的已批准动作；重复调用返回同一稳定业务结果。"""
        raise NotImplementedError


class IdempotentActionExecutor:
    """幂等模拟执行器：授权链验证 -> 幂等认领 -> 模拟执行 -> 结果落库。

    执行流程（设计 10.3）：

    1. 重新读取 Run、Proposal、Approval、ApprovalDecision 与被批准版本，
       验证同企业、引用链完整且决定为批准或修改后批准；
    2. 按版本化幂等键认领或读取既有执行记录；已成功执行原样返回稳定结果，
       不重复写业务记录（节点重放安全）；
    3. 只有本次认领 ``acquired=true`` 时才推进执行，调用模拟适配器；
    4. 由 Store 在写结果事务内重验退款余额/补偿上限并写入业务结果。

    失败归类（设计 16）：

    - 缺少批准决定或决定为拒绝：``EXECUTION_NOT_APPROVED``，不触碰任何记录；
    - 退款余额不足、补偿重复、超过补偿上限：由 Store 抛出稳定业务错误，
      执行器把执行记录标记为不可重试失败后重新抛出；
    - 模拟注入的临时故障：``EXECUTION_RETRYABLE_FAILURE``，执行器把执行
      记录标记为可重试失败后重新抛出，保留原幂等键；
    - 引用链损坏、批准版本不存在等数据一致性问题：``EXECUTION_DATA_INTEGRITY_ERROR``，
      执行器把执行记录标记为不可重试失败后重新抛出。
    """

    def __init__(
        self,
        *,
        store: ActionStore,
        refund_adapter: SimulatedRefundAdapter | None = None,
        compensation_adapter: SimulatedCompensationAdapter | None = None,
    ):
        self._store = store
        # 未显式注入时使用默认模拟适配器（无故障注入）。
        self._refund_adapter = refund_adapter or SimulatedRefundAdapter()
        self._compensation_adapter = (
            compensation_adapter or SimulatedCompensationAdapter()
        )

    def execute(self, *, organization_id: str, run_id: str) -> ExecutionOutcome:
        """执行 Run 对应的已批准动作，重复调用返回同一稳定业务结果。

        调用前要求 Run 已由工作流推进到 ``running``（设计 11.5 / Task 2
        报告：审批恢复后先进入 running，再认领和执行）；Run 已成功的
        重复调用走只读重放路径，不要求 running。
        """
        run, proposal, approval, decision, version = (
            self._load_approved_chain(
                organization_id=organization_id,
                run_id=run_id,
            )
        )

        # 已成功的执行直接重放稳定结果（设计 15.2），不检查 Run 状态。
        existing = self._store.get_execution_by_version(
            organization_id=organization_id,
            proposal_version_id=version.version_id,
        )
        if existing is not None and existing.status is ToolExecutionStatus.SUCCEEDED:
            return self._replay_success(existing)

        if run.status is not ActionRunStatus.RUNNING:
            raise RunStateConflictError(
                "只有 running 状态的 Run 才能执行动作"
            )

        claim = self._store.claim_execution(
            organization_id=organization_id,
            proposal_id=proposal.proposal_id,
            proposal_version_id=version.version_id,
            action_type=proposal.action_type,
            idempotency_key=build_idempotency_key(
                organization_id=organization_id,
                proposal_id=proposal.proposal_id,
                proposal_version_id=version.version_id,
                action_type=proposal.action_type,
            ),
        )
        if not claim.acquired:
            # 重复认领：已成功由上方重放路径处理；claimed/running 表示
            # 执行权不在本次调用方且执行尚未完成，禁止执行副作用。
            if claim.status is ToolExecutionStatus.SUCCEEDED:
                return self._replay_success(claim.execution)
            raise RunStateConflictError(
                "执行权不在本次调用方，且执行尚未完成"
            )

        execution = self._store.mark_execution_running(
            organization_id=organization_id,
            execution_id=claim.execution_id,
        )
        return self._run_and_record(
            organization_id=organization_id,
            proposal=proposal,
            version=version,
            execution=execution,
        )

    # —— 内部实现 ——

    def _load_approved_chain(
        self,
        *,
        organization_id: str,
        run_id: str,
    ) -> tuple[
        ActionRun,
        ActionProposal,
        Approval,
        ApprovalDecision,
        ActionProposalVersion,
    ]:
        """重新加载并验证完整批准授权链（设计 10.3 步骤 1-3）。

        所有读取都显式携带 ``organization_id``；任何一环缺失、错配或决定
        不是批准，都按稳定错误码拒绝，不产生执行记录。
        """
        run = self._store.get_run(
            organization_id=organization_id,
            run_id=run_id,
        )
        proposal = self._store.get_proposal_by_run(
            organization_id=organization_id,
            run_id=run_id,
        )
        approval = self._store.get_approval_by_proposal(
            organization_id=organization_id,
            proposal_id=proposal.proposal_id,
        )
        if approval.proposal_id != proposal.proposal_id:
            raise ExecutionDataIntegrityError("审批请求不属于该提案")
        decision = self._store.get_decision(
            organization_id=organization_id,
            approval_id=approval.approval_id,
        )
        if decision is None or decision.decision not in (
            ApprovalDecisionType.APPROVED,
            ApprovalDecisionType.APPROVED_WITH_CHANGES,
        ):
            # 没有持久化的批准决定，或决定为拒绝：任何调用路径都拒绝执行。
            raise ExecutionNotApprovedError(
                "缺少持久化的批准决定，拒绝执行动作"
            )
        if approval.status not in (
            ApprovalStatus.APPROVED,
            ApprovalStatus.APPROVED_WITH_CHANGES,
        ):
            raise ExecutionDataIntegrityError(
                "审批状态与批准决定不一致"
            )
        version = self._store.get_version(
            organization_id=organization_id,
            version_id=decision.decided_version_id,
        )
        if version.proposal_id != proposal.proposal_id:
            raise ExecutionDataIntegrityError(
                "批准版本不属于该提案"
            )
        if proposal.current_version_id != decision.decided_version_id:
            raise ExecutionDataIntegrityError(
                "当前生效版本与批准版本不一致"
            )
        return run, proposal, approval, decision, version

    @staticmethod
    def _replay_success(execution: ToolExecution) -> ExecutionOutcome:
        """按既有成功执行记录返回稳定业务结果（重放，不产生副作用）。"""
        try:
            result = json.loads(execution.result_json or "{}")
        except json.JSONDecodeError as exc:
            raise ExecutionDataIntegrityError(
                "执行结果 JSON 损坏"
            ) from exc
        business_record_id = result.get(RESULT_FIELD_BUSINESS_RECORD_ID)
        if not isinstance(business_record_id, str):
            raise ExecutionDataIntegrityError(
                "已成功执行缺少业务结果标识"
            )
        return ExecutionOutcome(
            execution=execution,
            business_record_id=business_record_id,
            order_marked_refunded=bool(
                result.get(RESULT_FIELD_ORDER_MARKED_REFUNDED, False)
            ),
            replayed=True,
        )

    def _run_and_record(
        self,
        *,
        organization_id: str,
        proposal: ActionProposal,
        version: ActionProposalVersion,
        execution: ToolExecution,
    ) -> ExecutionOutcome:
        """调用模拟适配器并落库成功结果；失败时按类别标记执行记录后重抛。"""
        action_type = proposal.action_type
        if action_type is ActionType.REFUND:
            return self._run_refund(
                organization_id=organization_id,
                proposal=proposal,
                version=version,
                execution=execution,
            )
        return self._run_compensation(
            organization_id=organization_id,
            proposal=proposal,
            version=version,
            execution=execution,
        )

    def _run_refund(
        self,
        *,
        organization_id: str,
        proposal: ActionProposal,
        version: ActionProposalVersion,
        execution: ToolExecution,
    ) -> ExecutionOutcome:
        """执行退款：读取最新可退余额，调用退款适配器并落库结果。"""
        try:
            remaining_balance = self._store.get_refundable_balance(
                organization_id=organization_id,
                order_id=proposal.order_id,
            )
            if remaining_balance is None:
                raise ExecutionDataIntegrityError(
                    "提案订单不存在或不属于当前企业"
                )
            parameters = self._parse_version_parameters(
                version=version,
                action_type=ActionType.REFUND,
            )
            simulated = self._refund_adapter.execute(
                order_id=proposal.order_id,
                amount_cents=version.amount_cents,
                currency=version.currency,
                reason_code=version.reason_code,
                refund_scope=RefundScope(parameters["refund_scope"]),
                remaining_balance_cents=remaining_balance,
            )
        except ExecutionRetryableFailureError as exc:
            self._record_failure(
                organization_id=organization_id,
                execution_id=execution.execution_id,
                error_code=exc.code,
                retryable=True,
            )
            raise
        except ExecutionDataIntegrityError as exc:
            self._record_failure(
                organization_id=organization_id,
                execution_id=execution.execution_id,
                error_code=exc.code,
                retryable=False,
            )
            raise
        return self._record_success(
            organization_id=organization_id,
            proposal=proposal,
            version=version,
            execution=execution,
            business_record_id=simulated.business_record_id,
            coupon_valid_days=None,
            mark_order_refunded=simulated.mark_order_refunded,
        )

    def _run_compensation(
        self,
        *,
        organization_id: str,
        proposal: ActionProposal,
        version: ActionProposalVersion,
        execution: ToolExecution,
    ) -> ExecutionOutcome:
        """执行补偿：调用补偿适配器并落库结果。"""
        try:
            parameters = self._parse_version_parameters(
                version=version,
                action_type=ActionType.COMPENSATION,
            )
            simulated = self._compensation_adapter.execute(
                order_id=proposal.order_id,
                amount_cents=version.amount_cents,
                currency=version.currency,
                reason_code=version.reason_code,
                coupon_valid_days=parameters["coupon_valid_days"],
            )
        except ExecutionRetryableFailureError as exc:
            self._record_failure(
                organization_id=organization_id,
                execution_id=execution.execution_id,
                error_code=exc.code,
                retryable=True,
            )
            raise
        except ExecutionDataIntegrityError as exc:
            self._record_failure(
                organization_id=organization_id,
                execution_id=execution.execution_id,
                error_code=exc.code,
                retryable=False,
            )
            raise
        return self._record_success(
            organization_id=organization_id,
            proposal=proposal,
            version=version,
            execution=execution,
            business_record_id=simulated.business_record_id,
            coupon_valid_days=parameters["coupon_valid_days"],
            mark_order_refunded=False,
        )

    def _record_success(
        self,
        *,
        organization_id: str,
        proposal: ActionProposal,
        version: ActionProposalVersion,
        execution: ToolExecution,
        business_record_id: str,
        coupon_valid_days: int | None,
        mark_order_refunded: bool,
    ) -> ExecutionOutcome:
        """由 Store 在同一事务内重验上限、写入业务结果并更新状态。

        Store 抛出的业务拒绝（余额不足/补偿重复/超上限）与数据完整性
        错误按不可重试失败记录后重新抛出，保留稳定错误码。
        """
        try:
            success = self._store.record_execution_success(
                organization_id=organization_id,
                execution_id=execution.execution_id,
                proposal_id=proposal.proposal_id,
                proposal_version_id=version.version_id,
                order_id=proposal.order_id,
                action_type=proposal.action_type,
                amount_cents=version.amount_cents,
                currency=version.currency,
                reason_code=version.reason_code,
                business_record_id=business_record_id,
                coupon_valid_days=coupon_valid_days,
                mark_order_refunded=mark_order_refunded,
            )
        except (
            ActionRefundBalanceExceededError,
            ActionCompensationDuplicateError,
            ActionCompensationCapExceededError,
            ExecutionDataIntegrityError,
        ) as exc:
            # 设计 16.2：执行时业务拒绝与不可恢复失败 -> 不可重试终态。
            self._record_failure(
                organization_id=organization_id,
                execution_id=execution.execution_id,
                error_code=exc.code,
                retryable=False,
            )
            raise
        return ExecutionOutcome(
            execution=success.execution,
            business_record_id=success.business_record_id,
            order_marked_refunded=success.order_marked_refunded,
            replayed=False,
        )

    def _record_failure(
        self,
        *,
        organization_id: str,
        execution_id: str,
        error_code: str,
        retryable: bool,
    ) -> None:
        """把本次拥有的执行记录标记为失败并更新关联 Run 状态。

        失败记录本身冲突时（例如并发下另一路已推进执行状态），以原始
        异常为准，不再覆盖其他调用方的状态。
        """
        try:
            self._store.record_execution_failure(
                organization_id=organization_id,
                execution_id=execution_id,
                error_code=error_code,
                retryable=retryable,
            )
        except ActionError:
            pass

    @staticmethod
    def _parse_version_parameters(
        *,
        version: ActionProposalVersion,
        action_type: ActionType,
    ) -> dict:
        """解析批准版本的类型专属参数 JSON，损坏时按数据完整性错误处理。"""
        try:
            parameters = json.loads(version.parameters_json)
        except json.JSONDecodeError as exc:
            raise ExecutionDataIntegrityError(
                "提案版本参数 JSON 损坏"
            ) from exc
        if not isinstance(parameters, dict):
            raise ExecutionDataIntegrityError(
                "提案版本参数必须是 JSON 对象"
            )
        if action_type is ActionType.REFUND:
            if set(parameters) != {"refund_scope"}:
                raise ExecutionDataIntegrityError(
                    "退款版本参数只能包含 refund_scope"
                )
            try:
                RefundScope(parameters["refund_scope"])
            except (TypeError, ValueError) as exc:
                raise ExecutionDataIntegrityError(
                    "退款版本 refund_scope 非法"
                ) from exc
        else:
            if set(parameters) != {"coupon_valid_days"}:
                raise ExecutionDataIntegrityError(
                    "补偿版本参数只能包含 coupon_valid_days"
                )
            if (
                type(parameters["coupon_valid_days"]) is not int
                or parameters["coupon_valid_days"] != 30
            ):
                raise ExecutionDataIntegrityError(
                    "补偿版本优惠券有效期必须为 30 天"
                )
        return parameters
