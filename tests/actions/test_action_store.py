"""动作工作流 SQLite 存储实现的事务与不变量测试。

覆盖设计文档 Task 2 的验收点：提案版本、审批决定、Run 状态转换、
幂等认领、业务结果与审计；并发决定、金额竞争与稳定结果读取。
"""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest

from app.actions.base import (
    ActionActiveProposalExistsError,
    ActionCompensationCapExceededError,
    ActionCompensationDuplicateError,
    ActionCurrencyMismatchError,
    ActionError,
    ActionInvalidAmountError,
    ActionOrderNotFoundError,
    ActionRefundBalanceExceededError,
    ActionRunStatus,
    ActionStore,
    ActionType,
    ApprovalAdminRequiredError,
    ApprovalAlreadyDecidedError,
    ApprovalDecision,
    ApprovalDecisionType,
    ApprovalInvalidChangesError,
    ApprovalNotFoundError,
    ApprovalStatus,
    AuditActorType,
    ExecutionDataIntegrityError,
    NewProposalVersion,
    ProposalNotFoundError,
    ProposalStatus,
    RunNotFoundError,
    RunStateConflictError,
    ToolExecution,
    ToolExecutionStatus,
)
from app.actions.sqlite_store import (
    EVENT_APPROVAL_REQUESTED,
    EVENT_DECISION_RECORDED,
    EVENT_EXECUTION_CLAIMED,
    EVENT_EXECUTION_SUCCEEDED,
    EVENT_PROPOSAL_VERSION_CREATED,
    EVENT_RUN_CREATED,
    EVENT_RUN_SUCCEEDED,
    SQLiteActionStore,
)
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
    """动作存储测试所需的完整企业范围数据。"""

    store: ActionStore  # 被测的动作工作流存储
    database_path: Path  # 业务数据库文件路径
    org_a: Organization  # 企业 A
    org_b: Organization  # 企业 B，用于跨租户访问测试
    alice: User  # 企业 A 管理员，兼提案人
    bob: User  # 企业 A 的 agent 成员与企业 B 管理员
    carol: User  # 不属于企业 A 的用户，用于非成员决定人测试
    order_a: Order  # 企业 A 订单，总额 100 元
    order_b: Order  # 企业 B 订单


def build_action_scope(tmp_path) -> ActionScope:
    """构造用户、企业、客户与订单，并返回动作存储。"""
    database_path = tmp_path / "app.db"
    users = SQLiteUserStore(database_path)
    organizations = SQLiteOrganizationStore(database_path)
    customers = SQLiteCustomerStore(database_path)
    orders = SQLiteOrderStore(database_path)
    actions = SQLiteActionStore(database_path)

    alice = users.create_user(username="alice", password_hash="hash")
    bob = users.create_user(username="bob", password_hash="hash")
    carol = users.create_user(username="carol", password_hash="hash")
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
        carol=carol,
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


def make_idempotency_key(
    org_id: str,
    proposal_id: str,
    version_id: str,
    action_type_value: str,
):
    """按设计格式生成版本化幂等键。"""
    return (
        f"action-execution:v1:{org_id}:{proposal_id}:"
        f"{version_id}:{action_type_value}"
    )


def claim_and_run(
    store: ActionStore,
    *,
    organization_id: str,
    proposal_id: str,
    version_id: str,
    action_type: ActionType,
    idempotency_key: str | None = None,
) -> ToolExecution:
    """认领执行并把执行推进到 running 状态。"""
    proposal = store.get_proposal(
        organization_id=organization_id,
        proposal_id=proposal_id,
    )
    run = store.get_run(
        organization_id=organization_id,
        run_id=proposal.run_id,
    )
    if run.status is not ActionRunStatus.RUNNING:
        store.transition_run(
            organization_id=organization_id,
            run_id=run.run_id,
            expected_statuses=(run.status,),
            new_status=ActionRunStatus.RUNNING,
            error_code=None,
            error_retryable=False,
            actor_type=AuditActorType.SYSTEM,
            actor_user_id=None,
            event_type="run_started",
            details_json=json.dumps({}),
        )
    claim = store.claim_execution(
        organization_id=organization_id,
        proposal_id=proposal_id,
        proposal_version_id=version_id,
        action_type=action_type,
        idempotency_key=idempotency_key
        or make_idempotency_key(
            organization_id,
            proposal_id,
            version_id,
            action_type.value,
        ),
    )
    assert claim.acquired is True
    return store.mark_execution_running(
        organization_id=organization_id,
        execution_id=claim.execution_id,
    )


def complete_compensation(
    scope: ActionScope,
    creation,
    *,
    amount_cents: int,
    reason_code: str,
) -> None:
    """把一个已创建、已批准的补偿提案执行到成功终态。"""
    execution = claim_and_run(
        scope.store,
        organization_id=scope.org_a.organization_id,
        proposal_id=creation.proposal.proposal_id,
        version_id=creation.version.version_id,
        action_type=ActionType.COMPENSATION,
    )
    scope.store.record_execution_success(
        organization_id=scope.org_a.organization_id,
        execution_id=execution.execution_id,
        proposal_id=creation.proposal.proposal_id,
        proposal_version_id=creation.version.version_id,
        order_id=scope.order_a.order_id,
        action_type=ActionType.COMPENSATION,
        amount_cents=amount_cents,
        currency="CNY",
        reason_code=reason_code,
        business_record_id=f"record-comp-{uuid4()}",
        coupon_valid_days=30,
        mark_order_refunded=False,
    )


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


def count_rows(database_path: Path, table: str) -> int:
    """统计指定业务表的行数。"""
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    return int(row[0])


# —— 创建工作流 ——


def test_create_refund_workflow_persists_four_pieces(tmp_path):
    # 保护行为：合法退款提案应原子落库 Run、提案、版本 1 与审批请求，
    # Run 状态 queued、提案等待审批、thread_id 默认等于 Run ID。
    scope = build_action_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
    )

    run = scope.store.get_run(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    proposal = scope.store.get_proposal(
        organization_id=scope.org_a.organization_id,
        proposal_id=creation.proposal.proposal_id,
    )
    approval = scope.store.get_approval(
        organization_id=scope.org_a.organization_id,
        approval_id=creation.approval.approval_id,
    )
    assert run.status is ActionRunStatus.QUEUED
    assert run.thread_id == run.run_id
    assert run.proposal_id == proposal.proposal_id
    assert proposal.status is ProposalStatus.AWAITING_APPROVAL
    assert proposal.current_version_id == creation.version.version_id
    assert approval.status is ApprovalStatus.PENDING
    assert approval.requested_version_id == creation.version.version_id
    assert creation.version.version_no == 1
    assert creation.version.amount_cents == 6000
    assert creation.version.currency == "CNY"


def test_create_compensation_workflow(tmp_path):
    # 保护行为：合法补偿提案创建成功，版本与审批引用链完整。
    scope = build_action_scope(tmp_path)
    creation = create_compensation_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
    )

    version = scope.store.get_version(
        organization_id=scope.org_a.organization_id,
        version_id=creation.version.version_id,
    )
    approval = scope.store.get_approval(
        organization_id=scope.org_a.organization_id,
        approval_id=creation.approval.approval_id,
    )
    assert version.reason_code == "delayed_shipment"
    assert json.loads(version.parameters_json) == {"coupon_valid_days": 30}
    assert approval.proposal_id == creation.proposal.proposal_id


def test_create_workflow_rejects_unknown_order(tmp_path):
    # 边界情况：订单不存在时创建提案应抛出稳定订单错误，不产生任何业务行。
    scope = build_action_scope(tmp_path)
    with pytest.raises(ActionOrderNotFoundError):
        create_refund_workflow(
            scope.store,
            organization_id=scope.org_a.organization_id,
            user_id=scope.alice.user_id,
            order_id="order-does-not-exist",
        )
    assert count_rows(scope.database_path, "action_runs") == 0


def test_create_workflow_rejects_cross_tenant_order(tmp_path):
    # 保护行为：用企业 B 的订单在企业 A 创建提案，按不存在处理。
    scope = build_action_scope(tmp_path)
    with pytest.raises(ActionOrderNotFoundError):
        create_refund_workflow(
            scope.store,
            organization_id=scope.org_a.organization_id,
            user_id=scope.alice.user_id,
            order_id=scope.order_b.order_id,
        )


def test_create_workflow_rejects_currency_mismatch(tmp_path):
    # 边界情况：币种必须等于订单币种，传 USD 应被拒绝。
    scope = build_action_scope(tmp_path)
    with pytest.raises(ActionCurrencyMismatchError):
        scope.store.create_workflow(
            organization_id=scope.org_a.organization_id,
            conversation_id="conv-1",
            turn_id="turn-1",
            created_by_user_id=scope.alice.user_id,
            order_id=scope.order_a.order_id,
            action_type=ActionType.REFUND,
            amount_cents=6000,
            currency="USD",
            reason_code="quality_issue",
            reason_text="质量问题",
            parameters_json=json.dumps({"refund_scope": "partial"}),
        )


def test_create_workflow_rejects_non_positive_amount(tmp_path):
    # 边界情况：金额必须为正整数，0 与负数应被拒绝。
    scope = build_action_scope(tmp_path)
    with pytest.raises(ActionInvalidAmountError):
        create_refund_workflow(
            scope.store,
            organization_id=scope.org_a.organization_id,
            user_id=scope.alice.user_id,
            order_id=scope.order_a.order_id,
            amount_cents=0,
        )


