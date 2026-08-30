"""LangGraph 动作工作流运行器测试。

覆盖设计 11.5 / 18.4 的运行器验收点：start/resume 使用业务库中的
``thread_id``、审批后自动恢复、拒绝取消、可重试失败恢复沿用原幂等键，
以及基础设施异常归一化为稳定错误码。
"""

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.actions.base import (
    ActionRunStatus,
    ActionStore,
    ActionType,
    ApprovalDecisionType,
    AuditActorType,
    CheckpointUnavailableError,
    ProposalStatus,
    ToolExecutionStatus,
)
from app.actions.executor import (
    RetryableFailureInjector,
    build_idempotency_key,
)
from app.actions.factory import build_action_executor
from app.actions.sqlite_store import SQLiteActionStore
from app.customers.sqlite_store import SQLiteCustomerStore
from app.orders.base import Order, OrderStatus
from app.orders.sqlite_store import SQLiteOrderStore
from app.organizations.base import MembershipRole, Organization
from app.organizations.sqlite_store import SQLiteOrganizationStore
from app.users.base import User
from app.users.sqlite_store import SQLiteUserStore
from app.workflows.action_graph import build_action_graph
from app.workflows.checkpointer import create_sqlite_checkpointer
from app.workflows.runtime import LangGraphActionWorkflowRunner


# —— 测试环境搭建 ——


@dataclass
class ActionScope:
    """运行器测试所需的完整企业范围数据。"""

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


def approve_decision(
    store: ActionStore,
    *,
    organization_id: str,
    approval_id: str,
    user_id: str,
    decision: ApprovalDecisionType = ApprovalDecisionType.APPROVED,
    comment: str | None = "同意退款",
):
    """提交审批决定。"""
    return store.decide_approval(
        organization_id=organization_id,
        approval_id=approval_id,
        decided_by_user_id=user_id,
        decision=decision,
        comment=comment,
        new_version=None,
    )


def count_rows(database_path: Path, table: str) -> int:
    """统计指定业务表的行数。"""
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    return int(row[0])


def build_runtime(scope: ActionScope, tmp_path, *, fail_count: int = 0):
    """装配运行器：Store -> 执行器 -> 图（真实 SQLite checkpointer）。"""
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
    return LangGraphActionWorkflowRunner(store=scope.store, graph=graph)


# —— start / resume ——


