"""幂等模拟执行器测试。

覆盖设计文档 Task 4 / 18.3 的验收点：没有批准决定时永远拒绝、重复执行
返回同一结果 ID、全额退款更新订单状态、部分退款不更新主状态、执行时余额
与补偿上限重验、同原因补偿不能重复、可重试失败沿用原幂等键并递增尝试次数，
以及跨租户与非法运行状态的拒绝语义。
"""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest

from app.actions.base import (
    ActionCompensationCapExceededError,
    ActionCompensationDuplicateError,
    ActionRefundBalanceExceededError,
    ActionRunStatus,
    ActionStore,
    ActionType,
    ApprovalDecisionType,
    AuditActorType,
    ExecutionNotApprovedError,
    ExecutionRetryableFailureError,
    NewProposalVersion,
    ProposalStatus,
    RefundScope,
    RunNotFoundError,
    RunStateConflictError,
    ToolExecutionStatus,
)
from app.actions.executor import (
    ExecutionOutcome,
    IdempotentActionExecutor,
    RetryableFailureInjector,
    SimulatedCompensationAdapter,
    SimulatedRefundAdapter,
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


# —— 测试环境搭建 ——


@dataclass
class ActionScope:
    """执行器测试所需的完整企业范围数据。"""

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


def advance_run_to_running(
    store: ActionStore,
    *,
    organization_id: str,
    run_id: str,
):
    """把 Run 推进到 running（模拟图在启动或审批后进入执行前状态）。

    覆盖三种入口：新建后首次启动（queued）、审批后自动恢复
    （awaiting_approval）、可重试失败后的显式恢复（failed）。
    """
    run = store.get_run(
        organization_id=organization_id,
        run_id=run_id,
    )
    if run.status is ActionRunStatus.QUEUED:
        expected = (ActionRunStatus.QUEUED,)
    elif run.status is ActionRunStatus.AWAITING_APPROVAL:
        expected = (ActionRunStatus.AWAITING_APPROVAL,)
    elif run.status is ActionRunStatus.FAILED:
        expected = (ActionRunStatus.FAILED,)
    else:
        return
    store.transition_run(
        organization_id=organization_id,
        run_id=run_id,
        expected_statuses=expected,
        new_status=ActionRunStatus.RUNNING,
        error_code=None,
        error_retryable=False,
        actor_type=AuditActorType.SYSTEM,
        actor_user_id=None,
        event_type="run_resumed",
        details_json="{}",
    )


def count_rows(database_path: Path, table: str) -> int:
    """统计指定业务表的行数。"""
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    return int(row[0])


def insert_phantom_refund(
    database_path: Path,
    *,
    org_id: str,
    user_id: str,
    order_id: str,
    amount_cents: int,
    reason_code: str = "quality_issue",
) -> None:
    """以终态引用链直接写入一条「外部已发生」的退款结果。

    模拟审批等待期间其他路径写入的退款事实，用于验证执行事务内的
    金额重验不受既有记录之外的竞争影响。
    """
    run_id = f"run-phantom-{uuid4()}"
    proposal_id = f"proposal-phantom-{uuid4()}"
    version_id = f"version-phantom-{uuid4()}"
    execution_id = f"execution-phantom-{uuid4()}"
    record_id = f"record-phantom-{uuid4()}"
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            INSERT INTO action_runs(
                id, organization_id, conversation_id, turn_id,
                created_by_user_id, workflow_type, status, thread_id,
                proposal_id
            ) VALUES (?, ?, 'phantom', ?, ?, 'refund', 'succeeded', ?, NULL)
            """,
            (run_id, org_id, run_id, user_id, run_id),
        )
        connection.execute(
            """
            INSERT INTO action_proposals(
                id, organization_id, run_id, order_id, action_type,
                status, current_version_id, created_by_user_id
            ) VALUES (?, ?, ?, ?, 'refund', 'succeeded', NULL, ?)
            """,
            (proposal_id, org_id, run_id, order_id, user_id),
        )
        connection.execute(
            """
            INSERT INTO action_proposal_versions(
                id, organization_id, proposal_id, version_no, amount_cents,
                currency, reason_code, reason_text, parameters_json,
                created_by_user_id
            ) VALUES (?, ?, ?, 1, ?, 'CNY', ?, 'phantom',
                      '{"refund_scope": "partial"}', ?)
            """,
            (version_id, org_id, proposal_id, amount_cents, reason_code, user_id),
        )
        connection.execute(
            """
            UPDATE action_proposals
            SET current_version_id = ?
            WHERE organization_id = ? AND id = ?
            """,
            (version_id, org_id, proposal_id),
        )
        connection.execute(
            """
            INSERT INTO tool_executions(
                id, organization_id, proposal_id, proposal_version_id,
                action_type, idempotency_key, status, attempt_count
            ) VALUES (?, ?, ?, ?, 'refund', ?, 'succeeded', 1)
            """,
            (execution_id, org_id, proposal_id, version_id, f"key-{execution_id}"),
        )
        connection.execute(
            """
            INSERT INTO refund_records(
                id, organization_id, order_id, proposal_id,
                proposal_version_id, tool_execution_id, amount_cents,
                currency, reason_code, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'CNY', ?, 'simulated_succeeded')
            """,
            (
                record_id,
                org_id,
                order_id,
                proposal_id,
                version_id,
                execution_id,
                amount_cents,
                reason_code,
            ),
        )
        connection.execute(
            """
            UPDATE action_runs
            SET proposal_id = ?
            WHERE organization_id = ? AND id = ?
            """,
            (proposal_id, org_id, run_id),
        )


def insert_phantom_compensation(
    database_path: Path,
    *,
    org_id: str,
    user_id: str,
    order_id: str,
    amount_cents: int,
    reason_code: str,
) -> None:
    """以终态引用链直接写入一条「外部已发生」的补偿结果。"""
    run_id = f"run-phantom-{uuid4()}"
    proposal_id = f"proposal-phantom-{uuid4()}"
    version_id = f"version-phantom-{uuid4()}"
    execution_id = f"execution-phantom-{uuid4()}"
    record_id = f"record-phantom-{uuid4()}"
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            INSERT INTO action_runs(
                id, organization_id, conversation_id, turn_id,
                created_by_user_id, workflow_type, status, thread_id,
                proposal_id
            ) VALUES (?, ?, 'phantom', ?, ?, 'compensation', 'succeeded', ?, NULL)
            """,
            (run_id, org_id, run_id, user_id, run_id),
        )
        connection.execute(
            """
            INSERT INTO action_proposals(
                id, organization_id, run_id, order_id, action_type,
                status, current_version_id, created_by_user_id
            ) VALUES (?, ?, ?, ?, 'compensation', 'succeeded', NULL, ?)
            """,
            (proposal_id, org_id, run_id, order_id, user_id),
        )
        connection.execute(
            """
            INSERT INTO action_proposal_versions(
                id, organization_id, proposal_id, version_no, amount_cents,
                currency, reason_code, reason_text, parameters_json,
                created_by_user_id
            ) VALUES (?, ?, ?, 1, ?, 'CNY', ?, 'phantom',
                      '{"coupon_valid_days": 30}', ?)
            """,
            (version_id, org_id, proposal_id, amount_cents, reason_code, user_id),
        )
        connection.execute(
            """
            UPDATE action_proposals
            SET current_version_id = ?
            WHERE organization_id = ? AND id = ?
            """,
            (version_id, org_id, proposal_id),
        )
        connection.execute(
            """
            INSERT INTO tool_executions(
                id, organization_id, proposal_id, proposal_version_id,
                action_type, idempotency_key, status, attempt_count
            ) VALUES (?, ?, ?, ?, 'compensation', ?, 'succeeded', 1)
            """,
            (execution_id, org_id, proposal_id, version_id, f"key-{execution_id}"),
        )
        connection.execute(
            """
            INSERT INTO compensation_records(
                id, organization_id, order_id, proposal_id,
                proposal_version_id, tool_execution_id, amount_cents,
                currency, reason_code, coupon_valid_days, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'CNY', ?, 30, 'simulated_succeeded')
            """,
            (
                record_id,
                org_id,
                order_id,
                proposal_id,
                version_id,
                execution_id,
                amount_cents,
                reason_code,
            ),
        )
        connection.execute(
            """
            UPDATE action_runs
            SET proposal_id = ?
            WHERE organization_id = ? AND id = ?
            """,
            (proposal_id, org_id, run_id),
        )