def test_create_workflow_rejects_invalid_type_parameters(tmp_path):
    # 边界情况：全额退款金额必须等于余额；补偿原因和类型参数必须使用固定契约。
    scope = build_action_scope(tmp_path)
    with pytest.raises(ActionInvalidAmountError):
        create_refund_workflow(
            scope.store,
            organization_id=scope.org_a.organization_id,
            user_id=scope.alice.user_id,
            order_id=scope.order_a.order_id,
            amount_cents=6000,
            refund_scope="full",
        )
    with pytest.raises(ActionError):
        scope.store.create_workflow(
            organization_id=scope.org_a.organization_id,
            conversation_id="conv-invalid-compensation",
            turn_id="turn-invalid-compensation",
            created_by_user_id=scope.alice.user_id,
            order_id=scope.order_a.order_id,
            action_type=ActionType.COMPENSATION,
            amount_cents=1000,
            currency="CNY",
            reason_code="unknown_reason",
            reason_text="",
            parameters_json=json.dumps(
                {"coupon_valid_days": 10, "unexpected": True}
            ),
        )


def test_create_workflow_other_reason_requires_details(tmp_path):
    # 边界情况：退款或补偿使用 other 原因时必须提供非空详细说明。
    scope = build_action_scope(tmp_path)
    with pytest.raises(ActionError):
        create_refund_workflow(
            scope.store,
            organization_id=scope.org_a.organization_id,
            user_id=scope.alice.user_id,
            order_id=scope.order_a.order_id,
            reason_code="other",
            reason_text="   ",
        )


def test_create_workflow_rejects_refund_exceeding_balance(tmp_path):
    # 边界情况：退款金额超过订单可退余额时拒绝创建。
    scope = build_action_scope(tmp_path)
    with pytest.raises(ActionRefundBalanceExceededError):
        create_refund_workflow(
            scope.store,
            organization_id=scope.org_a.organization_id,
            user_id=scope.alice.user_id,
            order_id=scope.order_a.order_id,
            amount_cents=10001,
        )


def test_create_workflow_rejects_active_proposal_until_terminal(tmp_path):
    # 保护行为：同订单同动作类型只能有一个非终态提案；
    # 前一个进入终态后允许再次创建新的申请。
    scope = build_action_scope(tmp_path)
    first = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
        conversation_id="conv-1",
        turn_id="turn-1",
    )
    with pytest.raises(ActionActiveProposalExistsError):
        create_refund_workflow(
            scope.store,
            organization_id=scope.org_a.organization_id,
            user_id=scope.alice.user_id,
            order_id=scope.order_a.order_id,
            conversation_id="conv-2",
            turn_id="turn-2",
        )
    approve_decision(
        scope.store,
        organization_id=scope.org_a.organization_id,
        approval_id=first.approval.approval_id,
        user_id=scope.alice.user_id,
        decision=ApprovalDecisionType.REJECTED,
        comment="不符合退款条件",
    )
    second = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
        conversation_id="conv-3",
        turn_id="turn-3",
    )
    assert second.proposal.proposal_id != first.proposal.proposal_id


def test_create_workflow_rejects_same_turn_duplicate(tmp_path):
    # 边界情况：同一聊天回合重复创建同类型提案应被抑制，
    # 不产生第二套待审批记录。
    scope = build_action_scope(tmp_path)
    create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
        conversation_id="conv-1",
        turn_id="turn-1",
    )
    with pytest.raises(ActionActiveProposalExistsError):
        create_refund_workflow(
            scope.store,
            organization_id=scope.org_a.organization_id,
            user_id=scope.alice.user_id,
            order_id=scope.order_a.order_id,
            conversation_id="conv-1",
            turn_id="turn-1",
        )
    assert count_rows(scope.database_path, "approvals") == 1


def test_create_workflow_rejects_compensation_duplicate_reason(tmp_path):
    # 边界情况：同一订单同一补偿原因已有成功记录时，不允许再提补偿提案。
    scope = build_action_scope(tmp_path)
    first = create_compensation_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
        amount_cents=3000,
        reason_code="delayed_shipment",
    )
    approve_decision(
        scope.store,
        organization_id=scope.org_a.organization_id,
        approval_id=first.approval.approval_id,
        user_id=scope.alice.user_id,
    )
    complete_compensation(
        scope,
        first,
        amount_cents=3000,
        reason_code="delayed_shipment",
    )
    with pytest.raises(ActionCompensationDuplicateError):
        create_compensation_workflow(
            scope.store,
            organization_id=scope.org_a.organization_id,
            user_id=scope.alice.user_id,
            order_id=scope.order_a.order_id,
            amount_cents=2000,
            reason_code="delayed_shipment",
        )


def test_create_workflow_rejects_compensation_cap_exceeded(tmp_path):
    # 边界情况：补偿累计不得超过订单金额的 50%（向下取整），
    # 提案创建时即按成功记录校验累计上限。
    scope = build_action_scope(tmp_path)
    first = create_compensation_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
        amount_cents=4000,
        reason_code="transit_delay",
    )
    approve_decision(
        scope.store,
        organization_id=scope.org_a.organization_id,
        approval_id=first.approval.approval_id,
        user_id=scope.alice.user_id,
    )
    complete_compensation(
        scope,
        first,
        amount_cents=4000,
        reason_code="transit_delay",
    )
    # 已有成功补偿 4000 分，再提 2000 分累计超过 5000 分上限。
    with pytest.raises(ActionCompensationCapExceededError):
        create_compensation_workflow(
            scope.store,
            organization_id=scope.org_a.organization_id,
            user_id=scope.alice.user_id,
            order_id=scope.order_a.order_id,
            amount_cents=2000,
            reason_code="customer_dispute",
        )


# —— 查询与租户隔离 ——


def test_cross_tenant_queries_raise_not_found(tmp_path):
    # 保护行为：企业 B 用企业 A 的标识查询时，Run、提案与审批统一按不存在处理。
    scope = build_action_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
    )
    org_b = scope.org_b.organization_id
    with pytest.raises(RunNotFoundError):
        scope.store.get_run(
            organization_id=org_b,
            run_id=creation.run.run_id,
        )
    with pytest.raises(ProposalNotFoundError):
        scope.store.get_proposal(
            organization_id=org_b,
            proposal_id=creation.proposal.proposal_id,
        )
    with pytest.raises(ApprovalNotFoundError):
        scope.store.get_approval(
            organization_id=org_b,
            approval_id=creation.approval.approval_id,
        )


def test_list_approvals_pagination_and_filter(tmp_path):
    # 保护行为：审批列表按创建时间倒序分页，可按状态过滤，且只返回本企业数据。
    scope = build_action_scope(tmp_path)
    orders_store = SQLiteOrderStore(scope.database_path)
    customers_store = SQLiteCustomerStore(scope.database_path)
    customer = customers_store.create_customer(
        organization_id=scope.org_a.organization_id,
        customer_no="CUST-A-002",
        name="客户 A2",
    )
    order_ids = [scope.order_a.order_id]
    for index in range(2):
        order = orders_store.create_order(
            organization_id=scope.org_a.organization_id,
            order_no=f"ORD-A-{index + 2:03d}",
            customer_id=customer.customer_id,
            status=OrderStatus.PROCESSING,
            item_summary="测试商品",
            total_amount_cents=10000,
            currency="CNY",
            placed_at="2026-08-28T08:00:00+00:00",
        )
        order_ids.append(order.order_id)
    creations = []
    for index, order_id in enumerate(order_ids):
        creations.append(
            create_refund_workflow(
                scope.store,
                organization_id=scope.org_a.organization_id,
                user_id=scope.alice.user_id,
                order_id=order_id,
                conversation_id=f"conv-{index}",
                turn_id=f"turn-{index}",
            )
        )
    # 把中间一个审批批准，用于状态过滤验证。
    approve_decision(
        scope.store,
        organization_id=scope.org_a.organization_id,
        approval_id=creations[1].approval.approval_id,
        user_id=scope.alice.user_id,
    )
    first_page = scope.store.list_approvals(
        organization_id=scope.org_a.organization_id,
        status=None,
        limit=2,
        offset=0,
    )
    second_page = scope.store.list_approvals(
        organization_id=scope.org_a.organization_id,
        status=None,
        limit=2,
        offset=2,
    )
    assert len(first_page) == 2
    assert len(second_page) == 1
    first_ids = {approval.approval_id for approval in first_page}
    second_ids = {approval.approval_id for approval in second_page}
    assert not first_ids & second_ids
    pending = scope.store.list_approvals(
        organization_id=scope.org_a.organization_id,
        status=ApprovalStatus.PENDING,
        limit=10,
        offset=0,
    )
    assert [approval.approval_id for approval in pending] == [
        creations[2].approval.approval_id,
        creations[0].approval.approval_id,
    ]
    other_tenant = scope.store.list_approvals(
        organization_id=scope.org_b.organization_id,
        status=None,
        limit=10,
        offset=0,
    )
    assert other_tenant == []


# —— Run 状态转换 ——