def test_runtime_start_advances_run_to_awaiting(tmp_path):
    # 保护行为：start 把 Run 推进到等待审批，并保存中断 checkpoint，
    # checkpoint 线程标识来自 action_runs.thread_id。
    scope = build_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
    )
    runner = build_runtime(scope, tmp_path)
    runner.start(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    run = scope.store.get_run(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    assert run.status is ActionRunStatus.AWAITING_APPROVAL
    checkpoint = runner._graph.checkpointer.get_tuple(
        {"configurable": {"thread_id": run.thread_id}}
    )
    assert checkpoint is not None


def test_runtime_resume_after_decision_executes(tmp_path):
    # 保护行为：批准决定后 resume 从审批中断点继续并成功执行，
    # 只产生一条模拟退款记录。
    scope = build_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
    )
    runner = build_runtime(scope, tmp_path)
    runner.start(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    decided = approve_decision(
        scope.store,
        organization_id=scope.org_a.organization_id,
        approval_id=creation.approval.approval_id,
        user_id=scope.alice.user_id,
    )
    runner.resume(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    run = scope.store.get_run(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    assert run.status is ActionRunStatus.SUCCEEDED
    assert count_rows(scope.database_path, "refund_records") == 1
    execution = scope.store.get_execution_by_version(
        organization_id=scope.org_a.organization_id,
        proposal_version_id=decided.decided_version.version_id,
    )
    assert execution.status is ToolExecutionStatus.SUCCEEDED


def test_runtime_rebuilds_from_business_state_when_checkpoint_is_missing(tmp_path):
    # 保护行为：审批决定已经持久化但原 checkpoint 丢失时，恢复应从 START
    # 按业务库事实重建并执行成功，不能静默返回后仍停留在等待审批。
    scope = build_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
    )
    original_runner = build_runtime(scope, tmp_path)
    original_runner.start(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    approve_decision(
        scope.store,
        organization_id=scope.org_a.organization_id,
        approval_id=creation.approval.approval_id,
        user_id=scope.alice.user_id,
    )

    replacement_checkpointer = create_sqlite_checkpointer(
        tmp_path / "replacement-checkpoints.db"
    )
    replacement_graph = build_action_graph(
        store=scope.store,
        executor=build_action_executor(scope.store),
        checkpointer=replacement_checkpointer,
    )
    replacement_runner = LangGraphActionWorkflowRunner(
        store=scope.store,
        graph=replacement_graph,
    )
    replacement_runner.resume(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )

    run = scope.store.get_run(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    assert run.status is ActionRunStatus.SUCCEEDED
    assert count_rows(scope.database_path, "refund_records") == 1


def test_runtime_recovers_approved_running_execution_without_regression(tmp_path):
    # 保护行为：审批后 Run 和执行记录已进入 running 但进程中断时，恢复应
    # 重新认领原执行记录并成功，不能把 Run 退回 awaiting_approval。
    scope = build_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
    )
    runner = build_runtime(scope, tmp_path)
    runner.start(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    decided = approve_decision(
        scope.store,
        organization_id=scope.org_a.organization_id,
        approval_id=creation.approval.approval_id,
        user_id=scope.alice.user_id,
    )
    scope.store.transition_run(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
        expected_statuses=(ActionRunStatus.AWAITING_APPROVAL,),
        new_status=ActionRunStatus.RUNNING,
        error_code=None,
        error_retryable=False,
        actor_type=AuditActorType.SYSTEM,
        actor_user_id=None,
        event_type="run_resumed",
        details_json="{}",
    )
    idempotency_key = build_idempotency_key(
        organization_id=scope.org_a.organization_id,
        proposal_id=creation.proposal.proposal_id,
        proposal_version_id=decided.decided_version.version_id,
        action_type=ActionType.REFUND,
    )
    claim = scope.store.claim_execution(
        organization_id=scope.org_a.organization_id,
        proposal_id=creation.proposal.proposal_id,
        proposal_version_id=decided.decided_version.version_id,
        action_type=ActionType.REFUND,
        idempotency_key=idempotency_key,
    )
    interrupted = scope.store.mark_execution_running(
        organization_id=scope.org_a.organization_id,
        execution_id=claim.execution_id,
    )

    runner.resume(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )

    run = scope.store.get_run(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    execution = scope.store.get_execution_by_version(
        organization_id=scope.org_a.organization_id,
        proposal_version_id=decided.decided_version.version_id,
    )
    assert run.status is ActionRunStatus.SUCCEEDED
    assert execution.execution_id == interrupted.execution_id
    assert execution.status is ToolExecutionStatus.SUCCEEDED
    assert execution.attempt_count == 2
    assert count_rows(scope.database_path, "refund_records") == 1


def test_runtime_resume_rejected_run_cancels(tmp_path):
    # 保护行为：拒绝决定恢复后 Run 进入 cancelled 终态，
    # 不产生任何执行副作用。
    scope = build_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
    )
    runner = build_runtime(scope, tmp_path)
    runner.start(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    approve_decision(
        scope.store,
        organization_id=scope.org_a.organization_id,
        approval_id=creation.approval.approval_id,
        user_id=scope.alice.user_id,
        decision=ApprovalDecisionType.REJECTED,
        comment="不同意",
    )
    runner.resume(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    run = scope.store.get_run(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    assert run.status is ActionRunStatus.CANCELLED
    proposal = scope.store.get_proposal(
        organization_id=scope.org_a.organization_id,
        proposal_id=creation.proposal.proposal_id,
    )
    assert proposal.status is ProposalStatus.REJECTED
    assert count_rows(scope.database_path, "tool_executions") == 0
    assert count_rows(scope.database_path, "refund_records") == 0


def test_runtime_resume_uses_store_thread_id(tmp_path):
    # 保护行为：thread_id 始终取自 action_runs.thread_id，
    # 不接受调用方覆盖；自定义 thread_id 后 checkpoint 落在该线程上。
    scope = build_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
    )
    with sqlite3.connect(scope.database_path) as connection:
        connection.execute(
            """
            UPDATE action_runs
            SET thread_id = 'custom-thread'
            WHERE organization_id = ? AND id = ?
            """,
            (scope.org_a.organization_id, creation.run.run_id),
        )
    runner = build_runtime(scope, tmp_path)
    runner.start(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    # 默认线程（等于 run_id）上不存在 checkpoint，自定义线程上存在。
    assert (
        runner._graph.checkpointer.get_tuple(
            {"configurable": {"thread_id": creation.run.run_id}}
        )
        is None
    )
    assert (
        runner._graph.checkpointer.get_tuple(
            {"configurable": {"thread_id": "custom-thread"}}
        )
        is not None
    )


# —— 可重试失败恢复 ——


def test_runtime_retryable_failure_then_resume_succeeds(tmp_path):
    # 保护行为：模拟注入的临时故障记录为可重试失败，Run 可恢复；
    # 再次 resume 沿用原执行记录与幂等键，attempt 递增并成功执行。
    scope = build_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
    )
    runner = build_runtime(scope, tmp_path, fail_count=1)
    runner.start(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    approve_decision(
        scope.store,
        organization_id=scope.org_a.organization_id,
        approval_id=creation.approval.approval_id,
        user_id=scope.alice.user_id,
    )
    runner.resume(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
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
    assert count_rows(scope.database_path, "refund_records") == 0

    runner.resume(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    succeeded = scope.store.get_execution_by_version(
        organization_id=scope.org_a.organization_id,
        proposal_version_id=creation.version.version_id,
    )
    assert succeeded.execution_id == failed.execution_id
    assert succeeded.attempt_count == 2
    assert succeeded.status is ToolExecutionStatus.SUCCEEDED
    assert count_rows(scope.database_path, "refund_records") == 1


# —— 基础设施错误归一化 ——


class BrokenGraph:
    """测试用故障图：invoke 直接抛基础设施异常。"""

    def invoke(self, *args, **kwargs):
        raise RuntimeError("checkpoint 数据库暂时锁定")


def test_runtime_maps_infra_failure_to_stable_error(tmp_path):
    # 保护行为：checkpoint 或图基础设施异常归一化为
    # CHECKPOINT_UNAVAILABLE 稳定错误码，不泄露内部细节。
    scope = build_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
    )
    runner = LangGraphActionWorkflowRunner(
        store=scope.store,
        graph=BrokenGraph(),
    )
    with pytest.raises(CheckpointUnavailableError) as exc_info:
        runner.start(
            organization_id=scope.org_a.organization_id,
            run_id=creation.run.run_id,
        )
    assert exc_info.value.code == "CHECKPOINT_UNAVAILABLE"
    # 业务事实仍然持久化，可稍后显式恢复。
    run = scope.store.get_run(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    assert run.status is ActionRunStatus.QUEUED