def build_executor(scope: ActionScope) -> IdempotentActionExecutor:
    """通过工厂装配默认幂等模拟执行器。"""
    return build_action_executor(scope.store)


# —— 幂等键与注入器 ——


def test_build_idempotency_key_matches_design_format():
    # 保护行为：幂等键严格使用设计 9.6 的版本化格式，
    # 且不包含任何可被调用者伪造的输入。
    key = build_idempotency_key(
        organization_id="org-1",
        proposal_id="proposal-1",
        proposal_version_id="version-2",
        action_type=ActionType.REFUND,
    )
    assert key == (
        "action-execution:v1:org-1:proposal-1:version-2:refund"
    )
    assert key == (
        "action-execution:v1:org-1:proposal-1:version-2:"
        + ActionType.REFUND.value
    )


def test_retryable_failure_injector_limits_injections():
    # 保护行为：故障注入器只在前 N 次调用抛可重试失败，
    # 后续调用恢复正常，用于模拟临时基础设施故障。
    injector = RetryableFailureInjector(fail_count=2)
    with pytest.raises(ExecutionRetryableFailureError):
        injector()
    with pytest.raises(ExecutionRetryableFailureError):
        injector()
    injector()  # 第三次不再注入


# —— 授权链验证 ——


def test_executor_rejects_without_approval_decision(tmp_path):
    # 保护行为：没有持久化批准决定时，任何调用路径都拒绝执行，
    # 且不创建执行记录、不写业务结果（设计 12.5 工具暴露边界）。
    scope = build_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
    )
    executor = build_executor(scope)
    with pytest.raises(ExecutionNotApprovedError) as exc_info:
        executor.execute(
            organization_id=scope.org_a.organization_id,
            run_id=creation.run.run_id,
        )
    assert exc_info.value.code == "EXECUTION_NOT_APPROVED"
    assert count_rows(scope.database_path, "tool_executions") == 0
    assert count_rows(scope.database_path, "refund_records") == 0