def test_transition_run_to_awaiting_writes_audit(tmp_path):
    # 保护行为：Run 按 queued -> running -> awaiting_approval 转换，
    # 并在同一事务写入「进入等待」审计事件。
    scope = build_action_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
    )
    running = scope.store.transition_run(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
        expected_statuses=(ActionRunStatus.QUEUED,),
        new_status=ActionRunStatus.RUNNING,
        error_code=None,
        error_retryable=False,
        actor_type=AuditActorType.SYSTEM,
        actor_user_id=None,
        event_type="run_started",
        details_json=json.dumps({}),
    )
    updated = scope.store.transition_run(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
        expected_statuses=(running.status,),
        new_status=ActionRunStatus.AWAITING_APPROVAL,
        error_code=None,
        error_retryable=False,
        actor_type=AuditActorType.SYSTEM,
        actor_user_id=None,
        event_type="awaiting_approval",
        details_json=json.dumps({}),
    )
    assert updated.status is ActionRunStatus.AWAITING_APPROVAL
    logs = scope.store.list_audit_logs(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    assert any(log.event_type == "awaiting_approval" for log in logs)
    awaiting_log = next(
        log for log in logs if log.event_type == "awaiting_approval"
    )
    assert awaiting_log.proposal_id == creation.proposal.proposal_id


def test_transition_run_rejects_unexpected_status(tmp_path):
    # 边界情况：当前状态不在预期集合时抛出状态冲突，状态保持不变。
    scope = build_action_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
    )


def test_transition_run_rejects_illegal_edge(tmp_path):
    # 边界情况：即使调用者把 queued 放入预期集合，也不能跳过执行直接进入成功终态。
    scope = build_action_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
    )
    with pytest.raises(RunStateConflictError):
        scope.store.transition_run(
            organization_id=scope.org_a.organization_id,
            run_id=creation.run.run_id,
            expected_statuses=(ActionRunStatus.QUEUED,),
            new_status=ActionRunStatus.SUCCEEDED,
            error_code=None,
            error_retryable=False,
            actor_type=AuditActorType.SYSTEM,
            actor_user_id=None,
            event_type="run_succeeded",
            details_json=json.dumps({}),
        )
    with pytest.raises(RunStateConflictError):
        scope.store.transition_run(
            organization_id=scope.org_a.organization_id,
            run_id=creation.run.run_id,
            expected_statuses=(ActionRunStatus.RUNNING,),
            new_status=ActionRunStatus.AWAITING_APPROVAL,
            error_code=None,
            error_retryable=False,
            actor_type=AuditActorType.SYSTEM,
            actor_user_id=None,
            event_type="awaiting_approval",
            details_json=json.dumps({}),
        )
    assert (
        scope.store.get_run(
            organization_id=scope.org_a.organization_id,
            run_id=creation.run.run_id,
        ).status
        is ActionRunStatus.QUEUED
    )


def test_transition_run_terminal_blocks_further_transition(tmp_path):
    # 保护行为：succeeded / cancelled 是终态，进入后任何转换都被拒绝。
    scope = build_action_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
    )
    scope.store.transition_run(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
        expected_statuses=(ActionRunStatus.QUEUED,),
        new_status=ActionRunStatus.RUNNING,
        error_code=None,
        error_retryable=False,
        actor_type=AuditActorType.SYSTEM,
        actor_user_id=None,
        event_type="run_started",
        details_json=json.dumps({}),
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
        details_json=json.dumps({}),
    )
    run = scope.store.get_run(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    assert run.completed_at is not None
    with pytest.raises(RunStateConflictError):
        scope.store.transition_run(
            organization_id=scope.org_a.organization_id,
            run_id=creation.run.run_id,
            expected_statuses=(ActionRunStatus.CANCELLED,),
            new_status=ActionRunStatus.RUNNING,
            error_code=None,
            error_retryable=False,
            actor_type=AuditActorType.SYSTEM,
            actor_user_id=None,
            event_type="run_resumed",
            details_json=json.dumps({}),
        )


def test_transition_run_failed_retryable_is_resumable(tmp_path):
    # 保护行为：failed + 可重试不写完成时间，允许显式恢复回 running。
    scope = build_action_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
    )
    failed = scope.store.transition_run(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
        expected_statuses=(ActionRunStatus.QUEUED,),
        new_status=ActionRunStatus.FAILED,
        error_code="EXECUTION_RETRYABLE_FAILURE",
        error_retryable=True,
        actor_type=AuditActorType.SYSTEM,
        actor_user_id=None,
        event_type="run_failed",
        details_json=json.dumps({}),
    )
    assert failed.completed_at is None
    assert failed.last_error_code == "EXECUTION_RETRYABLE_FAILURE"
    resumed = scope.store.transition_run(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
        expected_statuses=(ActionRunStatus.FAILED,),
        new_status=ActionRunStatus.RUNNING,
        error_code=None,
        error_retryable=False,
        actor_type=AuditActorType.USER,
        actor_user_id=scope.alice.user_id,
        event_type="run_resumed",
        details_json=json.dumps({}),
    )
    assert resumed.status is ActionRunStatus.RUNNING
    assert resumed.last_error_code is None
    assert resumed.completed_at is None


def test_transition_run_failed_terminal_is_not_resumable(tmp_path):
    # 保护行为：failed + 不可重试是终态，写入完成时间后拒绝恢复。
    scope = build_action_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
    )
    failed = scope.store.transition_run(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
        expected_statuses=(ActionRunStatus.QUEUED,),
        new_status=ActionRunStatus.FAILED,
        error_code="EXECUTION_DATA_INTEGRITY_ERROR",
        error_retryable=False,
        actor_type=AuditActorType.SYSTEM,
        actor_user_id=None,
        event_type="run_failed",
        details_json=json.dumps({}),
    )
    assert failed.completed_at is not None
    with pytest.raises(RunStateConflictError):
        scope.store.transition_run(
            organization_id=scope.org_a.organization_id,
            run_id=creation.run.run_id,
            expected_statuses=(ActionRunStatus.FAILED,),
            new_status=ActionRunStatus.RUNNING,
            error_code=None,
            error_retryable=False,
            actor_type=AuditActorType.SYSTEM,
            actor_user_id=None,
            event_type="run_resumed",
            details_json=json.dumps({}),
        )


# —— 审批决定 ——


def test_decide_approval_approved(tmp_path):
    # 保护行为：批准决定更新审批与提案状态，决定版本为请求版本，
    # Run 保持等待图恢复的状态不变。
    scope = build_action_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
    )
    result = approve_decision(
        scope.store,
        organization_id=scope.org_a.organization_id,
        approval_id=creation.approval.approval_id,
        user_id=scope.alice.user_id,
    )
    assert result.approval.status is ApprovalStatus.APPROVED
    assert result.approval.decided_at is not None
    assert result.proposal.status is ProposalStatus.APPROVED
    assert result.decided_version.version_id == creation.version.version_id
    assert result.run.status is ActionRunStatus.QUEUED


def test_decide_approval_rejected(tmp_path):
    # 保护行为：拒绝决定使审批与提案进入 rejected，决定版本仍指向请求版本。
    scope = build_action_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
    )
    result = approve_decision(
        scope.store,
        organization_id=scope.org_a.organization_id,
        approval_id=creation.approval.approval_id,
        user_id=scope.alice.user_id,
        decision=ApprovalDecisionType.REJECTED,
        comment="不符合退款条件",
    )
    assert result.approval.status is ApprovalStatus.REJECTED
    assert result.proposal.status is ProposalStatus.REJECTED
    assert result.decided_version.version_id == creation.version.version_id
    assert result.decision.comment == "不符合退款条件"


def test_decide_approval_with_changes_creates_next_version(tmp_path):
    # 保护行为：修改后批准在同一事务创建新版本，提案当前版本前移，
    # 历史版本保持不可变并可完整审计。
    scope = build_action_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
    )
    result = approve_decision(
        scope.store,
        organization_id=scope.org_a.organization_id,
        approval_id=creation.approval.approval_id,
        user_id=scope.alice.user_id,
        decision=ApprovalDecisionType.APPROVED_WITH_CHANGES,
        comment="调整为部分退款",
        new_version=NewProposalVersion(
            amount_cents=4000,
            currency="CNY",
            reason_code="quality_issue",
            reason_text="仅对质量问题商品退款",
            parameters_json=json.dumps({"refund_scope": "partial"}),
        ),
    )
    assert result.approval.status is ApprovalStatus.APPROVED_WITH_CHANGES
    assert result.proposal.current_version_id == result.decided_version.version_id
    assert result.decided_version.version_no == 2
    assert result.decided_version.amount_cents == 4000
    versions = scope.store.list_versions(
        organization_id=scope.org_a.organization_id,
        proposal_id=creation.proposal.proposal_id,
    )
    assert [version.version_no for version in versions] == [1, 2]
    assert versions[0].amount_cents == 6000  # 原版本不被覆盖


