"""退款/补偿审批确定性工作流图测试。

覆盖设计文档 18.4 的验收点：首次运行在审批节点 interrupt、拒绝后进入
cancelled 且没有业务结果、批准后恢复并成功执行、修改后批准执行新版本、
关闭并重新创建 checkpointer/graph 后可以恢复、interrupt 节点重放不重复
写等待审计、执行成功后 checkpoint 丢失再恢复不重复执行、恢复值伪造
批准标记或金额时被忽略。

所有暂停恢复验收都使用真实临时 SQLite checkpointer 并重新创建运行时
实例（设计 17）。
"""

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest
from langgraph.types import Command

from app.actions.base import (
    ActionRunStatus,
    ActionStore,
    ActionType,
    ApprovalDecisionType,
    ExecutionDataIntegrityError,
    NewProposalVersion,
    ProposalStatus,
    RunStateConflictError,
    ToolExecutionStatus,
)
from app.actions.executor import RetryableFailureInjector
from app.actions.factory import build_action_executor
from app.actions.sqlite_store import SQLiteActionStore
from app.customers.sqlite_store import SQLiteCustomerStore
from app.orders.base import Order, OrderStatus
from app.orders.sqlite_store import SQLiteOrderStore
from app.organizations.base import MembershipRole, Organization
from app.organizations.sqlite_store import SQLiteOrganizationStore
from app.users.base import User
from app.users.sqlite_store import SQLiteUserStore
from app.workflows.action_graph import (
    ALLOWED_DECISIONS,
    OUTCOME_FAILED,
    OUTCOME_REJECTED,
    OUTCOME_SUCCEEDED,
    build_action_graph,
)
from app.workflows.checkpointer import create_sqlite_checkpointer


# —— 测试环境搭建 ——


@dataclass
class ActionScope:
    """工作流图测试所需的完整企业范围数据。"""

    store: ActionStore  # 被测的动作工作流存储
    database_path: Path  # 业务数据库文件路径
    org_a: Organization  # 企业 A
    org_b: Organization  # 企业 B，用于跨租户访问测试
    alice: User  # 企业 A 管理员，兼提案人
    bob: User  # 企业 A 的 agent 成员与企业 B 管理员
    order_a: Order  # 企业 A 订单，总额 100 元
    order_b: Order  # 企业 B 订单


def build_scope(tmp_path) -> ActionScope:
    """构造用户、企业、客户与订单，并返回动作存储。"""
    database_path = tmp_path / "app.db"
    users = SQLiteUserStore(database_path)
    organizations = SQLiteOrganizationStore(database_path)
    customers = SQLiteCustomerStore(database_path)
    orders = SQLiteOrderStore(database_path)
    actions = SQLiteActionStore(database_path)

    alice = users.create_user(username="alice", password_hash="hash")
    bob = users.create_user(username="bob", password_hash="hash")
    org_a = organizations.create_with_admin(
        name="企业 A",
        admin_user_id=alice.user_id,
    )
    org_b = organizations.create_with_admin(
        name="企业 B",
        admin_user_id=bob.user_id,
    )
    organizations.add_membership(
        organization_id=org_a.organization_id,
        user_id=bob.user_id,
        role=MembershipRole.AGENT,
    )
    customer_a = customers.create_customer(
        organization_id=org_a.organization_id,
        customer_no="CUST-A-001",
        name="客户 A",
    )
    customer_b = customers.create_customer(
        organization_id=org_b.organization_id,
        customer_no="CUST-B-001",
        name="客户 B",
    )
    order_a = orders.create_order(
        organization_id=org_a.organization_id,
        order_no="ORD-A-001",
        customer_id=customer_a.customer_id,
        status=OrderStatus.PROCESSING,
        item_summary="无线耳机 x1",
        total_amount_cents=10000,
        currency="CNY",
        placed_at="2026-08-28T08:00:00+00:00",
    )
    order_b = orders.create_order(
        organization_id=org_b.organization_id,
        order_no="ORD-B-001",
        customer_id=customer_b.customer_id,
        status=OrderStatus.PROCESSING,
        item_summary="机械键盘 x1",
        total_amount_cents=10000,
        currency="CNY",
        placed_at="2026-08-28T08:00:00+00:00",
    )
    return ActionScope(
        store=actions,
        database_path=database_path,
        org_a=org_a,
        org_b=org_b,
        alice=alice,
        bob=bob,
        order_a=order_a,
        order_b=order_b,
    )