def test_executor_rejects_rejected_decision(tmp_path):
    # 边界情况：审批已拒绝时执行同样被拒绝（EXECUTION_NOT_APPROVED），
    # 拒绝决定不能成为执行授权。
    scope = build_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
    )
    approve_decision(
        scope.store,
        organization_id=scope.org_a.organization_id,
        approval_id=creation.approval.approval_id,
        user_id=scope.alice.user_id,
        decision=ApprovalDecisionType.REJECTED,
        comment="不同意",
    )
    executor = build_executor(scope)
    with pytest.raises(ExecutionNotApprovedError):
        executor.execute(
            organization_id=scope.org_a.organization_id,
            run_id=creation.run.run_id,
        )
    assert count_rows(scope.database_path, "tool_executions") == 0


def test_executor_rejects_cross_tenant_run(tmp_path):
    # 边界情况：企业 B 使用企业 A 的 Run 标识执行时，
    # 与不存在 Run 一样返回 RUN_NOT_FOUND（设计 19 场景 F）。
    scope = build_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
    )
    executor = build_executor(scope)
    with pytest.raises(RunNotFoundError):
        executor.execute(
            organization_id=scope.org_b.organization_id,
            run_id=creation.run.run_id,
        )


def test_executor_rejects_non_running_run(tmp_path):
    # 边界情况：决定已落库但 Run 尚未由工作流推进到 running 时，
    # 拒绝执行且不产生执行记录（执行前状态由工作流负责推进）。
    scope = build_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
    )
    approve_decision(
        scope.store,
        organization_id=scope.org_a.organization_id,
        approval_id=creation.approval.approval_id,
        user_id=scope.alice.user_id,
    )
    executor = build_executor(scope)
    with pytest.raises(RunStateConflictError):
        executor.execute(
            organization_id=scope.org_a.organization_id,
            run_id=creation.run.run_id,
        )
    assert count_rows(scope.database_path, "tool_executions") == 0