def test_decide_approval_with_changes_requires_changes(tmp_path):
    # 边界情况：修改后批准必须至少改变一个允许字段，原样重复原版本被拒绝。
    scope = build_action_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
    )
    with pytest.raises(ApprovalInvalidChangesError):
        approve_decision(
            scope.store,
            organization_id=scope.org_a.organization_id,
            approval_id=creation.approval.approval_id,
            user_id=scope.alice.user_id,
            decision=ApprovalDecisionType.APPROVED_WITH_CHANGES,
            new_version=NewProposalVersion(
                amount_cents=6000,
                currency="CNY",
                reason_code="quality_issue",
                reason_text="商品存在质量问题",
                parameters_json=json.dumps({"refund_scope": "partial"}),
            ),
        )


def test_decide_approval_rejects_changes_with_plain_decision(tmp_path):
    # 边界情况：批准与拒绝不允许附带变更内容，携带时按非法变更拒绝。
    scope = build_action_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
    )
    with pytest.raises(ApprovalInvalidChangesError):
        approve_decision(
            scope.store,
            organization_id=scope.org_a.organization_id,
            approval_id=creation.approval.approval_id,
            user_id=scope.alice.user_id,
            new_version=NewProposalVersion(
                amount_cents=6000,
                currency="CNY",
                reason_code="quality_issue",
                reason_text="商品存在质量问题",
                parameters_json=json.dumps({"refund_scope": "partial"}),
            ),
        )
    with pytest.raises(ApprovalInvalidChangesError):
        scope.store.decide_approval(
            organization_id=scope.org_a.organization_id,
            approval_id=creation.approval.approval_id,
            decided_by_user_id=scope.alice.user_id,
            decision=ApprovalDecisionType.APPROVED_WITH_CHANGES,
            comment=None,
            new_version=None,
        )


def test_decide_approval_rejects_currency_change(tmp_path):
    # 边界情况：修改后批准不允许修改币种，即使其他字段合法。
    scope = build_action_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
    )
    with pytest.raises(ApprovalInvalidChangesError):
        approve_decision(
            scope.store,
            organization_id=scope.org_a.organization_id,
            approval_id=creation.approval.approval_id,
            user_id=scope.alice.user_id,
            decision=ApprovalDecisionType.APPROVED_WITH_CHANGES,
            new_version=NewProposalVersion(
                amount_cents=4000,
                currency="USD",
                reason_code="quality_issue",
                reason_text="质量问题",
                parameters_json=json.dumps({"refund_scope": "partial"}),
            ),
        )


def test_decide_approval_unknown_or_cross_tenant(tmp_path):
    # 保护行为：审批不存在或属于其他企业时，统一按不存在处理。
    scope = build_action_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
    )
    with pytest.raises(ApprovalNotFoundError):
        approve_decision(
            scope.store,
            organization_id=scope.org_a.organization_id,
            approval_id="approval-does-not-exist",
            user_id=scope.alice.user_id,
        )
    with pytest.raises(ApprovalNotFoundError):
        approve_decision(
            scope.store,
            organization_id=scope.org_b.organization_id,
            approval_id=creation.approval.approval_id,
            user_id=scope.bob.user_id,
        )


def test_decide_approval_requires_org_member(tmp_path):
    # 边界情况：决定人不是当前企业成员时，应抛出稳定权限错误且不落库。
    scope = build_action_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
    )
    with pytest.raises(ApprovalAdminRequiredError):
        approve_decision(
            scope.store,
            organization_id=scope.org_a.organization_id,
            approval_id=creation.approval.approval_id,
            user_id=scope.carol.user_id,
        )
    assert count_rows(scope.database_path, "approval_decisions") == 0


def test_decide_approval_same_content_returns_first_decision(tmp_path):
    # 保护行为：相同决定内容重复提交（即使决定人不同）返回首次决定，
    # 数据库中只有一条决定。
    scope = build_action_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
    )
    first = approve_decision(
        scope.store,
        organization_id=scope.org_a.organization_id,
        approval_id=creation.approval.approval_id,
        user_id=scope.alice.user_id,
    )
    second = approve_decision(
        scope.store,
        organization_id=scope.org_a.organization_id,
        approval_id=creation.approval.approval_id,
        user_id=scope.bob.user_id,
    )
    assert first.decision.decision_id == second.decision.decision_id
    assert second.decision.decided_by_user_id == scope.alice.user_id
    assert count_rows(scope.database_path, "approval_decisions") == 1


def test_decide_approval_same_changes_content_returns_first(tmp_path):
    # 保护行为：修改后批准以相同变更内容重复提交时，返回首次决定与首次版本。
    scope = build_action_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
    )
    changes = NewProposalVersion(
        amount_cents=4000,
        currency="CNY",
        reason_code="quality_issue",
        reason_text="仅退款 40 元",
        parameters_json=json.dumps({"refund_scope": "partial"}),
    )
    first = approve_decision(
        scope.store,
        organization_id=scope.org_a.organization_id,
        approval_id=creation.approval.approval_id,
        user_id=scope.alice.user_id,
        decision=ApprovalDecisionType.APPROVED_WITH_CHANGES,
        new_version=changes,
    )
    second = approve_decision(
        scope.store,
        organization_id=scope.org_a.organization_id,
        approval_id=creation.approval.approval_id,
        user_id=scope.alice.user_id,
        decision=ApprovalDecisionType.APPROVED_WITH_CHANGES,
        new_version=changes,
    )
    assert first.decision.decision_id == second.decision.decision_id
    versions = scope.store.list_versions(
        organization_id=scope.org_a.organization_id,
        proposal_id=creation.proposal.proposal_id,
    )
    assert len(versions) == 2  # 不重复创建版本


def test_decide_approval_different_content_conflicts(tmp_path):
    # 保护行为：已决定后提交不同决定返回冲突，且不覆盖原决定。
    scope = build_action_scope(tmp_path)
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
    with pytest.raises(ApprovalAlreadyDecidedError):
        approve_decision(
            scope.store,
            organization_id=scope.org_a.organization_id,
            approval_id=creation.approval.approval_id,
            user_id=scope.bob.user_id,
            decision=ApprovalDecisionType.REJECTED,
            comment="不同意",
        )
    decision = scope.store.get_decision(
        organization_id=scope.org_a.organization_id,
        approval_id=creation.approval.approval_id,
    )
    assert decision.decision is ApprovalDecisionType.APPROVED


def test_decide_approval_concurrent_same_content_single_decision(tmp_path):
    # 保护行为：两个管理员并发提交相同决定时，只有一个决定落库，
    # 双方都获得首次决定而不是 409。
    scope = build_action_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
    )

    def decide(user_id: str) -> ApprovalDecision:
        return approve_decision(
            scope.store,
            organization_id=scope.org_a.organization_id,
            approval_id=creation.approval.approval_id,
            user_id=user_id,
        ).decision

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(decide, scope.alice.user_id),
            pool.submit(decide, scope.bob.user_id),
        ]
        first, second = [future.result() for future in futures]
    assert first.decision_id == second.decision_id
    assert count_rows(scope.database_path, "approval_decisions") == 1


def test_decide_approval_revalidates_refund_balance_in_transaction(tmp_path):
    # 保护行为：审批等待期间余额变化后，批准必须在决定事务内拒绝超额退款。
    scope = build_action_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
        amount_cents=6000,
    )
    insert_phantom_refund(
        scope.database_path,
        org_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
        amount_cents=5000,
    )

    with pytest.raises(ActionRefundBalanceExceededError):
        approve_decision(
            scope.store,
            organization_id=scope.org_a.organization_id,
            approval_id=creation.approval.approval_id,
            user_id=scope.alice.user_id,
        )
    assert count_rows(scope.database_path, "approval_decisions") == 0


def test_decide_approval_rejects_inconsistent_full_refund_change(tmp_path):
    # 边界情况：修改后批准把范围改为 full 时，金额必须等于审批时全部可退余额。
    scope = build_action_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
        amount_cents=6000,
    )
    with pytest.raises(ApprovalInvalidChangesError):
        approve_decision(
            scope.store,
            organization_id=scope.org_a.organization_id,
            approval_id=creation.approval.approval_id,
            user_id=scope.alice.user_id,
            decision=ApprovalDecisionType.APPROVED_WITH_CHANGES,
            new_version=NewProposalVersion(
                amount_cents=6000,
                currency="CNY",
                reason_code="quality_issue",
                reason_text="改为全额退款",
                parameters_json=json.dumps({"refund_scope": "full"}),
            ),
        )


def test_decide_approval_revalidates_compensation_duplicate(tmp_path):
    # 保护行为：审批等待期间同原因已成功补偿时，决定事务必须拒绝再次批准。
    scope = build_action_scope(tmp_path)
    creation = create_compensation_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
        reason_code="transit_delay",
    )
    insert_phantom_compensation(
        scope.database_path,
        org_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
        amount_cents=1000,
        reason_code="transit_delay",
    )

    with pytest.raises(ActionCompensationDuplicateError):
        approve_decision(
            scope.store,
            organization_id=scope.org_a.organization_id,
            approval_id=creation.approval.approval_id,
            user_id=scope.alice.user_id,
        )


