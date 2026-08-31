"""退款/补偿审批工作流端到端集成测试。

把应用服务（Task 3）、幂等执行器（Task 4）与 LangGraph 运行器（Task 5）
按真实装配方式组合，覆盖设计 19 的验收场景 A-E：退款批准、修改后批准、
拒绝、重复恢复和重启恢复，并额外覆盖可重试失败显式恢复。场景 F 的 HTTP
跨租户攻击验收位于 ``tests/api/test_action_router.py``。所有暂停恢复验收都
使用真实临时 SQLite checkpointer，并在重启场景中重新创建运行时实例。
"""

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.actions.base import (
    ActionRunStatus,
    ActionStore,
    ActionType,
    ApprovalDecisionType,
    ProposalStatus,
    RefundScope,
    ToolExecutionStatus,
)
from app.actions.executor import RetryableFailureInjector
from app.actions.factory import build_action_executor
from app.actions.service import ActionWorkflowService, ApprovalChanges
from app.actions.sqlite_store import SQLiteActionStore
from app.application.organization_service import OrganizationService
from app.customers.sqlite_store import SQLiteCustomerStore
from app.orders.base import Order, OrderStatus
from app.orders.sqlite_store import SQLiteOrderStore
from app.organizations.base import Organization
from app.organizations.sqlite_store import SQLiteOrganizationStore
from app.users.base import User
from app.users.sqlite_store import SQLiteUserStore
from app.workflows.action_graph import build_action_graph
from app.workflows.checkpointer import create_sqlite_checkpointer
from app.workflows.runtime import LangGraphActionWorkflowRunner


@dataclass
class ActionStack:
    """完整动作工作流组件栈。"""

    store: ActionStore  # 动作工作流存储
    database_path: Path  # 业务数据库文件路径
    org_a: Organization  # 企业 A
    alice: User  # 企业 A 管理员，兼提案人
    order_a: Order  # 企业 A 订单，总额 100 元