def create_refund_workflow(
    store: ActionStore,
    *,
    organization_id: str,
    user_id: str,
    order_id: str,
    amount_cents: int = 6000,
    reason_code: str = "quality_issue",
    reason_text: str = "商品存在质量问题",
    refund_scope: str = "partial",
    conversation_id: str = "conv-refund",
    turn_id: str = "turn-1",
):
    """创建退款提案工作流。"""
    return store.create_workflow(
        organization_id=organization_id,
        conversation_id=conversation_id,
        turn_id=turn_id,
        created_by_user_id=user_id,
        order_id=order_id,
        action_type=ActionType.REFUND,
        amount_cents=amount_cents,
        currency="CNY",
        reason_code=reason_code,
        reason_text=reason_text,
        parameters_json=json.dumps({"refund_scope": refund_scope}),
    )


def create_compensation_workflow(
    store: ActionStore,
    *,
    organization_id: str,
    user_id: str,
    order_id: str,
    amount_cents: int = 3000,
    reason_code: str = "delayed_shipment",
    reason_text: str = "发货延迟",
    conversation_id: str = "conv-compensation",
    turn_id: str = "turn-1",
):
    """创建补偿提案工作流。"""
    return store.create_workflow(
        organization_id=organization_id,
        conversation_id=conversation_id,
        turn_id=turn_id,
        created_by_user_id=user_id,
        order_id=order_id,
        action_type=ActionType.COMPENSATION,
        amount_cents=amount_cents,
        currency="CNY",
        reason_code=reason_code,
        reason_text=reason_text,
        parameters_json=json.dumps({"coupon_valid_days": 30}),
    )


def approve_decision(
    store: ActionStore,
    *,
    organization_id: str,
    approval_id: str,
    user_id: str,
    decision: ApprovalDecisionType = ApprovalDecisionType.APPROVED,
    comment: str | None = "同意退款",
    new_version: NewProposalVersion | None = None,
):
    """提交审批决定。"""
    return store.decide_approval(
        organization_id=organization_id,
        approval_id=approval_id,
        decided_by_user_id=user_id,
        decision=decision,
        comment=comment,
        new_version=new_version,
    )


def count_rows(database_path: Path, table: str) -> int:
    """统计指定业务表的行数。"""
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    return int(row[0])


def count_audit_events(scope: ActionScope, *, run_id: str, event_type: str) -> int:
    """统计指定 Run 的某类审计事件数量。"""
    logs = scope.store.list_audit_logs(
        organization_id=scope.org_a.organization_id,
        run_id=run_id,
    )
    return sum(1 for log in logs if log.event_type == event_type)


def build_graph(scope: ActionScope, tmp_path, *, fail_count: int = 0):
    """装配带真实 SQLite checkpointer 的工作流图。"""
    executor = build_action_executor(
        scope.store,
        failure_injector=(
            RetryableFailureInjector(fail_count=fail_count)
            if fail_count
            else None
        ),
    )
    checkpointer = create_sqlite_checkpointer(tmp_path / "checkpoints.db")
    graph = build_action_graph(
        store=scope.store,
        executor=executor,
        checkpointer=checkpointer,
    )
    return graph


def start_graph(graph, *, organization_id: str, run_id: str, thread_id: str):
    """从 START 首次运行图，返回带 __interrupt__ 的结果。"""
    return graph.invoke(
        {"run_id": run_id, "organization_id": organization_id},
        {"configurable": {"thread_id": thread_id}},
    )


def resume_graph(
    graph,
    *,
    organization_id: str,
    run_id: str,
    thread_id: str,
    decision_id: str,
):
    """用 Command(resume=...) 恢复中断的图。"""
    return graph.invoke(
        Command(
            resume={"decision_id": decision_id},
            update={
                "run_id": run_id,
                "organization_id": organization_id,
            },
        ),
        {"configurable": {"thread_id": thread_id}},
    )


# —— 首次运行与中断 ——