def test_decide_approval_concurrent_different_content_single_winner(tmp_path):
    # 保护行为：两个管理员并发提交不同决定时，恰好一个成功、
    # 另一个收到已决定冲突，数据库中只有一条决定。
    scope = build_action_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
    )

    def approve() -> ApprovalDecision:
        return approve_decision(
            scope.store,
            organization_id=scope.org_a.organization_id,
            approval_id=creation.approval.approval_id,
            user_id=scope.alice.user_id,
        ).decision

    def reject() -> ApprovalDecision:
        return approve_decision(
            scope.store,
            organization_id=scope.org_a.organization_id,
            approval_id=creation.approval.approval_id,
            user_id=scope.bob.user_id,
            decision=ApprovalDecisionType.REJECTED,
            comment="不符合",
        ).decision

    with ThreadPoolExecutor(max_workers=2) as pool:
        approve_future = pool.submit(approve)
        reject_future = pool.submit(reject)
        results = []
        for future in (approve_future, reject_future):
            try:
                results.append(("ok", future.result().decision_id))
            except ApprovalAlreadyDecidedError:
                results.append(("conflict", None))
    ok_count = sum(1 for kind, _ in results if kind == "ok")
    conflict_count = sum(1 for kind, _ in results if kind == "conflict")
    assert ok_count == 1
    assert conflict_count == 1
    assert count_rows(scope.database_path, "approval_decisions") == 1


# —— 幂等认领与执行状态 ——


def test_claim_execution_creates_claimed_record(tmp_path):
    # 保护行为：首次认领创建 claimed 执行记录，提案推进到 executing，
    # 并写入执行认领审计。
    scope = build_action_scope(tmp_path)
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
    execution = scope.store.claim_execution(
        organization_id=scope.org_a.organization_id,
        proposal_id=creation.proposal.proposal_id,
        proposal_version_id=creation.version.version_id,
        action_type=ActionType.REFUND,
        idempotency_key=make_idempotency_key(
            scope.org_a.organization_id,
            creation.proposal.proposal_id,
            creation.version.version_id,
            "refund",
        ),
    )
    assert execution.acquired is True
    assert execution.status is ToolExecutionStatus.CLAIMED
    assert execution.attempt_count == 1
    proposal = scope.store.get_proposal(
        organization_id=scope.org_a.organization_id,
        proposal_id=creation.proposal.proposal_id,
    )
    assert proposal.status is ProposalStatus.EXECUTING


def test_claim_execution_same_key_returns_same_record(tmp_path):
    # 保护行为：同一幂等键重复认领返回同一执行记录，数据库中只有一行。
    scope = build_action_scope(tmp_path)
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
    key = make_idempotency_key(
        scope.org_a.organization_id,
        creation.proposal.proposal_id,
        creation.version.version_id,
        "refund",
    )
    first = scope.store.claim_execution(
        organization_id=scope.org_a.organization_id,
        proposal_id=creation.proposal.proposal_id,
        proposal_version_id=creation.version.version_id,
        action_type=ActionType.REFUND,
        idempotency_key=key,
    )
    second = scope.store.claim_execution(
        organization_id=scope.org_a.organization_id,
        proposal_id=creation.proposal.proposal_id,
        proposal_version_id=creation.version.version_id,
        action_type=ActionType.REFUND,
        idempotency_key=key,
    )
    assert first.execution_id == second.execution_id
    assert first.acquired is True
    assert second.acquired is False
    assert count_rows(scope.database_path, "tool_executions") == 1


def test_concurrent_claim_grants_execution_right_once(tmp_path):
    # 保护行为：并发使用同一幂等键认领时只有一个结果 acquired=True。
    scope = build_action_scope(tmp_path)
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
    key = make_idempotency_key(
        scope.org_a.organization_id,
        creation.proposal.proposal_id,
        creation.version.version_id,
        "refund",
    )

    def claim():
        # 并发分支：返回值用于验证是否取得本次执行权。
        return scope.store.claim_execution(
            organization_id=scope.org_a.organization_id,
            proposal_id=creation.proposal.proposal_id,
            proposal_version_id=creation.version.version_id,
            action_type=ActionType.REFUND,
            idempotency_key=key,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = [future.result() for future in (pool.submit(claim), pool.submit(claim))]
    assert sum(claim.acquired for claim in claims) == 1
    assert len({claim.execution_id for claim in claims}) == 1


def test_claim_execution_rejects_mismatched_chain(tmp_path):
    # 边界情况：幂等键对应的执行记录与本次请求的提案/版本/类型不一致时，
    # 拒绝认领，防止同租户内拼接授权链。
    scope = build_action_scope(tmp_path)
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
    key = make_idempotency_key(
        scope.org_a.organization_id,
        creation.proposal.proposal_id,
        creation.version.version_id,
        "refund",
    )
    scope.store.claim_execution(
        organization_id=scope.org_a.organization_id,
        proposal_id=creation.proposal.proposal_id,
        proposal_version_id=creation.version.version_id,
        action_type=ActionType.REFUND,
        idempotency_key=key,
    )
    other = create_compensation_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
    )
    approve_decision(
        scope.store,
        organization_id=scope.org_a.organization_id,
        approval_id=other.approval.approval_id,
        user_id=scope.alice.user_id,
    )
    with pytest.raises(ExecutionDataIntegrityError):
        scope.store.claim_execution(
            organization_id=scope.org_a.organization_id,
            proposal_id=other.proposal.proposal_id,
            proposal_version_id=other.version.version_id,
            action_type=ActionType.REFUND,
            idempotency_key=key,
        )


def test_claim_execution_retryable_reclaim_increments_attempt(tmp_path):
    # 保护行为：可重试失败后使用原幂等键再次认领，尝试次数递增，
    # 错误信息清空并重置为 claimed。
    scope = build_action_scope(tmp_path)
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
    key = make_idempotency_key(
        scope.org_a.organization_id,
        creation.proposal.proposal_id,
        creation.version.version_id,
        "refund",
    )
    execution = scope.store.claim_execution(
        organization_id=scope.org_a.organization_id,
        proposal_id=creation.proposal.proposal_id,
        proposal_version_id=creation.version.version_id,
        action_type=ActionType.REFUND,
        idempotency_key=key,
    )
    scope.store.mark_execution_running(
        organization_id=scope.org_a.organization_id,
        execution_id=execution.execution_id,
    )
    failed = scope.store.record_execution_failure(
        organization_id=scope.org_a.organization_id,
        execution_id=execution.execution_id,
        error_code="EXECUTION_RETRYABLE_FAILURE",
        retryable=True,
    )
    assert failed.status is ToolExecutionStatus.FAILED_RETRYABLE
    reclaimed = scope.store.claim_execution(
        organization_id=scope.org_a.organization_id,
        proposal_id=creation.proposal.proposal_id,
        proposal_version_id=creation.version.version_id,
        action_type=ActionType.REFUND,
        idempotency_key=key,
    )
    assert reclaimed.execution_id == execution.execution_id
    assert reclaimed.acquired is True
    assert reclaimed.attempt_count == 2
    assert reclaimed.status is ToolExecutionStatus.CLAIMED
    assert reclaimed.error_code is None


def test_mark_execution_running_idempotent(tmp_path):
    # 保护行为：claimed 可推进到 running，重复标记保持 running 不报错。
    scope = build_action_scope(tmp_path)
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
    execution = scope.store.claim_execution(
        organization_id=scope.org_a.organization_id,
        proposal_id=creation.proposal.proposal_id,
        proposal_version_id=creation.version.version_id,
        action_type=ActionType.REFUND,
        idempotency_key=make_idempotency_key(
            scope.org_a.organization_id,
            creation.proposal.proposal_id,
            creation.version.version_id,
            "refund",
        ),
    )
    running = scope.store.mark_execution_running(
        organization_id=scope.org_a.organization_id,
        execution_id=execution.execution_id,
    )
    again = scope.store.mark_execution_running(
        organization_id=scope.org_a.organization_id,
        execution_id=execution.execution_id,
    )
    assert running.status is ToolExecutionStatus.RUNNING
    assert again.status is ToolExecutionStatus.RUNNING


def test_mark_execution_running_rejects_failed_execution(tmp_path):
    # 边界情况：失败执行必须先重新认领，直接标记 running 被拒绝。
    scope = build_action_scope(tmp_path)
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
    execution = scope.store.claim_execution(
        organization_id=scope.org_a.organization_id,
        proposal_id=creation.proposal.proposal_id,
        proposal_version_id=creation.version.version_id,
        action_type=ActionType.REFUND,
        idempotency_key=make_idempotency_key(
            scope.org_a.organization_id,
            creation.proposal.proposal_id,
            creation.version.version_id,
            "refund",
        ),
    )
    scope.store.mark_execution_running(
        organization_id=scope.org_a.organization_id,
        execution_id=execution.execution_id,
    )
    scope.store.record_execution_failure(
        organization_id=scope.org_a.organization_id,
        execution_id=execution.execution_id,
        error_code="EXECUTION_RETRYABLE_FAILURE",
        retryable=True,
    )
    with pytest.raises(RunStateConflictError):
        scope.store.mark_execution_running(
            organization_id=scope.org_a.organization_id,
            execution_id=execution.execution_id,
        )


