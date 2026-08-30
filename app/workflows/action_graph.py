"""退款/补偿审批的确定性工作流图（StateGraph、节点、条件边与状态类型）。

设计 11.1 / 11.2 / 11.3：State 只保存可序列化的稳定标识与流程输出；
每个节点通过依赖注入的 ``ActionStore`` 按 Run ID 重新读取权威业务数据，
不信任 State 中的金额、角色或批准标记。``await_decision`` 节点在调用
``interrupt()`` 前不创建业务记录、不追加审计；进入等待状态与审计由
前一个独立节点幂等完成。

节点拓扑：

.. code-block:: text

    START
      -> load_run
      -> mark_awaiting_approval -> await_decision [interrupt]
      -> load_persisted_decision
           | rejected -> finalize_rejected -> END
           | approved -> execute_action
                | success    -> finalize_succeeded -> END
                | failure    -> finalize_failed    -> END

恢复语义（设计 11.5）：同一 ``thread_id`` 存在中断 checkpoint 时由
``Command(resume=...)`` 继续；无 checkpoint（首次启动失败重试、可重试
失败恢复、checkpoint 丢失）时从 START 重新运行，``load_run`` 依据
业务库中的 Run 状态与持久化决定路由到正确分支。
"""

from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from app.actions.base import (
    ActionError,
    ActionRunStatus,
    ActionStore,
    ApprovalDecisionType,
    AuditActorType,
    ExecutionDataIntegrityError,
    ExecutionNotApprovedError,
    ExecutionRetryableFailureError,
    RunStateConflictError,
)
from app.actions.executor import ActionExecutor

# 流程输出取值（设计 11.1 outcome 字段）。
OUTCOME_SUCCEEDED = "succeeded"  # 动作执行成功
OUTCOME_REJECTED = "rejected"  # 审批拒绝，流程按人工决定终止
OUTCOME_FAILED = "failed"  # 校验或执行失败

# 允许的决定列表，用于审批中断载荷展示（设计 11.3）。
ALLOWED_DECISIONS = ["approved", "approved_with_changes", "rejected"]

# 图节点名称。
NODE_LOAD_RUN = "load_run"  # 重新读取 Run 并路由
NODE_MARK_AWAITING = "mark_awaiting_approval"  # 幂等推进到等待审批
NODE_AWAIT_DECISION = "await_decision"  # 审批中断点
NODE_LOAD_DECISION = "load_persisted_decision"  # 读取并校验持久化决定
NODE_EXECUTE = "execute_action"  # 幂等执行动作
NODE_FINALIZE_REJECTED = "finalize_rejected"  # 拒绝终态
NODE_FINALIZE_SUCCEEDED = "finalize_succeeded"  # 成功终态
NODE_FINALIZE_FAILED = "finalize_failed"  # 失败终态


class ActionWorkflowState(TypedDict, total=False):
    """工作流图状态：只保存稳定标识与流程输出，不作为授权来源。"""

    run_id: str  # 动作 Run 标识
    proposal_id: str  # 提案标识（冗余展示用，权威数据按 Run 重新读取）
    approval_id: str  # 审批标识（冗余展示用，权威数据按 Run 重新读取）
    organization_id: str  # 企业标识，用于一致性校验，不作为授权来源
    decision_id: str | None  # 恢复值携带的决定标识（仅作一致性参考）
    execution_id: str | None  # 执行记录标识（执行成功后写入）
    outcome: str | None  # 流程输出：succeeded / rejected / failed
    error_code: str | None  # 失败时的稳定错误码
    error_retryable: bool  # 失败是否允许显式恢复重试