def test_first_run_interrupts_at_approval(tmp_path):
    # 保护行为：首次运行推进到等待审批后在审批节点中断，
    # 中断载荷只包含展示所需的掩码信息，且不产生任何执行副作用。
    scope = build_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
    )
    graph = build_graph(scope, tmp_path)
    result = start_graph(
        graph,
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
        thread_id=creation.run.thread_id,
    )
    assert "__interrupt__" in result
    payload = result["__interrupt__"][0].value
    assert payload["run_id"] == creation.run.run_id
    assert payload["approval_id"] == creation.approval.approval_id
    assert payload["action_type"] == "refund"
    assert payload["order_no_masked"] == "ORD****01"
    assert payload["amount_cents"] == 6000
    assert payload["currency"] == "CNY"
    assert payload["reason_code"] == "quality_issue"
    assert payload["allowed_decisions"] == ALLOWED_DECISIONS
    run = scope.store.get_run(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    assert run.status is ActionRunStatus.AWAITING_APPROVAL
    assert count_audit_events(
        scope, run_id=creation.run.run_id, event_type="run_awaiting_approval"
    ) == 1
    # 中断本身不产生执行与业务副作用。
    assert count_rows(scope.database_path, "tool_executions") == 0
    assert count_rows(scope.database_path, "refund_records") == 0


def test_interrupt_replay_does_not_duplicate_awaiting_audit(tmp_path):
    # 保护行为：中断节点重放（不带恢复值再次调用）不会重复写等待审计，
    # 也不会推进 Run 状态。
    scope = build_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
    )
    graph = build_graph(scope, tmp_path)
    config = {
        "configurable": {"thread_id": creation.run.thread_id}
    }
    graph.invoke(
        {"run_id": creation.run.run_id, "organization_id": scope.org_a.organization_id},
        config,
    )
    # 不带 Command 的重放只会重新中断（LangGraph 语义）。
    graph.invoke(None, config)
    assert count_audit_events(
        scope, run_id=creation.run.run_id, event_type="run_awaiting_approval"
    ) == 1
    run = scope.store.get_run(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    assert run.status is ActionRunStatus.AWAITING_APPROVAL


# —— 拒绝分支 ——


def test_rejected_decision_cancels_run_without_business_result(tmp_path):
    # 保护行为：拒绝决定恢复后 Run 进入 cancelled 终态（非系统错误），
    # 不创建执行记录与业务结果，提案保持 rejected。
    scope = build_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
    )
    decided = approve_decision(
        scope.store,
        organization_id=scope.org_a.organization_id,
        approval_id=creation.approval.approval_id,
        user_id=scope.alice.user_id,
        decision=ApprovalDecisionType.REJECTED,
        comment="不同意",
    )
    graph = build_graph(scope, tmp_path)
    start_graph(
        graph,
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
        thread_id=creation.run.thread_id,
    )
    result = resume_graph(
        graph,
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
        thread_id=creation.run.thread_id,
        decision_id=decided.decision.decision_id,
    )
    assert result.get("outcome") == OUTCOME_REJECTED
    run = scope.store.get_run(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    assert run.status is ActionRunStatus.CANCELLED
    assert run.completed_at is not None
    proposal = scope.store.get_proposal(
        organization_id=scope.org_a.organization_id,
        proposal_id=creation.proposal.proposal_id,
    )
    assert proposal.status is ProposalStatus.REJECTED
    assert count_rows(scope.database_path, "tool_executions") == 0
    assert count_rows(scope.database_path, "refund_records") == 0
    assert count_audit_events(
        scope, run_id=creation.run.run_id, event_type="run_cancelled"
    ) == 1


# —— 批准执行 ——


def test_approved_resume_executes_refund(tmp_path):
    # 保护行为：批准决定恢复后执行成功，图终态为 succeeded，
    # 只产生一条模拟退款记录。
    scope = build_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
    )
    decided = approve_decision(
        scope.store,
        organization_id=scope.org_a.organization_id,
        approval_id=creation.approval.approval_id,
        user_id=scope.alice.user_id,
    )
    graph = build_graph(scope, tmp_path)
    start_graph(
        graph,
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
        thread_id=creation.run.thread_id,
    )
    result = resume_graph(
        graph,
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
        thread_id=creation.run.thread_id,
        decision_id=decided.decision.decision_id,
    )
    assert result.get("outcome") == OUTCOME_SUCCEEDED
    run = scope.store.get_run(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    assert run.status is ActionRunStatus.SUCCEEDED
    assert count_rows(scope.database_path, "refund_records") == 1
    execution = scope.store.get_execution_by_version(
        organization_id=scope.org_a.organization_id,
        proposal_version_id=creation.version.version_id,
    )
    assert execution.status is ToolExecutionStatus.SUCCEEDED
    assert execution.attempt_count == 1


def test_approved_with_changes_executes_new_version(tmp_path):
    # 保护行为：修改后批准恢复后执行新版本参数（金额），
    # 业务结果严格使用批准版本。
    scope = build_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
        amount_cents=6000,
        refund_scope="partial",
    )
    decided = approve_decision(
        scope.store,
        organization_id=scope.org_a.organization_id,
        approval_id=creation.approval.approval_id,
        user_id=scope.alice.user_id,
        decision=ApprovalDecisionType.APPROVED_WITH_CHANGES,
        comment="改为部分退款",
        new_version=NewProposalVersion(
            amount_cents=4000,
            currency="CNY",
            reason_code="quality_issue",
            reason_text="仅对质量问题部分退款",
            parameters_json=json.dumps({"refund_scope": "partial"}),
        ),
    )
    graph = build_graph(scope, tmp_path)
    start_graph(
        graph,
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
        thread_id=creation.run.thread_id,
    )
    result = resume_graph(
        graph,
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
        thread_id=creation.run.thread_id,
        decision_id=decided.decision.decision_id,
    )
    assert result.get("outcome") == OUTCOME_SUCCEEDED
    with sqlite3.connect(scope.database_path) as connection:
        row = connection.execute(
            """
            SELECT amount_cents, proposal_version_id
            FROM refund_records
            """
        ).fetchone()
    assert tuple(row) == (4000, decided.decided_version.version_id)