def test_execution_result_requires_running_state(tmp_path):
    # 边界情况：claimed 执行不能跳过 running 直接记录成功或失败。
    scope = build_action_scope(tmp_path)
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
    claim = scope.store.claim_execution(
        organization_id=scope.org_a.organization_id,
        proposal_id=creation.proposal.proposal_id,
        proposal_version_id=creation.version.version_id,
        action_type=ActionType.REFUND,
        idempotency_key=make_idempotency_key(
            scope.org_a.organization_id,
            creation.proposal.proposal_id,
            creation.version.version_id,
            "refund",
        ),
    )
    with pytest.raises(RunStateConflictError):
        scope.store.record_execution_success(
            organization_id=scope.org_a.organization_id,
            execution_id=claim.execution_id,
            proposal_id=creation.proposal.proposal_id,
            proposal_version_id=creation.version.version_id,
            order_id=scope.order_a.order_id,
            action_type=ActionType.REFUND,
            amount_cents=6000,
            currency="CNY",
            reason_code="quality_issue",
            business_record_id="record-without-running",
            coupon_valid_days=None,
            mark_order_refunded=False,
        )
    with pytest.raises(RunStateConflictError):
        scope.store.record_execution_failure(
            organization_id=scope.org_a.organization_id,
            execution_id=claim.execution_id,
            error_code="EXECUTION_RETRYABLE_FAILURE",
            retryable=True,
        )


def test_get_execution_by_version_stable_read(tmp_path):
    # 保护行为：按版本读取执行记录，认领前为 None，认领后可读，
    # 成功后仍是同一稳定记录。
    scope = build_action_scope(tmp_path)
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
    assert (
        scope.store.get_execution_by_version(
            organization_id=scope.org_a.organization_id,
            proposal_version_id=creation.version.version_id,
        )
        is None
    )
    execution = scope.store.claim_execution(
        organization_id=scope.org_a.organization_id,
        proposal_id=creation.proposal.proposal_id,
        proposal_version_id=creation.version.version_id,
        action_type=ActionType.REFUND,
        idempotency_key=make_idempotency_key(
            scope.org_a.organization_id,
            creation.proposal.proposal_id,
            creation.version.version_id,
            "refund",
        ),
    )
    loaded = scope.store.get_execution_by_version(
        organization_id=scope.org_a.organization_id,
        proposal_version_id=creation.version.version_id,
    )
    assert loaded.execution_id == execution.execution_id
    assert (
        scope.store.get_execution_by_version(
            organization_id=scope.org_b.organization_id,
            proposal_version_id=creation.version.version_id,
        )
        is None
    )


# —— 执行成功与金额竞争 ——


def test_record_execution_success_partial_refund(tmp_path):
    # 保护行为：部分退款成功后写入一条模拟退款记录，执行、提案与 Run
    # 均进入 succeeded，订单主状态不改变。
    scope = build_action_scope(tmp_path)
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
    execution = claim_and_run(
        scope.store,
        organization_id=scope.org_a.organization_id,
        proposal_id=creation.proposal.proposal_id,
        version_id=creation.version.version_id,
        action_type=ActionType.REFUND,
    )
    result = scope.store.record_execution_success(
        organization_id=scope.org_a.organization_id,
        execution_id=execution.execution_id,
        proposal_id=creation.proposal.proposal_id,
        proposal_version_id=creation.version.version_id,
        order_id=scope.order_a.order_id,
        action_type=ActionType.REFUND,
        amount_cents=6000,
        currency="CNY",
        reason_code="quality_issue",
        business_record_id="record-1",
        coupon_valid_days=None,
        mark_order_refunded=False,
    )
    assert result.execution.status is ToolExecutionStatus.SUCCEEDED
    assert result.business_record_id == "record-1"
    assert result.order_marked_refunded is False
    assert count_rows(scope.database_path, "refund_records") == 1
    proposal = scope.store.get_proposal(
        organization_id=scope.org_a.organization_id,
        proposal_id=creation.proposal.proposal_id,
    )
    run = scope.store.get_run(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    assert proposal.status is ProposalStatus.SUCCEEDED
    assert run.status is ActionRunStatus.SUCCEEDED
    assert run.completed_at is not None
    with sqlite3.connect(scope.database_path) as connection:
        order_status = connection.execute(
            "SELECT status FROM orders WHERE id = ?",
            (scope.order_a.order_id,),
        ).fetchone()[0]
    assert order_status == "processing"


def test_record_execution_success_full_refund_marks_order_refunded(tmp_path):
    # 保护行为：全额退款使可退余额归零时，订单主状态置为 refunded。
    scope = build_action_scope(tmp_path)
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
    execution = claim_and_run(
        scope.store,
        organization_id=scope.org_a.organization_id,
        proposal_id=creation.proposal.proposal_id,
        version_id=creation.version.version_id,
        action_type=ActionType.REFUND,
    )
    result = scope.store.record_execution_success(
        organization_id=scope.org_a.organization_id,
        execution_id=execution.execution_id,
        proposal_id=creation.proposal.proposal_id,
        proposal_version_id=creation.version.version_id,
        order_id=scope.order_a.order_id,
        action_type=ActionType.REFUND,
        amount_cents=10000,
        currency="CNY",
        reason_code="quality_issue",
        business_record_id="record-full",
        coupon_valid_days=None,
        mark_order_refunded=True,
    )
    assert result.order_marked_refunded is True
    with sqlite3.connect(scope.database_path) as connection:
        order_status = connection.execute(
            "SELECT status FROM orders WHERE id = ?",
            (scope.order_a.order_id,),
        ).fetchone()[0]
    assert order_status == "refunded"


def test_record_execution_success_rejects_inconsistent_mark(tmp_path):
    # 边界情况：传入的 refunded 标记与事务内最新可退余额不一致时，
    # 拒绝写入，防止订单状态与余额事实脱节。
    scope = build_action_scope(tmp_path)
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
    execution = claim_and_run(
        scope.store,
        organization_id=scope.org_a.organization_id,
        proposal_id=creation.proposal.proposal_id,
        version_id=creation.version.version_id,
        action_type=ActionType.REFUND,
    )
    with pytest.raises(ExecutionDataIntegrityError):
        scope.store.record_execution_success(
            organization_id=scope.org_a.organization_id,
            execution_id=execution.execution_id,
            proposal_id=creation.proposal.proposal_id,
            proposal_version_id=creation.version.version_id,
            order_id=scope.order_a.order_id,
            action_type=ActionType.REFUND,
            amount_cents=6000,
            currency="CNY",
            reason_code="quality_issue",
            business_record_id="record-bad-mark",
            coupon_valid_days=None,
            mark_order_refunded=True,
        )
    assert count_rows(scope.database_path, "refund_records") == 0


def test_record_execution_success_replay_returns_same_result(tmp_path):
    # 保护行为：同一执行重复记录成功时返回首次业务结果，
    # 不重复写退款记录（节点重放安全）。
    scope = build_action_scope(tmp_path)
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
    execution = claim_and_run(
        scope.store,
        organization_id=scope.org_a.organization_id,
        proposal_id=creation.proposal.proposal_id,
        version_id=creation.version.version_id,
        action_type=ActionType.REFUND,
    )
    args = dict(
        organization_id=scope.org_a.organization_id,
        execution_id=execution.execution_id,
        proposal_id=creation.proposal.proposal_id,
        proposal_version_id=creation.version.version_id,
        order_id=scope.order_a.order_id,
        action_type=ActionType.REFUND,
        amount_cents=6000,
        currency="CNY",
        reason_code="quality_issue",
        coupon_valid_days=None,
        mark_order_refunded=False,
    )
    first = scope.store.record_execution_success(
        business_record_id="record-first",
        **args,
    )
    replay = scope.store.record_execution_success(
        business_record_id="record-second",
        **args,
    )
    assert first.business_record_id == replay.business_record_id == "record-first"
    assert count_rows(scope.database_path, "refund_records") == 1


def test_record_execution_success_revalidates_refund_balance(tmp_path):
    # 保护行为：批准期间其他路径写入退款后，执行事务内重验可退余额，
    # 超过余额时拒绝且不写任何业务结果。
    scope = build_action_scope(tmp_path)
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
    execution = claim_and_run(
        scope.store,
        organization_id=scope.org_a.organization_id,
        proposal_id=creation.proposal.proposal_id,
        version_id=creation.version.version_id,
        action_type=ActionType.REFUND,
    )
    # 模拟审批等待期间外部写入 7000 元退款，剩余可退余额仅 3000 元。
    insert_phantom_refund(
        scope.database_path,
        org_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
        amount_cents=7000,
    )
    with pytest.raises(ActionRefundBalanceExceededError):
        scope.store.record_execution_success(
            organization_id=scope.org_a.organization_id,
            execution_id=execution.execution_id,
            proposal_id=creation.proposal.proposal_id,
            proposal_version_id=creation.version.version_id,
            order_id=scope.order_a.order_id,
            action_type=ActionType.REFUND,
            amount_cents=4000,
            currency="CNY",
            reason_code="quality_issue",
            business_record_id="record-rejected",
            coupon_valid_days=None,
            mark_order_refunded=False,
        )
    assert count_rows(scope.database_path, "refund_records") == 1  # 只有外部那条
    assert (
        scope.store.get_execution(
            organization_id=scope.org_a.organization_id,
            execution_id=execution.execution_id,
        ).status
        is ToolExecutionStatus.RUNNING
    )


def test_record_execution_success_compensation(tmp_path):
    # 保护行为：补偿执行成功写入一条固定 30 天有效期的模拟补偿记录，
    # 订单状态不改变。
    scope = build_action_scope(tmp_path)
    creation = create_compensation_workflow(
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
    execution = claim_and_run(
        scope.store,
        organization_id=scope.org_a.organization_id,
        proposal_id=creation.proposal.proposal_id,
        version_id=creation.version.version_id,
        action_type=ActionType.COMPENSATION,
        idempotency_key=(
            "action-execution:v1:"
            f"{scope.org_a.organization_id}:{creation.proposal.proposal_id}:"
            f"{creation.version.version_id}:compensation"
        ),
    )
    result = scope.store.record_execution_success(
        organization_id=scope.org_a.organization_id,
        execution_id=execution.execution_id,
        proposal_id=creation.proposal.proposal_id,
        proposal_version_id=creation.version.version_id,
        order_id=scope.order_a.order_id,
        action_type=ActionType.COMPENSATION,
        amount_cents=3000,
        currency="CNY",
        reason_code="delayed_shipment",
        business_record_id="record-comp-1",
        coupon_valid_days=30,
        mark_order_refunded=False,
    )
    assert result.execution.status is ToolExecutionStatus.SUCCEEDED
    assert count_rows(scope.database_path, "compensation_records") == 1
    with sqlite3.connect(scope.database_path) as connection:
        row = connection.execute(
            "SELECT coupon_valid_days FROM compensation_records"
        ).fetchone()
    assert row[0] == 30


def test_record_execution_success_revalidates_compensation_duplicate(tmp_path):
    # 保护行为：批准期间同一订单同一原因已产生成功补偿时，
    # 执行事务内重验并拒绝重复补偿。
    scope = build_action_scope(tmp_path)
    creation = create_compensation_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
        reason_code="transit_delay",
    )
    approve_decision(
        scope.store,
        organization_id=scope.org_a.organization_id,
        approval_id=creation.approval.approval_id,
        user_id=scope.alice.user_id,
    )
    execution = claim_and_run(
        scope.store,
        organization_id=scope.org_a.organization_id,
        proposal_id=creation.proposal.proposal_id,
        version_id=creation.version.version_id,
        action_type=ActionType.COMPENSATION,
        idempotency_key=(
            "action-execution:v1:"
            f"{scope.org_a.organization_id}:{creation.proposal.proposal_id}:"
            f"{creation.version.version_id}:compensation"
        ),
    )
    insert_phantom_compensation(
        scope.database_path,
        org_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
        amount_cents=1000,
        reason_code="transit_delay",
    )
    with pytest.raises(ActionCompensationDuplicateError):
        scope.store.record_execution_success(
            organization_id=scope.org_a.organization_id,
            execution_id=execution.execution_id,
            proposal_id=creation.proposal.proposal_id,
            proposal_version_id=creation.version.version_id,
            order_id=scope.order_a.order_id,
            action_type=ActionType.COMPENSATION,
            amount_cents=3000,
            currency="CNY",
            reason_code="transit_delay",
            business_record_id="record-dup",
            coupon_valid_days=30,
            mark_order_refunded=False,
        )
    assert count_rows(scope.database_path, "compensation_records") == 1


def test_record_execution_success_revalidates_compensation_cap(tmp_path):
    # 保护行为：批准期间其他原因补偿累计后，执行事务内重验 50% 上限，
    # 超过上限时拒绝且不写结果。
    scope = build_action_scope(tmp_path)
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
    execution = claim_and_run(
        scope.store,
        organization_id=scope.org_a.organization_id,
        proposal_id=creation.proposal.proposal_id,
        version_id=creation.version.version_id,
        action_type=ActionType.COMPENSATION,
        idempotency_key=(
            "action-execution:v1:"
            f"{scope.org_a.organization_id}:{creation.proposal.proposal_id}:"
            f"{creation.version.version_id}:compensation"
        ),
    )
    insert_phantom_compensation(
        scope.database_path,
        org_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
        amount_cents=3000,
        reason_code="delayed_shipment",
    )
    with pytest.raises(ActionCompensationCapExceededError):
        scope.store.record_execution_success(
            organization_id=scope.org_a.organization_id,
            execution_id=execution.execution_id,
            proposal_id=creation.proposal.proposal_id,
            proposal_version_id=creation.version.version_id,
            order_id=scope.order_a.order_id,
            action_type=ActionType.COMPENSATION,
            amount_cents=3000,
            currency="CNY",
            reason_code="customer_dispute",
            business_record_id="record-cap",
            coupon_valid_days=30,
            mark_order_refunded=False,
        )
    assert count_rows(scope.database_path, "compensation_records") == 1


def test_record_execution_success_rejects_mismatched_amount(tmp_path):
    # 边界情况：执行金额与批准版本不一致时按数据完整性错误拒绝。
    scope = build_action_scope(tmp_path)
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
    execution = claim_and_run(
        scope.store,
        organization_id=scope.org_a.organization_id,
        proposal_id=creation.proposal.proposal_id,
        version_id=creation.version.version_id,
        action_type=ActionType.REFUND,
    )
    with pytest.raises(ExecutionDataIntegrityError):
        scope.store.record_execution_success(
            organization_id=scope.org_a.organization_id,
            execution_id=execution.execution_id,
            proposal_id=creation.proposal.proposal_id,
            proposal_version_id=creation.version.version_id,
            order_id=scope.order_a.order_id,
            action_type=ActionType.REFUND,
            amount_cents=9999,
            currency="CNY",
            reason_code="quality_issue",
            business_record_id="record-bad-amount",
            coupon_valid_days=None,
            mark_order_refunded=False,
        )


def test_record_execution_success_rejects_cross_chain_execution(tmp_path):
    # 保护行为：执行记录属于其他提案时，禁止用错误引用链写入业务结果。
    scope = build_action_scope(tmp_path)
    first = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
        conversation_id="conv-1",
        turn_id="turn-1",
    )
    approve_decision(
        scope.store,
        organization_id=scope.org_a.organization_id,
        approval_id=first.approval.approval_id,
        user_id=scope.alice.user_id,
    )
    second = create_compensation_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
    )
    approve_decision(
        scope.store,
        organization_id=scope.org_a.organization_id,
        approval_id=second.approval.approval_id,
        user_id=scope.alice.user_id,
    )
    execution = claim_and_run(
        scope.store,
        organization_id=scope.org_a.organization_id,
        proposal_id=first.proposal.proposal_id,
        version_id=first.version.version_id,
        action_type=ActionType.REFUND,
    )
    with pytest.raises(ExecutionDataIntegrityError):
        scope.store.record_execution_success(
            organization_id=scope.org_a.organization_id,
            execution_id=execution.execution_id,
            proposal_id=second.proposal.proposal_id,
            proposal_version_id=second.version.version_id,
            order_id=scope.order_a.order_id,
            action_type=ActionType.REFUND,
            amount_cents=6000,
            currency="CNY",
            reason_code="quality_issue",
            business_record_id="record-cross",
            coupon_valid_days=None,
            mark_order_refunded=False,
        )