def build_action_graph(
    *,
    store: ActionStore,
    executor: ActionExecutor,
    checkpointer=None,
) -> CompiledStateGraph:
    """装配确定性动作工作流图。

    ``checkpointer`` 传入已初始化的 LangGraph checkpointer（正式运行
    使用 SQLite 持久化器，测试可传入内存或临时 SQLite saver）。
    """
    # —— 节点实现（闭包注入 Store 与执行器） ——

    def load_run(state: ActionWorkflowState) -> dict:
        """重新读取 Run 并回填展示用标识，路由由条件边完成。

        设计 11.1：不信任 State 中可能存在的过期值，全部按 Run ID
        重新读取权威业务数据。
        """
        organization_id = state["organization_id"]
        run_id = state["run_id"]
        run = store.get_run(
            organization_id=organization_id,
            run_id=run_id,
        )
        proposal_id = None
        approval_id = None
        if run.proposal_id is not None:
            proposal = store.get_proposal(
                organization_id=organization_id,
                proposal_id=run.proposal_id,
            )
            proposal_id = proposal.proposal_id
            approval = store.get_approval_by_proposal(
                organization_id=organization_id,
                proposal_id=proposal.proposal_id,
            )
            approval_id = approval.approval_id
        return {
            "run_id": run_id,
            "proposal_id": proposal_id,
            "approval_id": approval_id,
            "organization_id": organization_id,
        }

    def route_from_load_run(state: ActionWorkflowState) -> str:
        """按 Run 状态路由到等待、执行或拒绝分支。

        queued/running 一律先经过等待节点（状态机必经之路）；已持久化
        的决定由 ``load_persisted_decision`` 从业务库读取，不依赖恢复值
        或本路由判断。
        """
        organization_id = state["organization_id"]
        run_id = state["run_id"]
        run = store.get_run(
            organization_id=organization_id,
            run_id=run_id,
        )
        if run.status is ActionRunStatus.QUEUED:
            # 首次启动：必须经过 running 再进入等待审批。
            return NODE_MARK_AWAITING
        if run.status in (
            ActionRunStatus.RUNNING,
            ActionRunStatus.AWAITING_APPROVAL,
        ):
            proposal = store.get_proposal_by_run(
                organization_id=organization_id,
                run_id=run_id,
            )
            approval = store.get_approval_by_proposal(
                organization_id=organization_id,
                proposal_id=proposal.proposal_id,
            )
            decision = store.get_decision(
                organization_id=organization_id,
                approval_id=approval.approval_id,
            )
            if decision is not None:
                # 审批后或 checkpoint 丢失的恢复必须直接进入决定分支，
                # 不能把已批准的 running Run 退回 awaiting_approval。
                return NODE_LOAD_DECISION
            if run.status is ActionRunStatus.RUNNING:
                # 首次启动在两次状态转换之间中断，补齐等待状态。
                return NODE_MARK_AWAITING
            return NODE_AWAIT_DECISION
        if run.status is ActionRunStatus.FAILED and run.last_error_retryable:
            # 可重试失败恢复：跳过等待，直接读取决定并重试执行。
            return NODE_LOAD_DECISION
        if run.status is ActionRunStatus.SUCCEEDED:
            # 成功终态的重放：执行器返回稳定结果，不产生副作用。
            return NODE_LOAD_DECISION
        raise RunStateConflictError(
            f"Run 状态 {run.status.value} 不允许启动或恢复"
        )

    def mark_awaiting_approval(state: ActionWorkflowState) -> dict:
        """幂等推进 Run 到等待审批并写入进入等待审计。

        设计 11.3：进入等待状态与审计由本节点幂等完成；重放时按当前
        状态跳过已完成的转换，避免重复写等待审计。
        """
        organization_id = state["organization_id"]
        run_id = state["run_id"]
        run = store.get_run(
            organization_id=organization_id,
            run_id=run_id,
        )
        if run.status is ActionRunStatus.QUEUED:
            store.transition_run(
                organization_id=organization_id,
                run_id=run_id,
                expected_statuses=(ActionRunStatus.QUEUED,),
                new_status=ActionRunStatus.RUNNING,
                error_code=None,
                error_retryable=False,
                actor_type=AuditActorType.SYSTEM,
                actor_user_id=None,
                event_type="run_started",
                details_json="{}",
            )
            store.transition_run(
                organization_id=organization_id,
                run_id=run_id,
                expected_statuses=(ActionRunStatus.RUNNING,),
                new_status=ActionRunStatus.AWAITING_APPROVAL,
                error_code=None,
                error_retryable=False,
                actor_type=AuditActorType.SYSTEM,
                actor_user_id=None,
                event_type="run_awaiting_approval",
                details_json="{}",
            )
        elif run.status is ActionRunStatus.RUNNING:
            # 上次进程在两次转换之间中断：补齐进入等待转换与审计。
            store.transition_run(
                organization_id=organization_id,
                run_id=run_id,
                expected_statuses=(ActionRunStatus.RUNNING,),
                new_status=ActionRunStatus.AWAITING_APPROVAL,
                error_code=None,
                error_retryable=False,
                actor_type=AuditActorType.SYSTEM,
                actor_user_id=None,
                event_type="run_awaiting_approval",
                details_json="{}",
            )
        elif run.status is not ActionRunStatus.AWAITING_APPROVAL:
            raise RunStateConflictError(
                f"Run 状态 {run.status.value} 不能进入等待审批"
            )
        return {}

    def await_decision(state: ActionWorkflowState) -> dict:
        """审批中断点：只读取展示数据并中断，不产生任何业务副作用。

        设计 11.3：中断载荷仅包含展示所需的 JSON；恢复值只携带
        ``decision_id``，金额、角色与批准标记一律以业务库为准。
        """
        organization_id = state["organization_id"]
        run_id = state["run_id"]
        proposal = store.get_proposal_by_run(
            organization_id=organization_id,
            run_id=run_id,
        )
        approval = store.get_approval_by_proposal(
            organization_id=organization_id,
            proposal_id=proposal.proposal_id,
        )
        version = store.get_version(
            organization_id=organization_id,
            version_id=approval.requested_version_id,
        )
        order_number = store.get_order_number(
            organization_id=organization_id,
            order_id=proposal.order_id,
        )
        if order_number is None:
            raise ExecutionDataIntegrityError(
                "提案订单不存在或不属于当前企业"
            )
        payload = {
            "run_id": run_id,
            "approval_id": approval.approval_id,
            "action_type": proposal.action_type.value,
            "order_no_masked": _mask_order_no(order_number),
            "amount_cents": version.amount_cents,
            "currency": version.currency,
            "reason_code": version.reason_code,
            "allowed_decisions": ALLOWED_DECISIONS,
        }
        resume_value = interrupt(payload)
        decision_id = None
        if isinstance(resume_value, dict):
            decision_id = resume_value.get("decision_id")
        elif isinstance(resume_value, str):
            decision_id = resume_value
        return {"decision_id": decision_id}

    def load_persisted_decision(state: ActionWorkflowState) -> dict:
        """从业务库读取并校验持久化决定（不信任恢复值）。

        设计 10.2 / 11.3：决定提交后恢复失败时，恢复必须读取该持久化
        决定继续；没有持久化决定的执行分支一律拒绝。
        """
        organization_id = state["organization_id"]
        run_id = state["run_id"]
        proposal = store.get_proposal_by_run(
            organization_id=organization_id,
            run_id=run_id,
        )
        approval = store.get_approval_by_proposal(
            organization_id=organization_id,
            proposal_id=proposal.proposal_id,
        )
        decision = store.get_decision(
            organization_id=organization_id,
            approval_id=approval.approval_id,
        )
        if decision is None:
            raise ExecutionNotApprovedError(
                "没有持久化的批准决定，拒绝恢复执行分支"
            )
        # 恢复值中的 decision_id 仅作一致性参考，权威决定以业务库为准；
        # 伪造或过期的恢复值被忽略，不会改变执行分支。
        resume_decision_id = state.get("decision_id")
        if (
            resume_decision_id is not None
            and resume_decision_id != decision.decision_id
        ):
            resume_decision_id = None
        return {
            "decision_id": decision.decision_id,
            "execution_id": None,
        }

    def route_from_decision(state: ActionWorkflowState) -> str:
        """按业务库中的决定类型路由到拒绝终态或执行分支。"""
        organization_id = state["organization_id"]
        run_id = state["run_id"]
        proposal = store.get_proposal_by_run(
            organization_id=organization_id,
            run_id=run_id,
        )
        approval = store.get_approval_by_proposal(
            organization_id=organization_id,
            proposal_id=proposal.proposal_id,
        )
        decision = store.get_decision(
            organization_id=organization_id,
            approval_id=approval.approval_id,
        )
        if decision is None:
            raise ExecutionNotApprovedError(
                "没有持久化的批准决定，拒绝恢复执行分支"
            )
        if decision.decision is ApprovalDecisionType.REJECTED:
            return NODE_FINALIZE_REJECTED
        return NODE_EXECUTE

    def execute_action(state: ActionWorkflowState) -> dict:
        """推进 Run 到 running 后调用幂等执行器。

        设计 11.2 / Task 4：执行器负责授权链验证、幂等认领（含中断
        重新认领）、模拟执行与结果/失败落库；本节点只映射结果到图状态。
        """
        organization_id = state["organization_id"]
        run_id = state["run_id"]
        _advance_run_to_running(
            store,
            organization_id=organization_id,
            run_id=run_id,
        )
        try:
            outcome = executor.execute(
                organization_id=organization_id,
                run_id=run_id,
            )
        except ActionError as exc:
            # 执行器通常会把失败写入执行记录与 Run；批准链校验等异常可能
            # 发生在执行认领之前，此时由工作流补齐 Proposal、Run 与审计。
            failed_run = store.get_run(
                organization_id=organization_id,
                run_id=run_id,
            )
            retryable = isinstance(exc, ExecutionRetryableFailureError)
            if failed_run.status is ActionRunStatus.RUNNING:
                failed_run = store.record_workflow_failure(
                    organization_id=organization_id,
                    run_id=run_id,
                    error_code=exc.code,
                    retryable=retryable,
                )
            elif failed_run.status is not ActionRunStatus.FAILED:
                raise
            return {
                "execution_id": None,
                "outcome": OUTCOME_FAILED,
                "error_code": failed_run.last_error_code or exc.code,
                "error_retryable": failed_run.last_error_retryable,
            }
        return {
            "execution_id": outcome.execution.execution_id,
            "outcome": OUTCOME_SUCCEEDED,
            "error_code": None,
            "error_retryable": False,
        }

    def route_from_execution(state: ActionWorkflowState) -> str:
        """按执行结果路由到成功或失败终态。"""
        if state.get("outcome") == OUTCOME_SUCCEEDED:
            return NODE_FINALIZE_SUCCEEDED
        return NODE_FINALIZE_FAILED

    def finalize_rejected(state: ActionWorkflowState) -> dict:
        """把 Run 推进到 cancelled 终态（审批拒绝，非系统错误）。

        Store 状态机不允许 awaiting_approval 直达 cancelled，需要先
        经过 running；重放时按当前状态跳过已完成的转换。
        """
        organization_id = state["organization_id"]
        run_id = state["run_id"]
        run = store.get_run(
            organization_id=organization_id,
            run_id=run_id,
        )
        if run.status is ActionRunStatus.AWAITING_APPROVAL:
            store.transition_run(
                organization_id=organization_id,
                run_id=run_id,
                expected_statuses=(ActionRunStatus.AWAITING_APPROVAL,),
                new_status=ActionRunStatus.RUNNING,
                error_code=None,
                error_retryable=False,
                actor_type=AuditActorType.SYSTEM,
                actor_user_id=None,
                event_type="run_resumed",
                details_json="{}",
            )
            store.transition_run(
                organization_id=organization_id,
                run_id=run_id,
                expected_statuses=(ActionRunStatus.RUNNING,),
                new_status=ActionRunStatus.CANCELLED,
                error_code=None,
                error_retryable=False,
                actor_type=AuditActorType.SYSTEM,
                actor_user_id=None,
                event_type="run_cancelled",
                details_json="{}",
            )
        elif run.status is ActionRunStatus.RUNNING:
            store.transition_run(
                organization_id=organization_id,
                run_id=run_id,
                expected_statuses=(ActionRunStatus.RUNNING,),
                new_status=ActionRunStatus.CANCELLED,
                error_code=None,
                error_retryable=False,
                actor_type=AuditActorType.SYSTEM,
                actor_user_id=None,
                event_type="run_cancelled",
                details_json="{}",
            )
        elif run.status is not ActionRunStatus.CANCELLED:
            raise RunStateConflictError(
                f"Run 状态 {run.status.value} 不能进入拒绝终态"
            )
        return {
            "outcome": OUTCOME_REJECTED,
            "error_code": None,
            "error_retryable": False,
        }

    def finalize_succeeded(state: ActionWorkflowState) -> dict:
        """成功终态：状态已由执行器在同一事务内落库，这里只收口图状态。"""
        return {
            "outcome": OUTCOME_SUCCEEDED,
            "error_code": None,
            "error_retryable": False,
        }

    def finalize_failed(state: ActionWorkflowState) -> dict:
        """失败终态：状态已由执行器落库，这里只收口图状态。"""
        return {
            "outcome": OUTCOME_FAILED,
        }

    # —— 图装配 ——

    graph = StateGraph(ActionWorkflowState)
    graph.add_node(NODE_LOAD_RUN, load_run)
    graph.add_node(NODE_MARK_AWAITING, mark_awaiting_approval)
    graph.add_node(NODE_AWAIT_DECISION, await_decision)
    graph.add_node(NODE_LOAD_DECISION, load_persisted_decision)
    graph.add_node(NODE_EXECUTE, execute_action)
    graph.add_node(NODE_FINALIZE_REJECTED, finalize_rejected)
    graph.add_node(NODE_FINALIZE_SUCCEEDED, finalize_succeeded)
    graph.add_node(NODE_FINALIZE_FAILED, finalize_failed)

    graph.add_edge(START, NODE_LOAD_RUN)
    graph.add_conditional_edges(
        NODE_LOAD_RUN,
        route_from_load_run,
        {
            NODE_MARK_AWAITING: NODE_MARK_AWAITING,
            NODE_AWAIT_DECISION: NODE_AWAIT_DECISION,
            NODE_LOAD_DECISION: NODE_LOAD_DECISION,
        },
    )
    graph.add_edge(NODE_MARK_AWAITING, NODE_AWAIT_DECISION)
    graph.add_edge(NODE_AWAIT_DECISION, NODE_LOAD_DECISION)
    graph.add_conditional_edges(
        NODE_LOAD_DECISION,
        route_from_decision,
        {
            NODE_FINALIZE_REJECTED: NODE_FINALIZE_REJECTED,
            NODE_EXECUTE: NODE_EXECUTE,
        },
    )
    graph.add_conditional_edges(
        NODE_EXECUTE,
        route_from_execution,
        {
            NODE_FINALIZE_SUCCEEDED: NODE_FINALIZE_SUCCEEDED,
            NODE_FINALIZE_FAILED: NODE_FINALIZE_FAILED,
        },
    )
    graph.add_edge(NODE_FINALIZE_REJECTED, END)
    graph.add_edge(NODE_FINALIZE_SUCCEEDED, END)
    graph.add_edge(NODE_FINALIZE_FAILED, END)

    return graph.compile(checkpointer=checkpointer)