def test_resume_forged_value_is_ignored(tmp_path):
    # 边界情况：恢复值伪造 decision_id、金额或批准标记时被忽略，
    # 执行严格使用业务库中的持久化决定（设计 11.3）。
    scope = build_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
        amount_cents=6000,
    )
    decided = approve_decision(
        scope.store,
        organization_id=scope.org_a.organization_id,
        approval_id=creation.approval.approval_id,
        user_id=scope.alice.user_id,
    )
    graph = build_graph(scope, tmp_path)
    start_graph(
        graph,
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
        thread_id=creation.run.thread_id,
    )
    result = graph.invoke(
        Command(
            resume={
                "decision_id": "forged-decision-id",
                "amount_cents": 1,
                "approved": True,
                "role": "admin",
            },
            update={
                "run_id": creation.run.run_id,
                "organization_id": scope.org_a.organization_id,
            },
        ),
        {"configurable": {"thread_id": creation.run.thread_id}},
    )
    assert result.get("outcome") == OUTCOME_SUCCEEDED
    # 业务结果金额是持久化决定的 6000 分，而不是伪造的 1 分。
    with sqlite3.connect(scope.database_path) as connection:
        row = connection.execute(
            "SELECT amount_cents FROM refund_records"
        ).fetchone()
    assert row[0] == 6000
    execution = scope.store.get_execution_by_version(
        organization_id=scope.org_a.organization_id,
        proposal_version_id=decided.decided_version.version_id,
    )
    assert execution is not None


# —— 重启与崩溃恢复 ——