def test_concurrent_execution_success_single_business_result(tmp_path):
    # 保护行为：两个线程并发记录同一执行成功（模拟重复恢复/节点重放），
    # 双方获得同一业务结果标识，业务库只有一条退款记录。
    scope = build_action_scope(tmp_path)
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
    execution = claim_and_run(
        scope.store,
        organization_id=scope.org_a.organization_id,
        proposal_id=creation.proposal.proposal_id,
        version_id=creation.version.version_id,
        action_type=ActionType.REFUND,
    )
    args = dict(
        organization_id=scope.org_a.organization_id,
        execution_id=execution.execution_id,
        proposal_id=creation.proposal.proposal_id,
        proposal_version_id=creation.version.version_id,
        order_id=scope.order_a.order_id,
        action_type=ActionType.REFUND,
        amount_cents=6000,
        currency="CNY",
        reason_code="quality_issue",
        coupon_valid_days=None,
        mark_order_refunded=False,
    )

    def succeed(record_id: str) -> str:
        return scope.store.record_execution_success(
            business_record_id=record_id,
            **args,
        ).business_record_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(succeed, "record-concurrent-a"),
            pool.submit(succeed, "record-concurrent-b"),
        ]
        first_record, second_record = [future.result() for future in futures]
    assert first_record == second_record
    assert count_rows(scope.database_path, "refund_records") == 1


# —— 执行失败 ——