def _advance_run_to_running(
    store: ActionStore,
    *,
    organization_id: str,
    run_id: str,
) -> None:
    """幂等推进 Run 到 running（审批后自动恢复 / 可重试失败显式恢复）。

    设计 11.5 / Task 2 报告：审批恢复后先进入 running，再认领和执行；
    重放时 Run 已处于 running 或 succeeded（成功重放路径）则跳过。
    """
    run = store.get_run(
        organization_id=organization_id,
        run_id=run_id,
    )
    if run.status is ActionRunStatus.AWAITING_APPROVAL:
        store.transition_run(
            organization_id=organization_id,
            run_id=run_id,
            expected_statuses=(ActionRunStatus.AWAITING_APPROVAL,),
            new_status=ActionRunStatus.RUNNING,
            error_code=None,
            error_retryable=False,
            actor_type=AuditActorType.SYSTEM,
            actor_user_id=None,
            event_type="run_resumed",
            details_json="{}",
        )
    elif run.status is ActionRunStatus.FAILED and run.last_error_retryable:
        store.transition_run(
            organization_id=organization_id,
            run_id=run_id,
            expected_statuses=(ActionRunStatus.FAILED,),
            new_status=ActionRunStatus.RUNNING,
            error_code=None,
            error_retryable=False,
            actor_type=AuditActorType.SYSTEM,
            actor_user_id=None,
            event_type="run_resumed",
            details_json="{}",
        )
    elif run.status in (
        ActionRunStatus.RUNNING,
        ActionRunStatus.SUCCEEDED,
    ):
        # 已处于执行中或已成功（成功重放路径），无需转换。
        return
    else:
        raise RunStateConflictError(
            f"Run 状态 {run.status.value} 不能进入执行"
        )


def _mask_order_no(order_no: str) -> str:
    """掩码订单号：仅保留前 3 位与后 2 位，中间以星号代替。

    审批中断载荷只展示掩码订单号，避免跨租户泄露完整业务数据。
    """
    if len(order_no) <= 6:
        return order_no[:1] + "****"
    return order_no[:3] + "****" + order_no[-2:]