def test_restart_recovery_with_recreated_checkpointer(tmp_path):
    # 保护行为：关闭并重新创建 checkpointer 与图实例后，
    # 使用原 thread_id 恢复并正常进入终态（设计 19 场景 E）。
    scope = build_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
    )
    graph = build_graph(scope, tmp_path)
    start_graph(
        graph,
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
        thread_id=creation.run.thread_id,
    )
    decided = approve_decision(
        scope.store,
        organization_id=scope.org_a.organization_id,
        approval_id=creation.approval.approval_id,
        user_id=scope.alice.user_id,
    )
    # 重新装配：新 checkpointer（同一文件）、新图实例。
    checkpointer = create_sqlite_checkpointer(tmp_path / "checkpoints.db")
    graph2 = build_action_graph(
        store=scope.store,
        executor=build_action_executor(scope.store),
        checkpointer=checkpointer,
    )
    result = resume_graph(
        graph2,
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
        thread_id=creation.run.thread_id,
        decision_id=decided.decision.decision_id,
    )
    assert result.get("outcome") == OUTCOME_SUCCEEDED
    run = scope.store.get_run(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    assert run.status is ActionRunStatus.SUCCEEDED
    assert count_rows(scope.database_path, "refund_records") == 1


def test_crash_after_success_replays_without_reexecution(tmp_path):
    # 保护行为：执行成功后 checkpoint 丢失（新线程无 checkpoint），
    # 从 START 重放返回同一稳定结果，不产生第二条业务记录。
    scope = build_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
    )
    decided = approve_decision(
        scope.store,
        organization_id=scope.org_a.organization_id,
        approval_id=creation.approval.approval_id,
        user_id=scope.alice.user_id,
    )
    graph = build_graph(scope, tmp_path)
    start_graph(
        graph,
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
        thread_id=creation.run.thread_id,
    )
    result = resume_graph(
        graph,
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
        thread_id=creation.run.thread_id,
        decision_id=decided.decision.decision_id,
    )
    assert result.get("outcome") == OUTCOME_SUCCEEDED
    first_record_id = scope.store.get_execution_by_version(
        organization_id=scope.org_a.organization_id,
        proposal_version_id=creation.version.version_id,
    ).result_json
    # 模拟 checkpoint 丢失：使用全新 thread_id 从 START 重新运行。
    replay = graph.invoke(
        {
            "run_id": creation.run.run_id,
            "organization_id": scope.org_a.organization_id,
        },
        {"configurable": {"thread_id": "thread-lost"}},
    )
    assert replay.get("outcome") == OUTCOME_SUCCEEDED
    assert count_rows(scope.database_path, "refund_records") == 1
    second_record_id = scope.store.get_execution_by_version(
        organization_id=scope.org_a.organization_id,
        proposal_version_id=creation.version.version_id,
    ).result_json
    assert second_record_id == first_record_id