def build_stack(tmp_path, *, fail_count: int = 0) -> ActionStack:
    """按生产装配方式构造 Store、执行器、图与运行器。"""
    database_path = tmp_path / "app.db"
    users = SQLiteUserStore(database_path)
    organizations = SQLiteOrganizationStore(database_path)
    customers = SQLiteCustomerStore(database_path)
    orders = SQLiteOrderStore(database_path)
    store = SQLiteActionStore(database_path)

    alice = users.create_user(username="alice", password_hash="hash")
    org_a = organizations.create_with_admin(
        name="企业 A",
        admin_user_id=alice.user_id,
    )
    customer_a = customers.create_customer(
        organization_id=org_a.organization_id,
        customer_no="CUST-A-001",
        name="客户 A",
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
    return ActionStack(
        store=store,
        database_path=database_path,
        org_a=org_a,
        alice=alice,
        order_a=order_a,
    )


def build_service(
    stack: ActionStack,
    tmp_path,
    *,
    fail_count: int = 0,
) -> ActionWorkflowService:
    """装配应用服务（含真实 LangGraph 运行器）。"""
    organization_service = OrganizationService(
        organization_store=SQLiteOrganizationStore(stack.database_path),
        user_store=SQLiteUserStore(stack.database_path),
    )
    executor = build_action_executor(
        stack.store,
        failure_injector=(
            RetryableFailureInjector(fail_count=fail_count)
            if fail_count
            else None
        ),
    )
    checkpointer = create_sqlite_checkpointer(tmp_path / "checkpoints.db")
    graph = build_action_graph(
        store=stack.store,
        executor=executor,
        checkpointer=checkpointer,
    )
    runner = LangGraphActionWorkflowRunner(
        store=stack.store,
        graph=graph,
    )
    return ActionWorkflowService(
        store=stack.store,
        organization_service=organization_service,
        runner=runner,
    )


def count_rows(database_path: Path, table: str) -> int:
    """统计指定业务表的行数。"""
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    return int(row[0])


def test_acceptance_scenario_a_refund_approval(tmp_path):
    # 验收场景 A：创建提案 -> 等待审批 -> 批准 -> 自动恢复执行成功，
    # 全程由真实 LangGraph 运行器推进，批准前无副作用且最终只有一条模拟退款记录。
    stack = build_stack(tmp_path)
    service = build_service(stack, tmp_path)
    creation = service.create_proposal(
        organization_id=stack.org_a.organization_id,
        user_id=stack.alice.user_id,
        conversation_id="conv-1",
        turn_id="turn-1",
        order_id=stack.order_a.order_id,
        action_type=ActionType.REFUND,
        amount_cents=6000,
        currency="CNY",
        reason_code="quality_issue",
        reason_text="商品存在质量问题",
        refund_scope=RefundScope.PARTIAL,
    )
    assert creation.start_ok is True
    assert creation.creation.run.status is ActionRunStatus.AWAITING_APPROVAL
    assert count_rows(stack.database_path, "refund_records") == 0

    decision = service.decide_approval(
        organization_id=stack.org_a.organization_id,
        approval_id=creation.creation.approval.approval_id,
        decided_by_user_id=stack.alice.user_id,
        decision=ApprovalDecisionType.APPROVED,
        comment="同意退款",
    )
    assert decision.resume_required is False
    assert decision.result.run.status is ActionRunStatus.SUCCEEDED
    assert count_rows(stack.database_path, "refund_records") == 1
    with sqlite3.connect(stack.database_path) as connection:
        row = connection.execute(
            "SELECT amount_cents FROM refund_records"
        ).fetchone()
    assert row[0] == 6000

    status = service.get_run_status(
        organization_id=stack.org_a.organization_id,
        run_id=creation.creation.run.run_id,
    )
    assert status.run.status is ActionRunStatus.SUCCEEDED
    assert status.execution.status is ToolExecutionStatus.SUCCEEDED
    assert status.result["business_record_id"] is not None


def test_acceptance_scenario_b_approved_with_changes(tmp_path):
    # 验收场景 B：原全额退款提案修改为部分退款后批准，必须保留原版本，
    # 执行严格使用新版本金额，部分退款不把订单主状态改为 refunded。
    stack = build_stack(tmp_path)
    service = build_service(stack, tmp_path)
    creation = service.create_proposal(
        organization_id=stack.org_a.organization_id,
        user_id=stack.alice.user_id,
        conversation_id="conv-b",
        turn_id="turn-b",
        order_id=stack.order_a.order_id,
        action_type=ActionType.REFUND,
        amount_cents=10000,
        currency="CNY",
        reason_code="quality_issue",
        reason_text="原申请全额退款",
        refund_scope=RefundScope.FULL,
    )

    outcome = service.decide_approval(
        organization_id=stack.org_a.organization_id,
        approval_id=creation.creation.approval.approval_id,
        decided_by_user_id=stack.alice.user_id,
        decision=ApprovalDecisionType.APPROVED_WITH_CHANGES,
        changes=ApprovalChanges(
            amount_cents=6000,
            reason_text="仅对质量问题商品部分退款",
            refund_scope=RefundScope.PARTIAL,
        ),
        comment="调整为部分退款",
    )

    assert outcome.result.run.status is ActionRunStatus.SUCCEEDED
    detail = service.get_approval_detail(
        organization_id=stack.org_a.organization_id,
        approval_id=creation.creation.approval.approval_id,
    )
    assert [version.version_no for version in detail.versions] == [1, 2]
    assert detail.versions[0].amount_cents == 10000
    assert json.loads(detail.versions[0].parameters_json) == {
        "refund_scope": "full"
    }
    assert detail.current_version.amount_cents == 6000
    assert json.loads(detail.current_version.parameters_json) == {
        "refund_scope": "partial"
    }
    assert detail.decision.decided_version_id == (
        detail.current_version.version_id
    )
    with sqlite3.connect(stack.database_path) as connection:
        refund = connection.execute(
            "SELECT amount_cents FROM refund_records"
        ).fetchone()
    assert refund[0] == 6000
    order = SQLiteOrderStore(stack.database_path).get_by_id(
        organization_id=stack.org_a.organization_id,
        order_id=stack.order_a.order_id,
    )
    assert order.status is OrderStatus.PROCESSING


def test_acceptance_scenario_c_rejected(tmp_path):
    # 验收场景 C：拒绝决定恢复后 Run 进入 cancelled 终态，
    # 不创建执行与业务结果，查询接口可解释拒绝原因。
    stack = build_stack(tmp_path)
    service = build_service(stack, tmp_path)
    creation = service.create_proposal(
        organization_id=stack.org_a.organization_id,
        user_id=stack.alice.user_id,
        conversation_id="conv-1",
        turn_id="turn-1",
        order_id=stack.order_a.order_id,
        action_type=ActionType.REFUND,
        amount_cents=6000,
        currency="CNY",
        reason_code="quality_issue",
        reason_text="商品存在质量问题",
        refund_scope=RefundScope.PARTIAL,
    )
    decision = service.decide_approval(
        organization_id=stack.org_a.organization_id,
        approval_id=creation.creation.approval.approval_id,
        decided_by_user_id=stack.alice.user_id,
        decision=ApprovalDecisionType.REJECTED,
        comment="不满足退换政策",
    )
    assert decision.result.run.status is ActionRunStatus.CANCELLED
    assert decision.result.proposal.status is ProposalStatus.REJECTED
    assert count_rows(stack.database_path, "tool_executions") == 0
    assert count_rows(stack.database_path, "refund_records") == 0
    detail = service.get_approval_detail(
        organization_id=stack.org_a.organization_id,
        approval_id=creation.creation.approval.approval_id,
    )
    assert detail.decision.comment == "不满足退换政策"


def test_acceptance_scenario_d_repeated_resume_is_idempotent(tmp_path):
    # 验收场景 D：同一批准 Run 重复恢复时返回相同 Execution ID 和业务结果 ID，
    # 数据库中仍只有一条执行记录和一条模拟退款记录。
    stack = build_stack(tmp_path)
    service = build_service(stack, tmp_path)
    creation = service.create_proposal(
        organization_id=stack.org_a.organization_id,
        user_id=stack.alice.user_id,
        conversation_id="conv-d",
        turn_id="turn-d",
        order_id=stack.order_a.order_id,
        action_type=ActionType.REFUND,
        amount_cents=6000,
        currency="CNY",
        reason_code="quality_issue",
        reason_text="商品存在质量问题",
        refund_scope=RefundScope.PARTIAL,
    )
    service.decide_approval(
        organization_id=stack.org_a.organization_id,
        approval_id=creation.creation.approval.approval_id,
        decided_by_user_id=stack.alice.user_id,
        decision=ApprovalDecisionType.APPROVED,
    )
    first_status = service.get_run_status(
        organization_id=stack.org_a.organization_id,
        run_id=creation.creation.run.run_id,
    )

    first_resume = service.resume_run(
        organization_id=stack.org_a.organization_id,
        run_id=creation.creation.run.run_id,
        requested_by_user_id=stack.alice.user_id,
    )
    second_resume = service.resume_run(
        organization_id=stack.org_a.organization_id,
        run_id=creation.creation.run.run_id,
        requested_by_user_id=stack.alice.user_id,
    )
    final_status = service.get_run_status(
        organization_id=stack.org_a.organization_id,
        run_id=creation.creation.run.run_id,
    )

    assert first_resume.result == second_resume.result == first_status.result
    assert final_status.execution.execution_id == (
        first_status.execution.execution_id
    )
    assert final_status.result["business_record_id"] == (
        first_status.result["business_record_id"]
    )
    assert count_rows(stack.database_path, "tool_executions") == 1
    assert count_rows(stack.database_path, "refund_records") == 1


def test_acceptance_scenario_e_restart_recovery(tmp_path):
    # 验收场景 E：Run 在审批节点暂停后关闭并重新创建所有组件，
    # 使用原 thread_id 恢复并正常进入终态（设计 19 场景 E）。
    stack = build_stack(tmp_path)
    service = build_service(stack, tmp_path)
    creation = service.create_proposal(
        organization_id=stack.org_a.organization_id,
        user_id=stack.alice.user_id,
        conversation_id="conv-1",
        turn_id="turn-1",
        order_id=stack.order_a.order_id,
        action_type=ActionType.REFUND,
        amount_cents=6000,
        currency="CNY",
        reason_code="quality_issue",
        reason_text="商品存在质量问题",
        refund_scope=RefundScope.PARTIAL,
    )
    assert creation.creation.run.status is ActionRunStatus.AWAITING_APPROVAL
    original_thread_id = creation.creation.run.thread_id

    # 重新装配所有组件（同一业务库与 checkpoint 文件）。
    service2 = build_service(stack, tmp_path)
    decision = service2.decide_approval(
        organization_id=stack.org_a.organization_id,
        approval_id=creation.creation.approval.approval_id,
        decided_by_user_id=stack.alice.user_id,
        decision=ApprovalDecisionType.APPROVED,
        comment="同意退款",
    )
    assert decision.resume_required is False
    assert decision.result.run.status is ActionRunStatus.SUCCEEDED
    assert decision.result.run.thread_id == original_thread_id
    assert count_rows(stack.database_path, "refund_records") == 1


def test_retryable_failure_explicit_resume_with_real_runtime(tmp_path):
    # 保护行为：模拟注入的临时故障后自动恢复失败，Run 进入可恢复失败；
    # 管理员显式恢复后沿用原幂等键执行成功，只有一条业务结果。
    stack = build_stack(tmp_path)
    service = build_service(stack, tmp_path, fail_count=1)
    creation = service.create_proposal(
        organization_id=stack.org_a.organization_id,
        user_id=stack.alice.user_id,
        conversation_id="conv-1",
        turn_id="turn-1",
        order_id=stack.order_a.order_id,
        action_type=ActionType.REFUND,
        amount_cents=6000,
        currency="CNY",
        reason_code="quality_issue",
        reason_text="商品存在质量问题",
        refund_scope=RefundScope.PARTIAL,
    )
    decision = service.decide_approval(
        organization_id=stack.org_a.organization_id,
        approval_id=creation.creation.approval.approval_id,
        decided_by_user_id=stack.alice.user_id,
        decision=ApprovalDecisionType.APPROVED,
        comment="同意退款",
    )
    # 图调用本身完成（执行失败由执行器落库），但 Run 进入可恢复失败。
    assert decision.resume_required is False
    assert decision.result.run.status is ActionRunStatus.FAILED
    assert decision.result.run.last_error_retryable is True
    run = stack.store.get_run(
        organization_id=stack.org_a.organization_id,
        run_id=creation.creation.run.run_id,
    )
    assert run.status is ActionRunStatus.FAILED
    assert run.last_error_retryable is True

    # 管理员显式恢复：沿用原执行记录与幂等键。
    resume_outcome = service.resume_run(
        organization_id=stack.org_a.organization_id,
        run_id=creation.creation.run.run_id,
        requested_by_user_id=stack.alice.user_id,
    )
    assert resume_outcome.resume_ok is True
    assert resume_outcome.run.status is ActionRunStatus.SUCCEEDED
    execution = stack.store.get_execution_by_version(
        organization_id=stack.org_a.organization_id,
        proposal_version_id=creation.creation.version.version_id,
    )
    assert execution.status is ToolExecutionStatus.SUCCEEDED
    assert execution.attempt_count == 2
    assert count_rows(stack.database_path, "refund_records") == 1