def test_record_execution_failure_retryable_then_success(tmp_path):
    # 保护行为：可重试失败保留原幂等键，Run 进入可恢复失败；
    # 显式恢复后再次认领、执行成功，最终只有一条业务结果。
    scope = build_action_scope(tmp_path)
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
    key = make_idempotency_key(
        scope.org_a.organization_id,
        creation.proposal.proposal_id,
        creation.version.version_id,
        "refund",
    )
    execution = scope.store.claim_execution(
        organization_id=scope.org_a.organization_id,
        proposal_id=creation.proposal.proposal_id,
        proposal_version_id=creation.version.version_id,
        action_type=ActionType.REFUND,
        idempotency_key=key,
    )
    scope.store.mark_execution_running(
        organization_id=scope.org_a.organization_id,
        execution_id=execution.execution_id,
    )
    scope.store.record_execution_failure(
        organization_id=scope.org_a.organization_id,
        execution_id=execution.execution_id,
        error_code="EXECUTION_RETRYABLE_FAILURE",
        retryable=True,
    )
    failed_run = scope.store.get_run(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    assert failed_run.status is ActionRunStatus.FAILED
    assert failed_run.last_error_retryable is True
    assert failed_run.completed_at is None

    # 显式恢复：Run 回到 running 后用原幂等键重试。
    scope.store.transition_run(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
        expected_statuses=(ActionRunStatus.FAILED,),
        new_status=ActionRunStatus.RUNNING,
        error_code=None,
        error_retryable=False,
        actor_type=AuditActorType.USER,
        actor_user_id=scope.alice.user_id,
        event_type="run_resumed",
        details_json=json.dumps({}),
    )
    reclaimed = scope.store.claim_execution(
        organization_id=scope.org_a.organization_id,
        proposal_id=creation.proposal.proposal_id,
        proposal_version_id=creation.version.version_id,
        action_type=ActionType.REFUND,
        idempotency_key=key,
    )
    assert reclaimed.attempt_count == 2
    scope.store.mark_execution_running(
        organization_id=scope.org_a.organization_id,
        execution_id=execution.execution_id,
    )
    result = scope.store.record_execution_success(
        organization_id=scope.org_a.organization_id,
        execution_id=execution.execution_id,
        proposal_id=creation.proposal.proposal_id,
        proposal_version_id=creation.version.version_id,
        order_id=scope.order_a.order_id,
        action_type=ActionType.REFUND,
        amount_cents=6000,
        currency="CNY",
        reason_code="quality_issue",
        business_record_id="record-after-retry",
        coupon_valid_days=None,
        mark_order_refunded=False,
    )
    assert result.execution.status is ToolExecutionStatus.SUCCEEDED
    assert count_rows(scope.database_path, "refund_records") == 1


def test_record_execution_failure_terminal_blocks_reclaim(tmp_path):
    # 保护行为：终态失败写入完成时间，之后既不能重新认领也不能覆盖。
    scope = build_action_scope(tmp_path)
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
    execution = claim_and_run(
        scope.store,
        organization_id=scope.org_a.organization_id,
        proposal_id=creation.proposal.proposal_id,
        version_id=creation.version.version_id,
        action_type=ActionType.REFUND,
    )
    failed = scope.store.record_execution_failure(
        organization_id=scope.org_a.organization_id,
        execution_id=execution.execution_id,
        error_code="EXECUTION_DATA_INTEGRITY_ERROR",
        retryable=False,
    )
    assert failed.status is ToolExecutionStatus.FAILED_TERMINAL
    assert failed.completed_at is not None
    run = scope.store.get_run(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    assert run.status is ActionRunStatus.FAILED
    assert run.completed_at is not None
    with pytest.raises(RunStateConflictError):
        scope.store.claim_execution(
            organization_id=scope.org_a.organization_id,
            proposal_id=creation.proposal.proposal_id,
            proposal_version_id=creation.version.version_id,
            action_type=ActionType.REFUND,
            idempotency_key=make_idempotency_key(
                scope.org_a.organization_id,
                creation.proposal.proposal_id,
                creation.version.version_id,
                "refund",
            ),
        )


def test_record_execution_failure_preserves_succeeded(tmp_path):
    # 保护行为：已成功的执行绝不因后续失败记录被覆盖，原样返回成功状态。
    scope = build_action_scope(tmp_path)
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
    execution = claim_and_run(
        scope.store,
        organization_id=scope.org_a.organization_id,
        proposal_id=creation.proposal.proposal_id,
        version_id=creation.version.version_id,
        action_type=ActionType.REFUND,
    )
    scope.store.record_execution_success(
        organization_id=scope.org_a.organization_id,
        execution_id=execution.execution_id,
        proposal_id=creation.proposal.proposal_id,
        proposal_version_id=creation.version.version_id,
        order_id=scope.order_a.order_id,
        action_type=ActionType.REFUND,
        amount_cents=6000,
        currency="CNY",
        reason_code="quality_issue",
        business_record_id="record-final",
        coupon_valid_days=None,
        mark_order_refunded=False,
    )
    unchanged = scope.store.record_execution_failure(
        organization_id=scope.org_a.organization_id,
        execution_id=execution.execution_id,
        error_code="EXECUTION_RETRYABLE_FAILURE",
        retryable=True,
    )
    assert unchanged.status is ToolExecutionStatus.SUCCEEDED
    assert count_rows(scope.database_path, "refund_records") == 1


# —— 审计 ——


def test_create_workflow_writes_three_audit_events(tmp_path):
    # 保护行为：创建工作流时审计记录 Run 创建、提案版本创建与审批请求。
    scope = build_action_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
    )
    logs = scope.store.list_audit_logs(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    assert [log.event_type for log in logs] == [
        EVENT_RUN_CREATED,
        EVENT_PROPOSAL_VERSION_CREATED,
        EVENT_APPROVAL_REQUESTED,
    ]
    assert all(
        log.actor_user_id == scope.alice.user_id for log in logs
    )


def test_append_audit_log_and_list_order(tmp_path):
    # 保护行为：追加不伴随状态变化的审计事件（如显式恢复请求），
    # 按插入顺序可查询，且校验 Run 与提案归属。
    scope = build_action_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
    )
    appended = scope.store.append_audit_log(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
        proposal_id=creation.proposal.proposal_id,
        actor_type=AuditActorType.USER,
        actor_user_id=scope.alice.user_id,
        event_type="resume_requested",
        resource_type="action_run",
        resource_id=creation.run.run_id,
        details_json=json.dumps({"reason": "恢复中断的执行"}),
    )
    assert appended.event_type == "resume_requested"
    logs = scope.store.list_audit_logs(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    assert logs[-1].event_type == "resume_requested"
    with pytest.raises(RunNotFoundError):
        scope.store.append_audit_log(
            organization_id=scope.org_a.organization_id,
            run_id="run-does-not-exist",
            proposal_id=None,
            actor_type=AuditActorType.SYSTEM,
            actor_user_id=None,
            event_type="resume_requested",
            resource_type="action_run",
            resource_id="run-does-not-exist",
            details_json=json.dumps({}),
        )


def test_append_audit_log_rejects_mismatched_proposal(tmp_path):
    # 边界情况：审计记录的 Proposal 必须属于指定 Run，不能在同租户内串接。
    scope = build_action_scope(tmp_path)
    refund = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
    )
    compensation = create_compensation_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
    )

    with pytest.raises(ExecutionDataIntegrityError):
        scope.store.append_audit_log(
            organization_id=scope.org_a.organization_id,
            run_id=refund.run.run_id,
            proposal_id=compensation.proposal.proposal_id,
            actor_type=AuditActorType.SYSTEM,
            actor_user_id=None,
            event_type="resume_requested",
            resource_type="action_run",
            resource_id=refund.run.run_id,
            details_json=json.dumps({}),
        )


def test_full_lifecycle_audit_trail(tmp_path):
    # 保护行为：完整生命周期形成可查询审计链，覆盖设计 9.9 要求的最小事件集合。
    scope = build_action_scope(tmp_path)
    creation = create_refund_workflow(
        scope.store,
        organization_id=scope.org_a.organization_id,
        user_id=scope.alice.user_id,
        order_id=scope.order_a.order_id,
    )
    scope.store.transition_run(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
        expected_statuses=(ActionRunStatus.QUEUED,),
        new_status=ActionRunStatus.RUNNING,
        error_code=None,
        error_retryable=False,
        actor_type=AuditActorType.SYSTEM,
        actor_user_id=None,
        event_type="run_started",
        details_json=json.dumps({}),
    )
    scope.store.transition_run(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
        expected_statuses=(ActionRunStatus.RUNNING,),
        new_status=ActionRunStatus.AWAITING_APPROVAL,
        error_code=None,
        error_retryable=False,
        actor_type=AuditActorType.SYSTEM,
        actor_user_id=None,
        event_type="awaiting_approval",
        details_json=json.dumps({}),
    )
    approve_decision(
        scope.store,
        organization_id=scope.org_a.organization_id,
        approval_id=creation.approval.approval_id,
        user_id=scope.alice.user_id,
    )
    execution = claim_and_run(
        scope.store,
        organization_id=scope.org_a.organization_id,
        proposal_id=creation.proposal.proposal_id,
        version_id=creation.version.version_id,
        action_type=ActionType.REFUND,
    )
    scope.store.record_execution_success(
        organization_id=scope.org_a.organization_id,
        execution_id=execution.execution_id,
        proposal_id=creation.proposal.proposal_id,
        proposal_version_id=creation.version.version_id,
        order_id=scope.order_a.order_id,
        action_type=ActionType.REFUND,
        amount_cents=6000,
        currency="CNY",
        reason_code="quality_issue",
        business_record_id="record-lifecycle",
        coupon_valid_days=None,
        mark_order_refunded=False,
    )
    logs = scope.store.list_audit_logs(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    event_types = [log.event_type for log in logs]
    for expected in (
        EVENT_RUN_CREATED,
        EVENT_PROPOSAL_VERSION_CREATED,
        EVENT_APPROVAL_REQUESTED,
        "awaiting_approval",
        EVENT_DECISION_RECORDED,
        EVENT_EXECUTION_CLAIMED,
        EVENT_EXECUTION_SUCCEEDED,
        EVENT_RUN_SUCCEEDED,
    ):
        assert expected in event_types