def test_retryable_failure_then_resume_succeeds(tmp_path):
    # 保护行为：模拟执行器注入的临时故障使图进入失败终态（Run 可恢复），
    # 再次从 START 运行恢复后沿用原幂等键执行成功，只有一条业务结果。
    scope = build_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
    )
    decided = approve_decision(
        scope.store,
        organization_id=scope.org_a.organization_id,
        approval_id=creation.approval.approval_id,
        user_id=scope.alice.user_id,
    )
    graph = build_graph(scope, tmp_path, fail_count=1)
    start_graph(
        graph,
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
        thread_id=creation.run.thread_id,
    )
    result = resume_graph(
        graph,
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
        thread_id=creation.run.thread_id,
        decision_id=decided.decision.decision_id,
    )
    assert result.get("outcome") == OUTCOME_FAILED
    assert result.get("error_code") == "EXECUTION_RETRYABLE_FAILURE"
    assert result.get("error_retryable") is True
    failed = scope.store.get_execution_by_version(
        organization_id=scope.org_a.organization_id,
        proposal_version_id=creation.version.version_id,
    )
    assert failed.status is ToolExecutionStatus.FAILED_RETRYABLE
    failed_run = scope.store.get_run(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    assert failed_run.status is ActionRunStatus.FAILED
    assert failed_run.last_error_retryable is True

    # 恢复：图在 END 之后从 START 重新运行并路由到执行分支。
    recovery = graph.invoke(
        {
            "run_id": creation.run.run_id,
            "organization_id": scope.org_a.organization_id,
        },
        {"configurable": {"thread_id": creation.run.thread_id}},
    )
    assert recovery.get("outcome") == OUTCOME_SUCCEEDED
    succeeded = scope.store.get_execution_by_version(
        organization_id=scope.org_a.organization_id,
        proposal_version_id=creation.version.version_id,
    )
    assert succeeded.execution_id == failed.execution_id
    assert succeeded.attempt_count == 2
    assert count_rows(scope.database_path, "refund_records") == 1


class PreClaimFailureExecutor:
    """在创建执行记录前模拟批准链数据损坏的执行器。"""

    def execute(self, *, organization_id: str, run_id: str):
        """抛出稳定的数据完整性错误，不自行写入执行失败记录。"""
        raise ExecutionDataIntegrityError("批准链数据损坏")


def test_pre_claim_executor_failure_persists_run_and_proposal_failure(tmp_path):
    # 保护行为：执行认领前发生稳定领域错误时，图进入 END 之前必须原子
    # 写入 Proposal、Run 和失败审计，不能留下永久 running 状态。
    scope = build_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
    )
    graph = build_action_graph(
        store=scope.store,
        executor=PreClaimFailureExecutor(),
        checkpointer=create_sqlite_checkpointer(tmp_path / "checkpoints.db"),
    )
    start_graph(
        graph,
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
        thread_id=creation.run.thread_id,
    )
    decided = approve_decision(
        scope.store,
        organization_id=scope.org_a.organization_id,
        approval_id=creation.approval.approval_id,
        user_id=scope.alice.user_id,
    )
    result = resume_graph(
        graph,
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
        thread_id=creation.run.thread_id,
        decision_id=decided.decision.decision_id,
    )

    run = scope.store.get_run(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    proposal = scope.store.get_proposal(
        organization_id=scope.org_a.organization_id,
        proposal_id=creation.proposal.proposal_id,
    )
    assert result.get("outcome") == OUTCOME_FAILED
    assert result.get("error_code") == "EXECUTION_DATA_INTEGRITY_ERROR"
    assert run.status is ActionRunStatus.FAILED
    assert run.last_error_code == "EXECUTION_DATA_INTEGRITY_ERROR"
    assert run.last_error_retryable is False
    assert proposal.status is ProposalStatus.FAILED
    assert count_audit_events(
        scope,
        run_id=creation.run.run_id,
        event_type="run_failed",
    ) == 1
    assert count_rows(scope.database_path, "tool_executions") == 0


def test_terminal_run_restart_rejected(tmp_path):
    # 边界情况：已取消（拒绝终态）的 Run 从 START 重新运行时被拒绝，
    # 不允许对终态 Run 再次启动或恢复。
    scope = build_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
    )
    decided = approve_decision(
        scope.store,
        organization_id=scope.org_a.organization_id,
        approval_id=creation.approval.approval_id,
        user_id=scope.alice.user_id,
        decision=ApprovalDecisionType.REJECTED,
        comment="不同意",
    )
    graph = build_graph(scope, tmp_path)
    start_graph(
        graph,
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
        thread_id=creation.run.thread_id,
    )
    resume_graph(
        graph,
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
        thread_id=creation.run.thread_id,
        decision_id=decided.decision.decision_id,
    )
    with pytest.raises(RunStateConflictError):
        graph.invoke(
            {
                "run_id": creation.run.run_id,
                "organization_id": scope.org_a.organization_id,
            },
            {"configurable": {"thread_id": "thread-other"}},
        )


# —— 补偿分支 ——


def test_compensation_flow_via_graph(tmp_path):
    # 保护行为：补偿提案完整走通中断-批准-恢复-执行，
    # 写入固定 30 天有效期的模拟补偿记录。
    scope = build_scope(tmp_path)
    creation = create_compensation_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
    )
    decided = approve_decision(
        scope.store,
        organization_id=scope.org_a.organization_id,
        approval_id=creation.approval.approval_id,
        user_id=scope.alice.user_id,
    )
    graph = build_graph(scope, tmp_path)
    start_graph(
        graph,
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
        thread_id=creation.run.thread_id,
    )
    result = resume_graph(
        graph,
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
        thread_id=creation.run.thread_id,
        decision_id=decided.decision.decision_id,
    )
    assert result.get("outcome") == OUTCOME_SUCCEEDED
    assert count_rows(scope.database_path, "compensation_records") == 1
    with sqlite3.connect(scope.database_path) as connection:
        row = connection.execute(
            """
            SELECT coupon_valid_days
            FROM compensation_records
            """
        ).fetchone()
    assert row[0] == 30