def test_executor_rejects_terminal_run(tmp_path):
    # 边界情况：Run 已进入终态（cancelled）时拒绝执行，
    # 终态 Run 不允许任何执行副作用。
    scope = build_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
    )
    approve_decision(
        scope.store,
        organization_id=scope.org_a.organization_id,
        approval_id=creation.approval.approval_id,
        user_id=scope.alice.user_id,
    )
    advance_run_to_running(
        scope.store,
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    scope.store.transition_run(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
        expected_statuses=(ActionRunStatus.RUNNING,),
        new_status=ActionRunStatus.CANCELLED,
        error_code=None,
        error_retryable=False,
        actor_type=AuditActorType.SYSTEM,
        actor_user_id=None,
        event_type="run_cancelled",
        details_json="{}",
    )
    executor = build_executor(scope)
    with pytest.raises(RunStateConflictError):
        executor.execute(
            organization_id=scope.org_a.organization_id,
            run_id=creation.run.run_id,
        )
    assert count_rows(scope.database_path, "tool_executions") == 0


# —— 退款执行 ——


def test_executor_executes_approved_partial_refund(tmp_path):
    # 保护行为：批准的部分退款执行成功，写入一条金额币种原因一致的
    # 模拟退款记录，订单主状态不改变，Run/提案/执行进入成功终态。
    scope = build_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
        amount_cents=6000,
    )
    approve_decision(
        scope.store,
        organization_id=scope.org_a.organization_id,
        approval_id=creation.approval.approval_id,
        user_id=scope.alice.user_id,
    )
    advance_run_to_running(
        scope.store,
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    executor = build_executor(scope)
    outcome = executor.execute(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    assert isinstance(outcome, ExecutionOutcome)
    assert outcome.replayed is False
    assert outcome.order_marked_refunded is False
    assert outcome.business_record_id is not None
    assert outcome.execution.status is ToolExecutionStatus.SUCCEEDED
    assert outcome.execution.idempotency_key == build_idempotency_key(
        organization_id=scope.org_a.organization_id,
        proposal_id=creation.proposal.proposal_id,
        proposal_version_id=creation.version.version_id,
        action_type=ActionType.REFUND,
    )
    # 业务结果与状态终态。
    assert count_rows(scope.database_path, "refund_records") == 1
    with sqlite3.connect(scope.database_path) as connection:
        row = connection.execute(
            """
            SELECT amount_cents, currency, reason_code, proposal_version_id
            FROM refund_records
            """
        ).fetchone()
    assert tuple(row) == (
        6000,
        "CNY",
        "quality_issue",
        creation.version.version_id,
    )
    run = scope.store.get_run(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    assert run.status is ActionRunStatus.SUCCEEDED
    assert run.completed_at is not None
    proposal = scope.store.get_proposal(
        organization_id=scope.org_a.organization_id,
        proposal_id=creation.proposal.proposal_id,
    )
    assert proposal.status is ProposalStatus.SUCCEEDED
    order = scope.store.get_refundable_balance(
        organization_id=scope.org_a.organization_id,
        order_id=scope.order_a.order_id,
    )
    assert order == 4000


def test_executor_full_refund_marks_order_refunded(tmp_path):
    # 保护行为：全额退款执行成功后把订单主状态置为 refunded，
    # 结果标记与最新可退余额归零一致（设计 8.2）。
    scope = build_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
        amount_cents=10000,
        refund_scope="full",
    )
    approve_decision(
        scope.store,
        organization_id=scope.org_a.organization_id,
        approval_id=creation.approval.approval_id,
        user_id=scope.alice.user_id,
    )
    advance_run_to_running(
        scope.store,
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    outcome = build_executor(scope).execute(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    assert outcome.order_marked_refunded is True
    with sqlite3.connect(scope.database_path) as connection:
        row = connection.execute(
            """
            SELECT status
            FROM orders
            WHERE organization_id = ? AND id = ?
            """,
            (scope.org_a.organization_id, scope.order_a.order_id),
        ).fetchone()
    assert row[0] == "refunded"
    assert scope.store.get_refundable_balance(
        organization_id=scope.org_a.organization_id,
        order_id=scope.order_a.order_id,
    ) == 0


def test_executor_replay_returns_same_business_record(tmp_path):
    # 保护行为：同一批准 Run 被重复执行时返回同一业务结果 ID 与执行记录，
    # 数据库中只有一条模拟退款记录（设计 19 场景 D 节点重放安全）。
    scope = build_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
    )
    approve_decision(
        scope.store,
        organization_id=scope.org_a.organization_id,
        approval_id=creation.approval.approval_id,
        user_id=scope.alice.user_id,
    )
    advance_run_to_running(
        scope.store,
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    executor = build_executor(scope)
    first = executor.execute(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    second = executor.execute(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    assert first.business_record_id == second.business_record_id
    assert first.execution.execution_id == second.execution.execution_id
    assert second.replayed is True
    assert count_rows(scope.database_path, "refund_records") == 1


@pytest.mark.parametrize("mark_running", [False, True])
def test_executor_reclaims_interrupted_claimed_or_running_execution(
    tmp_path,
    mark_running,
):
    # 保护行为：上次进程在 claimed 或 running 状态中断后，
    # 新进程沿用原执行记录和幂等键重新认领，不会永久卡死。
    scope = build_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
    )
    approve_decision(
        scope.store,
        organization_id=scope.org_a.organization_id,
        approval_id=creation.approval.approval_id,
        user_id=scope.alice.user_id,
    )
    advance_run_to_running(
        scope.store,
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    idempotency_key = build_idempotency_key(
        organization_id=scope.org_a.organization_id,
        proposal_id=creation.proposal.proposal_id,
        proposal_version_id=creation.version.version_id,
        action_type=ActionType.REFUND,
    )
    claim = scope.store.claim_execution(
        organization_id=scope.org_a.organization_id,
        proposal_id=creation.proposal.proposal_id,
        proposal_version_id=creation.version.version_id,
        action_type=ActionType.REFUND,
        idempotency_key=idempotency_key,
    )
    if mark_running:
        scope.store.mark_execution_running(
            organization_id=scope.org_a.organization_id,
            execution_id=claim.execution_id,
        )
    outcome = build_executor(scope).execute(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    assert outcome.execution.execution_id == claim.execution_id
    assert outcome.execution.idempotency_key == idempotency_key
    assert outcome.execution.attempt_count == 2
    assert outcome.execution.status is ToolExecutionStatus.SUCCEEDED
    assert count_rows(scope.database_path, "refund_records") == 1


def test_executor_process_lock_prevents_active_execution_from_being_reclaimed(tmp_path):
    # 保护行为：同一进程的首个调用仍在执行适配器时，
    # 第二个调用必须等待并重放成功结果，不能抢占 running 记录。
    scope = build_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
    )
    approve_decision(
        scope.store,
        organization_id=scope.org_a.organization_id,
        approval_id=creation.approval.approval_id,
        user_id=scope.alice.user_id,
    )
    advance_run_to_running(
        scope.store,
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    adapter_entered = Event()
    adapter_release = Event()
    adapter_calls: list[int] = []  # 退款适配器实际调用次数

    def pause_adapter() -> None:
        """暂停首次适配器调用，为并发调用创造可观测窗口。"""
        adapter_calls.append(1)
        adapter_entered.set()
        assert adapter_release.wait(timeout=5)

    executor = IdempotentActionExecutor(
        store=scope.store,
        refund_adapter=SimulatedRefundAdapter(
            failure_injector=pause_adapter,
        ),
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(
            executor.execute,
            organization_id=scope.org_a.organization_id,
            run_id=creation.run.run_id,
        )
        assert adapter_entered.wait(timeout=5)
        second_future = pool.submit(
            executor.execute,
            organization_id=scope.org_a.organization_id,
            run_id=creation.run.run_id,
        )
        adapter_release.set()
        first = first_future.result(timeout=5)
        second = second_future.result(timeout=5)
    assert len(adapter_calls) == 1
    assert first.business_record_id == second.business_record_id
    assert {first.replayed, second.replayed} == {False, True}
    assert count_rows(scope.database_path, "refund_records") == 1


def test_executor_executes_approved_with_changes_version(tmp_path):
    # 保护行为：修改后批准执行新版本参数（金额与范围），
    # 业务结果严格使用批准版本，原版本保持可审计。
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
    new_version_id = decided.decided_version.version_id
    assert new_version_id != creation.version.version_id
    advance_run_to_running(
        scope.store,
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    outcome = build_executor(scope).execute(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    assert outcome.replayed is False
    assert outcome.execution.proposal_version_id == new_version_id
    assert outcome.execution.idempotency_key == build_idempotency_key(
        organization_id=scope.org_a.organization_id,
        proposal_id=creation.proposal.proposal_id,
        proposal_version_id=new_version_id,
        action_type=ActionType.REFUND,
    )
    with sqlite3.connect(scope.database_path) as connection:
        row = connection.execute(
            """
            SELECT amount_cents, proposal_version_id
            FROM refund_records
            """
        ).fetchone()
    assert tuple(row) == (4000, new_version_id)
    # 历史版本仍然存在且未被覆盖。
    versions = scope.store.list_versions(
        organization_id=scope.org_a.organization_id,
        proposal_id=creation.proposal.proposal_id,
    )
    assert [item.version_no for item in versions] == [1, 2]


def test_executor_refund_balance_exceeded_at_execution(tmp_path):
    # 保护行为：审批等待期间订单可退余额下降后，执行事务内重验余额，
    # 超过余额时执行失败并进入不可重试终态，不写任何业务结果。
    scope = build_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
        amount_cents=4000,
    )
    approve_decision(
        scope.store,
        organization_id=scope.org_a.organization_id,
        approval_id=creation.approval.approval_id,
        user_id=scope.alice.user_id,
    )
    advance_run_to_running(
        scope.store,
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    # 模拟审批等待期间外部写入 7000 元退款，剩余可退余额仅 3000 元。
    insert_phantom_refund(
        scope.database_path,
        org_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
        amount_cents=7000,
    )
    with pytest.raises(ActionRefundBalanceExceededError) as exc_info:
        build_executor(scope).execute(
            organization_id=scope.org_a.organization_id,
            run_id=creation.run.run_id,
        )
    assert exc_info.value.code == "ACTION_REFUND_BALANCE_EXCEEDED"
    # 执行记录进入不可重试终态，业务结果只有外部那条。
    execution = scope.store.get_execution_by_version(
        organization_id=scope.org_a.organization_id,
        proposal_version_id=creation.version.version_id,
    )
    assert execution.status is ToolExecutionStatus.FAILED_TERMINAL
    assert execution.error_code == "ACTION_REFUND_BALANCE_EXCEEDED"
    assert execution.completed_at is not None
    assert count_rows(scope.database_path, "refund_records") == 1
    run = scope.store.get_run(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    assert run.status is ActionRunStatus.FAILED
    assert run.completed_at is not None


def test_executor_uses_success_transaction_balance_for_refunded_status(tmp_path):
    # 保护行为：适配器调用期间发生另一笔合法退款后，
    # 本次退款仍按成功事务内最新余额判断订单是否全额退完。
    scope = build_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
        amount_cents=4000,
    )
    approve_decision(
        scope.store,
        organization_id=scope.org_a.organization_id,
        approval_id=creation.approval.approval_id,
        user_id=scope.alice.user_id,
    )
    advance_run_to_running(
        scope.store,
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )

    def insert_concurrent_refund() -> None:
        """在适配器与成功写入事务之间插入另一笔退款。"""
        insert_phantom_refund(
            scope.database_path,
            org_id=scope.org_a.organization_id,
            user_id=scope.alice.user_id,
            order_id=scope.order_a.order_id,
            amount_cents=6000,
        )

    executor = IdempotentActionExecutor(
        store=scope.store,
        refund_adapter=SimulatedRefundAdapter(
            failure_injector=insert_concurrent_refund,
        ),
    )
    outcome = executor.execute(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    assert outcome.order_marked_refunded is True
    assert count_rows(scope.database_path, "refund_records") == 2
    with sqlite3.connect(scope.database_path) as connection:
        order_status = connection.execute(
            "SELECT status FROM orders WHERE id = ?",
            (scope.order_a.order_id,),
        ).fetchone()[0]
    assert order_status == "refunded"


# —— 补偿执行 ——


def test_executor_executes_compensation(tmp_path):
    # 保护行为：批准的补偿执行成功，写入一条 30 天有效期的模拟补偿记录，
    # 订单主状态不改变。
    scope = build_scope(tmp_path)
    creation = create_compensation_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
        amount_cents=3000,
    )
    approve_decision(
        scope.store,
        organization_id=scope.org_a.organization_id,
        approval_id=creation.approval.approval_id,
        user_id=scope.alice.user_id,
    )
    advance_run_to_running(
        scope.store,
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    outcome = build_executor(scope).execute(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    assert outcome.replayed is False
    assert outcome.order_marked_refunded is False
    assert count_rows(scope.database_path, "compensation_records") == 1
    with sqlite3.connect(scope.database_path) as connection:
        row = connection.execute(
            """
            SELECT amount_cents, currency, reason_code, coupon_valid_days
            FROM compensation_records
            """
        ).fetchone()
    assert tuple(row) == (3000, "CNY", "delayed_shipment", 30)
    run = scope.store.get_run(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    assert run.status is ActionRunStatus.SUCCEEDED


def test_executor_compensation_duplicate_rejected(tmp_path):
    # 保护行为：执行时同一订单同一补偿原因已有成功补偿记录时拒绝，
    # 进入不可重试终态且不写第二条补偿记录。
    scope = build_scope(tmp_path)
    creation = create_compensation_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
        amount_cents=1500,
        reason_code="transit_delay",
    )
    approve_decision(
        scope.store,
        organization_id=scope.org_a.organization_id,
        approval_id=creation.approval.approval_id,
        user_id=scope.alice.user_id,
    )
    advance_run_to_running(
        scope.store,
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    # 模拟审批等待期间同原因补偿已成功。
    insert_phantom_compensation(
        scope.database_path,
        org_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
        amount_cents=1500,
        reason_code="transit_delay",
    )
    with pytest.raises(ActionCompensationDuplicateError) as exc_info:
        build_executor(scope).execute(
            organization_id=scope.org_a.organization_id,
            run_id=creation.run.run_id,
        )
    assert exc_info.value.code == "ACTION_COMPENSATION_DUPLICATE"
    execution = scope.store.get_execution_by_version(
        organization_id=scope.org_a.organization_id,
        proposal_version_id=creation.version.version_id,
    )
    assert execution.status is ToolExecutionStatus.FAILED_TERMINAL
    assert count_rows(scope.database_path, "compensation_records") == 1


def test_executor_compensation_cap_exceeded(tmp_path):
    # 保护行为：执行时其他原因补偿累计后超过订单金额 50% 上限时拒绝，
    # 进入不可重试终态且不写第二条补偿记录（50% 上限向下取整）。
    scope = build_scope(tmp_path)
    creation = create_compensation_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
        amount_cents=3000,
        reason_code="customer_dispute",
    )
    approve_decision(
        scope.store,
        organization_id=scope.org_a.organization_id,
        approval_id=creation.approval.approval_id,
        user_id=scope.alice.user_id,
    )
    advance_run_to_running(
        scope.store,
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    # 模拟审批等待期间其他原因补偿 3000 元，累计 6000 超过 5000 上限。
    insert_phantom_compensation(
        scope.database_path,
        org_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
        amount_cents=3000,
        reason_code="delayed_shipment",
    )
    with pytest.raises(ActionCompensationCapExceededError) as exc_info:
        build_executor(scope).execute(
            organization_id=scope.org_a.organization_id,
            run_id=creation.run.run_id,
        )
    assert exc_info.value.code == "ACTION_COMPENSATION_CAP_EXCEEDED"
    execution = scope.store.get_execution_by_version(
        organization_id=scope.org_a.organization_id,
        proposal_version_id=creation.version.version_id,
    )
    assert execution.status is ToolExecutionStatus.FAILED_TERMINAL
    assert count_rows(scope.database_path, "compensation_records") == 1


# —— 可重试失败 ——


def test_executor_retryable_failure_reuses_key_and_increments_attempt(tmp_path):
    # 保护行为：模拟执行器注入的临时故障记录为可重试失败，
    # 显式恢复后沿用原幂等键重新认领、尝试次数递增，最终只有一条业务结果。
    scope = build_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
        amount_cents=6000,
    )
    approve_decision(
        scope.store,
        organization_id=scope.org_a.organization_id,
        approval_id=creation.approval.approval_id,
        user_id=scope.alice.user_id,
    )
    advance_run_to_running(
        scope.store,
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    executor = build_action_executor(
        scope.store,
        failure_injector=RetryableFailureInjector(fail_count=1),
    )
    with pytest.raises(ExecutionRetryableFailureError):
        executor.execute(
            organization_id=scope.org_a.organization_id,
            run_id=creation.run.run_id,
        )
    # 第一次尝试：可重试失败，attempt=1，Run 可恢复失败且 completed_at 为空。
    failed = scope.store.get_execution_by_version(
        organization_id=scope.org_a.organization_id,
        proposal_version_id=creation.version.version_id,
    )
    assert failed.status is ToolExecutionStatus.FAILED_RETRYABLE
    assert failed.attempt_count == 1
    assert failed.completed_at is None
    failed_run = scope.store.get_run(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    assert failed_run.status is ActionRunStatus.FAILED
    assert failed_run.last_error_retryable is True
    assert failed_run.last_error_code == "EXECUTION_RETRYABLE_FAILURE"
    assert count_rows(scope.database_path, "refund_records") == 0

    # 显式恢复：Run 回到 running 后再次执行，沿用原执行记录与幂等键。
    advance_run_to_running(
        scope.store,
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    outcome = executor.execute(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    assert outcome.replayed is False
    succeeded = scope.store.get_execution_by_version(
        organization_id=scope.org_a.organization_id,
        proposal_version_id=creation.version.version_id,
    )
    assert succeeded.execution_id == failed.execution_id
    assert succeeded.attempt_count == 2
    assert succeeded.idempotency_key == build_idempotency_key(
        organization_id=scope.org_a.organization_id,
        proposal_id=creation.proposal.proposal_id,
        proposal_version_id=creation.version.version_id,
        action_type=ActionType.REFUND,
    )
    assert count_rows(scope.database_path, "refund_records") == 1


# —— 适配器单元行为 ——


def test_refund_adapter_only_generates_simulated_record_id():
    # 保护行为：退款适配器只生成模拟业务结果标识，
    # 不根据事务外余额快照决定订单 refunded 状态。
    adapter = SimulatedRefundAdapter()
    first = adapter.execute(
        order_id="order-1",
        amount_cents=6000,
        currency="CNY",
        reason_code="quality_issue",
        refund_scope=RefundScope.PARTIAL,
    )
    second = adapter.execute(
        order_id="order-1",
        amount_cents=6000,
        currency="CNY",
        reason_code="quality_issue",
        refund_scope=RefundScope.PARTIAL,
    )
    assert first.business_record_id.startswith("refund-sim-")
    assert first.business_record_id != second.business_record_id


def test_compensation_adapter_generates_stable_record_id():
    # 保护行为：补偿适配器每次调用生成唯一业务结果标识，
    # 不依赖外部渠道且不产生真实发券副作用。
    adapter = SimulatedCompensationAdapter()
    first = adapter.execute(
        order_id="order-1",
        amount_cents=3000,
        currency="CNY",
        reason_code="delayed_shipment",
        coupon_valid_days=30,
    )
    second = adapter.execute(
        order_id="order-1",
        amount_cents=3000,
        currency="CNY",
        reason_code="delayed_shipment",
        coupon_valid_days=30,
    )
    assert first.business_record_id != second.business_record_id
    assert first.business_record_id.startswith("compensation-sim-")
