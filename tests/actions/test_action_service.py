"""退款/补偿提案与审批应用服务测试。

覆盖设计文档 Task 3 的验收点：创建提案、审批列表与详情、审批决定、
Run 状态查询与显式恢复、RBAC（admin/agent/非成员）、修改后批准字段
约束、决定后自动恢复失败不丢失决定，以及错误到 HTTP 状态码的映射。
"""

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest

from app.actions.base import (
    ActionActiveProposalExistsError,
    ActionCurrencyMismatchError,
    ActionError,
    ActionInvalidAmountError,
    ActionOrderNotFoundError,
    ActionRunStatus,
    ActionStore,
    ActionType,
    ApprovalAdminRequiredError,
    ApprovalAlreadyDecidedError,
    ApprovalDecisionType,
    ApprovalInvalidChangesError,
    ApprovalNotFoundError,
    ApprovalStatus,
    AuditActorType,
    CheckpointUnavailableError,
    ExecutionDataIntegrityError,
    ProposalStatus,
    RefundScope,
    RunNotFoundError,
    RunNotResumableError,
    ToolExecutionStatus,
)
from app.actions.service import (
    EVENT_RUN_RESUME_REQUESTED,
    ActionWorkflowService,
    ApprovalChanges,
    HTTP_STATUS_BY_ERROR_CODE,
    http_status_for_action_error,
)
from app.actions.sqlite_store import EVENT_DECISION_RECORDED, SQLiteActionStore
from app.application.organization_service import OrganizationService
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
    """应用服务测试所需的完整企业范围数据。"""

    store: ActionStore  # 被测的动作工作流存储
    database_path: Path  # 业务数据库文件路径
    org_a: Organization  # 企业 A
    org_b: Organization  # 企业 B，用于跨租户访问测试
    alice: User  # 企业 A 管理员，兼提案人
    bob: User  # 企业 A 的 agent 成员与企业 B 管理员
    carol: User  # 不属于企业 A 的用户，用于非成员决定人测试
    order_a: Order  # 企业 A 订单，总额 100 元
    order_a2: Order  # 企业 A 第二笔订单，总额 100 元（列表分页测试用）
    order_b: Order  # 企业 B 订单


class FakeWorkflowRunner:
    """测试用工作流运行器：记录调用，可注入启动/恢复失败。

    只模拟 Task 5 运行器的调用边界（start/resume 被正确调用），
    不模拟 LangGraph 的状态推进。
    """

    def __init__(
        self,
        *,
        start_error: ActionError | None = None,
        resume_error: ActionError | None = None,
    ):
        self.started: list[tuple[str, str]] = []  # 已启动的（企业, Run）标识
        self.resumed: list[tuple[str, str]] = []  # 已恢复的（企业, Run）标识
        self.start_error = start_error  # 首次启动注入的失败，可运行中修改
        self.resume_error = resume_error  # 恢复注入的失败，可运行中修改

    def start(self, *, organization_id: str, run_id: str) -> None:
        self.started.append((organization_id, run_id))
        if self.start_error is not None:
            raise self.start_error

    def resume(self, *, organization_id: str, run_id: str) -> None:
        self.resumed.append((organization_id, run_id))
        if self.resume_error is not None:
            raise self.resume_error


class StateChangingWorkflowRunner(FakeWorkflowRunner):
    """测试用状态推进运行器：用于验证应用服务返回工作流调用后的最新状态。"""

    def __init__(self, *, store: ActionStore, organization_id: str):
        super().__init__()
        self.store = store  # 被测动作存储
        self.organization_id = organization_id  # 状态推进所属企业

    def start(self, *, organization_id: str, run_id: str) -> None:
        super().start(organization_id=organization_id, run_id=run_id)
        advance_to_awaiting(
            self.store,
            organization_id=self.organization_id,
            run_id=run_id,
        )

    def resume(self, *, organization_id: str, run_id: str) -> None:
        super().resume(organization_id=organization_id, run_id=run_id)
        run = self.store.get_run(
            organization_id=self.organization_id,
            run_id=run_id,
        )
        if run.status is ActionRunStatus.AWAITING_APPROVAL:
            self.store.transition_run(
                organization_id=self.organization_id,
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
    order_a2 = orders.create_order(
        organization_id=org_a.organization_id,
        order_no="ORD-A-002",
        customer_id=customer_a.customer_id,
        status=OrderStatus.PROCESSING,
        item_summary="智能手环 x1",
        total_amount_cents=10000,
        currency="CNY",
        placed_at="2026-08-28T09:00:00+00:00",
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
        order_a2=order_a2,
        order_b=order_b,
    )


def build_service(tmp_path, runner: FakeWorkflowRunner | None = None):
    """构造应用服务及其测试范围数据。"""
    scope = build_scope(tmp_path)
    organization_service = OrganizationService(
        organization_store=SQLiteOrganizationStore(scope.database_path),
        user_store=SQLiteUserStore(scope.database_path),
    )
    service = ActionWorkflowService(
        store=scope.store,
        organization_service=organization_service,
        runner=runner,
    )
    return service, scope


def create_refund(
    service: ActionWorkflowService,
    scope: ActionScope,
    *,
    user_id: str | None = None,
    order_id: str | None = None,
    amount_cents: int = 6000,
    currency: str = "CNY",
    reason_code: str = "quality_issue",
    reason_text: str = "商品存在质量问题",
    refund_scope: RefundScope = RefundScope.PARTIAL,
    conversation_id: str = "conv-refund",
    turn_id: str = "turn-1",
):
    """通过应用服务创建退款提案。"""
    return service.create_proposal(
        organization_id=scope.org_a.organization_id,
        user_id=user_id or scope.alice.user_id,
        conversation_id=conversation_id,
        turn_id=turn_id,
        order_id=order_id or scope.order_a.order_id,
        action_type=ActionType.REFUND,
        amount_cents=amount_cents,
        currency=currency,
        reason_code=reason_code,
        reason_text=reason_text,
        refund_scope=refund_scope,
    )


def create_compensation(
    service: ActionWorkflowService,
    scope: ActionScope,
    *,
    user_id: str | None = None,
    order_id: str | None = None,
    amount_cents: int = 3000,
    reason_code: str = "delayed_shipment",
    reason_text: str = "发货延迟",
    conversation_id: str = "conv-compensation",
    turn_id: str = "turn-1",
):
    """通过应用服务创建补偿提案。"""
    return service.create_proposal(
        organization_id=scope.org_a.organization_id,
        user_id=user_id or scope.alice.user_id,
        conversation_id=conversation_id,
        turn_id=turn_id,
        order_id=order_id or scope.order_a.order_id,
        action_type=ActionType.COMPENSATION,
        amount_cents=amount_cents,
        currency="CNY",
        reason_code=reason_code,
        reason_text=reason_text,
        coupon_valid_days=30,
    )


def decide(
    service: ActionWorkflowService,
    scope: ActionScope,
    *,
    approval_id: str,
    user_id: str | None = None,
    decision: ApprovalDecisionType = ApprovalDecisionType.APPROVED,
    comment: str | None = "同意",
    changes: ApprovalChanges | None = None,
):
    """通过应用服务提交审批决定。"""
    return service.decide_approval(
        organization_id=scope.org_a.organization_id,
        approval_id=approval_id,
        decided_by_user_id=user_id or scope.alice.user_id,
        decision=decision,
        comment=comment,
        changes=changes,
    )


def advance_to_awaiting(store: ActionStore, *, organization_id: str, run_id: str):
    """模拟工作流推进：queued -> running -> awaiting_approval。"""
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


def complete_refund_execution(
    store: ActionStore,
    scope: ActionScope,
    *,
    creation,
    amount_cents: int,
    version_id: str | None = None,
):
    """把一个已创建的退款提案完整执行到成功终态（模拟执行器行为）。"""
    run = store.get_run(
        organization_id=scope.org_a.organization_id,
        run_id=creation.run.run_id,
    )
    if run.status is not ActionRunStatus.RUNNING:
        store.transition_run(
            organization_id=scope.org_a.organization_id,
            run_id=creation.run.run_id,
            expected_statuses=(ActionRunStatus.AWAITING_APPROVAL,),
            new_status=ActionRunStatus.RUNNING,
            error_code=None,
            error_retryable=False,
            actor_type=AuditActorType.SYSTEM,
            actor_user_id=None,
            event_type="run_started",
            details_json="{}",
        )
    version_id = version_id or creation.version.version_id
    idempotency_key = (
        f"action-execution:v1:{scope.org_a.organization_id}:"
        f"{creation.proposal.proposal_id}:{version_id}:refund"
    )
    claim = store.claim_execution(
        organization_id=scope.org_a.organization_id,
        proposal_id=creation.proposal.proposal_id,
        proposal_version_id=version_id,
        action_type=ActionType.REFUND,
        idempotency_key=idempotency_key,
    )
    assert claim.acquired is True
    execution = store.mark_execution_running(
        organization_id=scope.org_a.organization_id,
        execution_id=claim.execution_id,
    )
    return store.record_execution_success(
        organization_id=scope.org_a.organization_id,
        execution_id=execution.execution_id,
        proposal_id=creation.proposal.proposal_id,
        proposal_version_id=version_id,
        order_id=scope.order_a.order_id,
        action_type=ActionType.REFUND,
        amount_cents=amount_cents,
        currency="CNY",
        reason_code="quality_issue",
        business_record_id=f"record-{uuid4()}",
        coupon_valid_days=None,
        mark_order_refunded=False,
    )


def count_rows(database_path: Path, table: str, *, organization_id: str) -> int:
    """直接统计业务表中指定企业的行数（用于验证唯一性约束效果）。"""
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            f"SELECT COUNT(*) AS total FROM {table} WHERE organization_id = ?",
            (organization_id,),
        ).fetchone()
    return int(row["total"])


# —— 创建提案 ——


def test_create_refund_proposal_creates_awaiting_workflow(tmp_path):
    # 保护行为：合法退款参数应创建 Run、提案、版本 1 与待审批请求四件套，
    # 提案处于 awaiting_approval、审批处于 pending，版本参数与输入一致。
    service, scope = build_service(tmp_path)
    outcome = create_refund(service, scope)

    creation = outcome.creation
    assert creation.run.workflow_type is ActionType.REFUND
    assert creation.run.status is ActionRunStatus.QUEUED
    assert creation.run.conversation_id == "conv-refund"
    assert creation.run.turn_id == "turn-1"
    assert creation.proposal.status is ProposalStatus.AWAITING_APPROVAL
    assert creation.proposal.order_id == scope.order_a.order_id
    assert creation.version.version_no == 1
    assert creation.version.amount_cents == 6000
    assert json.loads(creation.version.parameters_json) == {
        "refund_scope": "partial"
    }
    assert creation.approval.status is ApprovalStatus.PENDING
    assert creation.approval.requested_version_id == creation.version.version_id
    assert outcome.start_ok is False
    assert outcome.start_error_code == "CHECKPOINT_UNAVAILABLE"


def test_create_compensation_proposal_creates_awaiting_workflow(tmp_path):
    # 保护行为：合法补偿参数应创建四件套，券有效期固定为 30 天。
    service, scope = build_service(tmp_path)
    outcome = create_compensation(service, scope)

    creation = outcome.creation
    assert creation.run.workflow_type is ActionType.COMPENSATION
    assert creation.proposal.status is ProposalStatus.AWAITING_APPROVAL
    assert json.loads(creation.version.parameters_json) == {
        "coupon_valid_days": 30
    }


def test_create_refund_requires_refund_scope(tmp_path):
    # 边界情况：退款提案缺少类型专属参数 refund_scope 时拒绝创建，
    # 且不产生任何业务记录。
    service, scope = build_service(tmp_path)
    with pytest.raises(ActionError) as exc_info:
        service.create_proposal(
            organization_id=scope.org_a.organization_id,
            user_id=scope.alice.user_id,
            conversation_id="conv-refund",
            turn_id="turn-1",
            order_id=scope.order_a.order_id,
            action_type=ActionType.REFUND,
            amount_cents=6000,
            currency="CNY",
            reason_code="quality_issue",
            reason_text="商品存在质量问题",
        )
    assert exc_info.value.code == "ACTION_ERROR"
    assert count_rows(scope.database_path, "action_runs", organization_id=scope.org_a.organization_id) == 0


def test_create_compensation_requires_coupon_valid_days_30(tmp_path):
    # 边界情况：补偿券有效期不是 30 天时拒绝创建，并给出稳定错误码。
    service, scope = build_service(tmp_path)
    with pytest.raises(ActionError) as exc_info:
        service.create_proposal(
            organization_id=scope.org_a.organization_id,
            user_id=scope.alice.user_id,
            conversation_id="conv-comp",
            turn_id="turn-1",
            order_id=scope.order_a.order_id,
            action_type=ActionType.COMPENSATION,
            amount_cents=3000,
            currency="CNY",
            reason_code="delayed_shipment",
            reason_text="发货延迟",
            coupon_valid_days=60,
        )
    assert exc_info.value.code == "ACTION_ERROR"


def test_create_rejects_invalid_amount_currency_reason(tmp_path):
    # 边界情况：非正金额、币种不匹配、未知原因码与 other 缺说明都应被拒绝，
    # 且分别给出对应稳定错误码。
    service, scope = build_service(tmp_path)
    with pytest.raises(ActionInvalidAmountError) as exc_info:
        create_refund(service, scope, amount_cents=0)
    assert exc_info.value.code == "ACTION_INVALID_AMOUNT"

    with pytest.raises(ActionCurrencyMismatchError) as exc_info:
        create_refund(
            service,
            scope,
            amount_cents=6000,
            currency="USD",
            conversation_id="conv-2",
        )
    assert exc_info.value.code == "ACTION_CURRENCY_MISMATCH"

    with pytest.raises(ActionError) as exc_info:
        create_refund(service, scope, reason_code="not_a_reason", conversation_id="conv-3")
    assert exc_info.value.code == "ACTION_ERROR"

    with pytest.raises(ActionError) as exc_info:
        create_refund(service, scope, reason_code="other", reason_text="  ", conversation_id="conv-4")
    assert exc_info.value.code == "ACTION_ERROR"


def test_create_rejects_duplicate_active_proposal(tmp_path):
    # 边界情况：同订单同动作类型已存在非终态提案时，第二次创建返回
    # ACTION_ACTIVE_PROPOSAL_EXISTS，且不会产生第二条提案。
    service, scope = build_service(tmp_path)
    create_refund(service, scope)
    with pytest.raises(ActionActiveProposalExistsError) as exc_info:
        create_refund(service, scope, conversation_id="conv-refund-2")
    assert exc_info.value.code == "ACTION_ACTIVE_PROPOSAL_EXISTS"
    assert count_rows(scope.database_path, "action_proposals", organization_id=scope.org_a.organization_id) == 1


def test_create_rejects_cross_tenant_order(tmp_path):
    # 边界情况：用其他企业的订单在当前企业创建提案应表现为订单不存在。
    service, scope = build_service(tmp_path)
    with pytest.raises(ActionOrderNotFoundError) as exc_info:
        create_refund(service, scope, order_id=scope.order_b.order_id)
    assert exc_info.value.code == "ACTION_ORDER_NOT_FOUND"


def test_create_starts_workflow_after_persist(tmp_path):
    # 保护行为：业务事务提交后应调用运行器首次启动，且只启动一次。
    runner = FakeWorkflowRunner()
    service, scope = build_service(tmp_path, runner=runner)
    outcome = create_refund(service, scope)
    assert outcome.start_ok is True
    assert outcome.start_error_code is None
    assert runner.started == [
        (scope.org_a.organization_id, outcome.creation.run.run_id)
    ]


def test_create_returns_latest_run_after_workflow_start(tmp_path):
    # 保护行为：首次启动把 Run 推进到等待审批后，
    # 创建结果必须返回最新状态，不能继续暴露事务创建时的 queued 快照。
    service, scope = build_service(tmp_path)
    runner = StateChangingWorkflowRunner(
        store=scope.store,
        organization_id=scope.org_a.organization_id,
    )
    service._runner = runner
    outcome = create_refund(service, scope)
    assert outcome.start_ok is True
    assert outcome.creation.run.status is ActionRunStatus.AWAITING_APPROVAL


def test_create_start_failure_keeps_business_facts(tmp_path):
    # 边界情况：首次图启动失败时，Run/提案/版本/审批事实仍然持久化，
    # 结果携带稳定错误码且不重复创建提案。
    runner = FakeWorkflowRunner(start_error=CheckpointUnavailableError("checkpoint 不可用"))
    service, scope = build_service(tmp_path, runner=runner)
    outcome = create_refund(service, scope)
    assert outcome.start_ok is False
    assert outcome.start_error_code == "CHECKPOINT_UNAVAILABLE"
    assert count_rows(scope.database_path, "action_runs", organization_id=scope.org_a.organization_id) == 1
    assert count_rows(scope.database_path, "action_proposals", organization_id=scope.org_a.organization_id) == 1
    # 同一回合不重复创建。
    with pytest.raises(ActionActiveProposalExistsError):
        create_refund(service, scope)


# —— 审批决定与 RBAC ——


def test_decide_by_agent_rejected(tmp_path):
    # 保护行为：agent 成员不能作出审批决定，返回 APPROVAL_ADMIN_REQUIRED。
    service, scope = build_service(tmp_path)
    outcome = create_refund(service, scope)
    with pytest.raises(ApprovalAdminRequiredError) as exc_info:
        decide(
            service,
            scope,
            approval_id=outcome.creation.approval.approval_id,
            user_id=scope.bob.user_id,
        )
    assert exc_info.value.code == "APPROVAL_ADMIN_REQUIRED"


def test_decide_by_outsider_maps_to_not_found(tmp_path):
    # 边界情况：非企业成员提交决定统一表现为审批不存在（404 语义）。
    service, scope = build_service(tmp_path)
    outcome = create_refund(service, scope)
    with pytest.raises(ApprovalNotFoundError) as exc_info:
        decide(
            service,
            scope,
            approval_id=outcome.creation.approval.approval_id,
            user_id=scope.carol.user_id,
        )
    assert exc_info.value.code == "APPROVAL_NOT_FOUND"


def test_decide_approved_records_decision_and_resumes(tmp_path):
    # 保护行为：admin 批准后决定落库、审批与提案状态更新，
    # 并自动调用运行器恢复等待中的 Run。
    runner = FakeWorkflowRunner()
    service, scope = build_service(tmp_path, runner=runner)
    outcome = create_refund(service, scope)
    approval_id = outcome.creation.approval.approval_id
    run_id = outcome.creation.run.run_id

    decision_outcome = decide(service, scope, approval_id=approval_id)
    assert decision_outcome.result.decision.decision == ApprovalDecisionType.APPROVED
    assert decision_outcome.result.approval.status is ApprovalStatus.APPROVED
    assert decision_outcome.result.proposal.status is ProposalStatus.APPROVED
    assert decision_outcome.result.decided_version.version_id == outcome.creation.version.version_id
    assert decision_outcome.resume_required is False
    assert decision_outcome.resume_error_code is None
    assert runner.resumed == [(scope.org_a.organization_id, run_id)]


def test_decide_returns_latest_run_after_workflow_resume(tmp_path):
    # 保护行为：审批自动恢复改变 Run 状态后，
    # 决定结果必须重新读取并返回恢复后的最新状态。
    service, scope = build_service(tmp_path)
    runner = StateChangingWorkflowRunner(
        store=scope.store,
        organization_id=scope.org_a.organization_id,
    )
    service._runner = runner
    outcome = create_refund(service, scope)
    decision_outcome = decide(
        service,
        scope,
        approval_id=outcome.creation.approval.approval_id,
    )
    assert decision_outcome.result.run.status is ActionRunStatus.RUNNING


def test_decide_self_approval_allowed_and_audited(tmp_path):
    # 保护行为：MVP 允许管理员自审；自审信息必须和审批决定
    # 在同一事务的 decision_recorded 审计事件中持久化。
    service, scope = build_service(tmp_path)
    outcome = create_refund(service, scope, user_id=scope.alice.user_id)
    decision_outcome = decide(
        service,
        scope,
        approval_id=outcome.creation.approval.approval_id,
        user_id=scope.alice.user_id,
    )
    assert decision_outcome.self_approved is True
    assert decision_outcome.resume_required is True
    assert decision_outcome.resume_error_code == "CHECKPOINT_UNAVAILABLE"
    logs = scope.store.list_audit_logs(
        organization_id=scope.org_a.organization_id,
        run_id=outcome.creation.run.run_id,
    )
    notes = [log for log in logs if log.event_type == EVENT_DECISION_RECORDED]
    assert len(notes) == 1
    details = json.loads(notes[0].details_json)
    assert details["self_approved"] is True
    assert details["proposer_user_id"] == scope.alice.user_id
    assert details["decider_user_id"] == scope.alice.user_id


def test_duplicate_same_decision_keeps_original_decider_and_single_audit(tmp_path):
    # 边界情况：第二位管理员幂等重试相同决定时，返回值与审计
    # 必须保留首次实际决定人，不能把重试者伪装成审批人。
    runner = FakeWorkflowRunner()
    service, scope = build_service(tmp_path, runner=runner)
    SQLiteOrganizationStore(scope.database_path).add_membership(
        organization_id=scope.org_a.organization_id,
        user_id=scope.carol.user_id,
        role=MembershipRole.ADMIN,
    )
    outcome = create_refund(service, scope)
    approval_id = outcome.creation.approval.approval_id
    first = decide(
        service,
        scope,
        approval_id=approval_id,
        user_id=scope.alice.user_id,
    )
    second = decide(
        service,
        scope,
        approval_id=approval_id,
        user_id=scope.carol.user_id,
    )
    assert first.result.created is True
    assert second.result.created is False
    assert second.result.decision.decision_id == first.result.decision.decision_id
    assert second.result.decision.decided_by_user_id == scope.alice.user_id
    assert second.self_approved is True
    assert len(runner.resumed) == 1
    logs = scope.store.list_audit_logs(
        organization_id=scope.org_a.organization_id,
        run_id=outcome.creation.run.run_id,
    )
    decision_logs = [
        log for log in logs if log.event_type == EVENT_DECISION_RECORDED
    ]
    assert len(decision_logs) == 1
    details = json.loads(decision_logs[0].details_json)
    assert details["decider_user_id"] == scope.alice.user_id
    assert details["self_approved"] is True


def test_decide_rejected_marks_proposal_rejected(tmp_path):
    # 保护行为：拒绝后决定落库、提案进入 rejected，且不产生执行记录。
    service, scope = build_service(tmp_path)
    outcome = create_refund(service, scope)
    decision_outcome = decide(
        service,
        scope,
        approval_id=outcome.creation.approval.approval_id,
        decision=ApprovalDecisionType.REJECTED,
        comment="理由不成立",
    )
    assert decision_outcome.result.decision.decision == ApprovalDecisionType.REJECTED
    assert decision_outcome.result.approval.status is ApprovalStatus.REJECTED
    assert decision_outcome.result.proposal.status is ProposalStatus.REJECTED
    assert count_rows(scope.database_path, "tool_executions", organization_id=scope.org_a.organization_id) == 0


def test_decide_approved_with_changes_creates_next_version(tmp_path):
    # 保护行为：修改后批准在同一事务内创建下一版本并更新当前版本指针，
    # 历史版本仍可审计且不可覆盖。
    service, scope = build_service(tmp_path)
    outcome = create_refund(
        service,
        scope,
        amount_cents=10000,
        refund_scope=RefundScope.FULL,
    )
    decision_outcome = decide(
        service,
        scope,
        approval_id=outcome.creation.approval.approval_id,
        decision=ApprovalDecisionType.APPROVED_WITH_CHANGES,
        comment="改为部分退款",
        changes=ApprovalChanges(
            amount_cents=6000,
            reason_code="quality_issue",
            reason_text="仅对存在质量问题的商品进行部分退款",
            refund_scope=RefundScope.PARTIAL,
        ),
    )
    result = decision_outcome.result
    assert result.decision.decision == ApprovalDecisionType.APPROVED_WITH_CHANGES
    assert result.approval.status is ApprovalStatus.APPROVED_WITH_CHANGES
    assert result.proposal.status is ProposalStatus.APPROVED
    assert result.decided_version.version_no == 2
    assert result.decided_version.amount_cents == 6000
    assert result.proposal.current_version_id == result.decided_version.version_id
    assert json.loads(result.decided_version.parameters_json) == {
        "refund_scope": "partial"
    }
    # 原版本 1 仍然存在且金额为全额 10000。
    versions = scope.store.list_versions(
        organization_id=scope.org_a.organization_id,
        proposal_id=outcome.creation.proposal.proposal_id,
    )
    assert [v.version_no for v in versions] == [1, 2]
    assert versions[0].amount_cents == 10000


def test_decide_changes_keep_currency_immutable(tmp_path):
    # 边界情况：修改后批准不允许更换币种，新版本币种必须沿用请求版本币种。
    service, scope = build_service(tmp_path)
    outcome = create_refund(service, scope)
    decision_outcome = decide(
        service,
        scope,
        approval_id=outcome.creation.approval.approval_id,
        decision=ApprovalDecisionType.APPROVED_WITH_CHANGES,
        changes=ApprovalChanges(amount_cents=4000),
    )
    assert decision_outcome.result.decided_version.currency == "CNY"
    assert decision_outcome.result.decided_version.currency == outcome.creation.version.currency


def test_decide_changes_reject_refund_scope_for_compensation(tmp_path):
    # 边界情况：补偿动作的修改不允许携带退款专属参数 refund_scope。
    service, scope = build_service(tmp_path)
    outcome = create_compensation(service, scope)
    with pytest.raises(ApprovalInvalidChangesError) as exc_info:
        decide(
            service,
            scope,
            approval_id=outcome.creation.approval.approval_id,
            decision=ApprovalDecisionType.APPROVED_WITH_CHANGES,
            changes=ApprovalChanges(refund_scope=RefundScope.FULL),
        )
    assert exc_info.value.code == "APPROVAL_INVALID_CHANGES"


def test_decide_changes_full_scope_amount_must_match_balance(tmp_path):
    # 边界情况：修改为 full 退款时金额必须等于审批时可退余额，
    # 不一致时整个决定回滚并返回 APPROVAL_INVALID_CHANGES。
    service, scope = build_service(tmp_path)
    outcome = create_refund(service, scope)
    with pytest.raises(ApprovalInvalidChangesError) as exc_info:
        decide(
            service,
            scope,
            approval_id=outcome.creation.approval.approval_id,
            decision=ApprovalDecisionType.APPROVED_WITH_CHANGES,
            changes=ApprovalChanges(
                amount_cents=3000,
                refund_scope=RefundScope.FULL,
            ),
        )
    assert exc_info.value.code == "APPROVAL_INVALID_CHANGES"
    assert count_rows(scope.database_path, "approval_decisions", organization_id=scope.org_a.organization_id) == 0
    assert count_rows(scope.database_path, "action_proposal_versions", organization_id=scope.org_a.organization_id) == 1


def test_decide_approved_rejects_changes(tmp_path):
    # 边界情况：普通批准不允许携带修改内容。
    service, scope = build_service(tmp_path)
    outcome = create_refund(service, scope)
    with pytest.raises(ApprovalInvalidChangesError) as exc_info:
        decide(
            service,
            scope,
            approval_id=outcome.creation.approval.approval_id,
            decision=ApprovalDecisionType.APPROVED,
            changes=ApprovalChanges(amount_cents=4000),
        )
    assert exc_info.value.code == "APPROVAL_INVALID_CHANGES"


def test_decide_changes_require_at_least_one_change(tmp_path):
    # 边界情况：修改后批准的内容与原版本完全一致时拒绝，
    # 禁止以「修改后批准」原样重复请求版本。
    service, scope = build_service(tmp_path)
    outcome = create_refund(service, scope)
    with pytest.raises(ApprovalInvalidChangesError) as exc_info:
        decide(
            service,
            scope,
            approval_id=outcome.creation.approval.approval_id,
            decision=ApprovalDecisionType.APPROVED_WITH_CHANGES,
            changes=ApprovalChanges(
                amount_cents=6000,
                reason_code="quality_issue",
                reason_text="商品存在质量问题",
                refund_scope=RefundScope.PARTIAL,
            ),
        )
    assert exc_info.value.code == "APPROVAL_INVALID_CHANGES"


def test_decide_duplicate_same_decision_returns_first(tmp_path):
    # 边界情况：相同审批重复提交完全相同的决定返回首次决定，
    # 数据库中仍只有一条决定、不产生新版本。
    service, scope = build_service(tmp_path)
    outcome = create_refund(service, scope)
    approval_id = outcome.creation.approval.approval_id
    first = decide(service, scope, approval_id=approval_id)
    second = decide(service, scope, approval_id=approval_id)
    assert second.result.decision.decision_id == first.result.decision.decision_id
    assert second.result.decision.comment == first.result.decision.comment
    assert count_rows(scope.database_path, "approval_decisions", organization_id=scope.org_a.organization_id) == 1
    assert count_rows(scope.database_path, "action_proposal_versions", organization_id=scope.org_a.organization_id) == 1


def test_decide_duplicate_different_decision_conflicts(tmp_path):
    # 边界情况：审批已被决定后提交不同决定返回 APPROVAL_ALREADY_DECIDED。
    service, scope = build_service(tmp_path)
    outcome = create_refund(service, scope)
    approval_id = outcome.creation.approval.approval_id
    decide(service, scope, approval_id=approval_id, decision=ApprovalDecisionType.APPROVED)
    with pytest.raises(ApprovalAlreadyDecidedError) as exc_info:
        decide(service, scope, approval_id=approval_id, decision=ApprovalDecisionType.REJECTED)
    assert exc_info.value.code == "APPROVAL_ALREADY_DECIDED"


def test_decide_resume_failure_keeps_decision(tmp_path):
    # 保护行为：决定落库后自动恢复失败时，决定仍然有效且结果标记
    # resume_required=true 与稳定错误码；重复提交相同决定仍返回首次决定，
    # 不需要管理员再次审批。
    runner = FakeWorkflowRunner(resume_error=CheckpointUnavailableError("checkpoint 不可用"))
    service, scope = build_service(tmp_path, runner=runner)
    outcome = create_refund(service, scope)
    approval_id = outcome.creation.approval.approval_id
    first = decide(service, scope, approval_id=approval_id)
    assert first.resume_required is True
    assert first.resume_error_code == "CHECKPOINT_UNAVAILABLE"
    assert first.result.decision.decision == ApprovalDecisionType.APPROVED
    second = decide(service, scope, approval_id=approval_id)
    assert second.result.decision.decision_id == first.result.decision.decision_id


# —— 列表、详情与状态查询 ——


def test_list_approvals_filters_status_and_paginates(tmp_path):
    # 保护行为：列表按创建时间倒序、按状态过滤并支持分页。
    service, scope = build_service(tmp_path)
    first = create_refund(service, scope, conversation_id="conv-1").creation
    second = create_refund(
        service,
        scope,
        order_id=scope.order_a2.order_id,
        conversation_id="conv-2",
    ).creation
    decide(service, scope, approval_id=first.approval.approval_id)

    pending = service.list_approvals(
        organization_id=scope.org_a.organization_id,
        status=ApprovalStatus.PENDING,
    )
    assert [item.approval.approval_id for item in pending] == [
        second.approval.approval_id
    ]
    decided = service.list_approvals(
        organization_id=scope.org_a.organization_id,
        status=ApprovalStatus.APPROVED,
    )
    assert [item.approval.approval_id for item in decided] == [
        first.approval.approval_id
    ]
    assert decided[0].run_status is ActionRunStatus.QUEUED
    assert decided[0].decision is not None
    # 分页：limit=1 只返回最新一条。
    page = service.list_approvals(
        organization_id=scope.org_a.organization_id,
        limit=1,
        offset=0,
    )
    assert len(page) == 1
    assert page[0].approval.approval_id == second.approval.approval_id


def test_approval_detail_contains_versions_and_decision(tmp_path):
    # 保护行为：详情包含请求版本、全部历史版本、Run 状态与决定摘要，
    # 修改后批准后当前版本指向新版本。
    service, scope = build_service(tmp_path)
    outcome = create_refund(service, scope)
    decide(
        service,
        scope,
        approval_id=outcome.creation.approval.approval_id,
        decision=ApprovalDecisionType.APPROVED_WITH_CHANGES,
        changes=ApprovalChanges(amount_cents=4000),
    )
    detail = service.get_approval_detail(
        organization_id=scope.org_a.organization_id,
        approval_id=outcome.creation.approval.approval_id,
    )
    assert detail.requested_version.version_id == outcome.creation.version.version_id
    assert [v.version_no for v in detail.versions] == [1, 2]
    assert detail.current_version.version_no == 2
    assert detail.decision.decision == ApprovalDecisionType.APPROVED_WITH_CHANGES
    assert detail.self_approved is True


def test_approval_detail_cross_tenant_not_found(tmp_path):
    # 边界情况：跨租户查询审批详情统一表现为审批不存在。
    service, scope = build_service(tmp_path)
    outcome = create_refund(service, scope)
    with pytest.raises(ApprovalNotFoundError) as exc_info:
        service.get_approval_detail(
            organization_id=scope.org_b.organization_id,
            approval_id=outcome.creation.approval.approval_id,
        )
    assert exc_info.value.code == "APPROVAL_NOT_FOUND"


def test_get_run_status_returns_full_view(tmp_path):
    # 保护行为：状态查询返回 Run、提案、审批、决定、当前版本、
    # 执行记录与解析后的业务结果摘要。
    service, scope = build_service(tmp_path)
    outcome = create_refund(service, scope)
    advance_to_awaiting(
        scope.store,
        organization_id=scope.org_a.organization_id,
        run_id=outcome.creation.run.run_id,
    )
    decide(
        service,
        scope,
        approval_id=outcome.creation.approval.approval_id,
    )
    complete_refund_execution(
        scope.store,
        scope,
        creation=outcome.creation,
        amount_cents=6000,
    )
    status = service.get_run_status(
        organization_id=scope.org_a.organization_id,
        run_id=outcome.creation.run.run_id,
    )
    assert status.run.status is ActionRunStatus.SUCCEEDED
    assert status.proposal.status is ProposalStatus.SUCCEEDED
    assert status.approval.status is ApprovalStatus.APPROVED
    assert status.decision.decision == ApprovalDecisionType.APPROVED
    assert status.current_version.version_id == outcome.creation.version.version_id
    assert status.execution.status is ToolExecutionStatus.SUCCEEDED
    assert status.result["business_record_id"]
    assert status.result["order_marked_refunded"] is False


def test_get_run_status_cross_tenant_not_found(tmp_path):
    # 边界情况：跨租户查询 Run 状态统一表现为 Run 不存在。
    service, scope = build_service(tmp_path)
    outcome = create_refund(service, scope)
    with pytest.raises(RunNotFoundError) as exc_info:
        service.get_run_status(
            organization_id=scope.org_b.organization_id,
            run_id=outcome.creation.run.run_id,
        )
    assert exc_info.value.code == "RUN_NOT_FOUND"


# —— 显式恢复 ——


def test_resume_run_requires_admin(tmp_path):
    # 保护行为：只有 admin 可以显式恢复 Run，agent 恢复返回
    # APPROVAL_ADMIN_REQUIRED。
    runner = FakeWorkflowRunner()
    service, scope = build_service(tmp_path, runner=runner)
    outcome = create_refund(service, scope)
    with pytest.raises(ApprovalAdminRequiredError) as exc_info:
        service.resume_run(
            organization_id=scope.org_a.organization_id,
            run_id=outcome.creation.run.run_id,
            requested_by_user_id=scope.bob.user_id,
        )
    assert exc_info.value.code == "APPROVAL_ADMIN_REQUIRED"
    assert runner.resumed == []


def test_resume_succeeded_run_returns_stable_result(tmp_path):
    # 保护行为：已成功 Run 的重复恢复请求幂等返回现有稳定结果，
    # 不再调用运行器，也不返回 RUN_NOT_RESUMABLE。
    runner = FakeWorkflowRunner()
    service, scope = build_service(tmp_path, runner=runner)
    outcome = create_refund(service, scope)
    advance_to_awaiting(
        scope.store,
        organization_id=scope.org_a.organization_id,
        run_id=outcome.creation.run.run_id,
    )
    decide(
        service,
        scope,
        approval_id=outcome.creation.approval.approval_id,
    )
    execution_success = complete_refund_execution(
        scope.store,
        scope,
        creation=outcome.creation,
        amount_cents=6000,
    )
    resume_outcome = service.resume_run(
        organization_id=scope.org_a.organization_id,
        run_id=outcome.creation.run.run_id,
        requested_by_user_id=scope.alice.user_id,
    )
    assert resume_outcome.run.status is ActionRunStatus.SUCCEEDED
    assert resume_outcome.resume_ok is True
    assert resume_outcome.error_code is None
    assert (
        resume_outcome.result["business_record_id"]
        == execution_success.business_record_id
    )
    # 只有决定落库时触发过一次自动恢复，成功重试不再执行图。
    assert runner.resumed == [
        (scope.org_a.organization_id, outcome.creation.run.run_id)
    ]


def test_resume_run_rejects_unapproved_waiting_run(tmp_path):
    # 边界情况：等待审批但没有持久化决定的 Run 不允许恢复（未批准分支）。
    runner = FakeWorkflowRunner()
    service, scope = build_service(tmp_path, runner=runner)
    outcome = create_refund(service, scope)
    advance_to_awaiting(
        scope.store,
        organization_id=scope.org_a.organization_id,
        run_id=outcome.creation.run.run_id,
    )
    with pytest.raises(RunNotResumableError) as exc_info:
        service.resume_run(
            organization_id=scope.org_a.organization_id,
            run_id=outcome.creation.run.run_id,
            requested_by_user_id=scope.alice.user_id,
        )
    assert exc_info.value.code == "RUN_NOT_RESUMABLE"
    assert runner.resumed == []


def test_resume_run_recovers_after_failed_auto_resume(tmp_path):
    # 保护行为：自动恢复失败后可显式恢复同一 Run；恢复请求写入审计，
    # 恢复成功时不再要求重复审批。
    runner = FakeWorkflowRunner(resume_error=CheckpointUnavailableError("checkpoint 不可用"))
    service, scope = build_service(tmp_path, runner=runner)
    outcome = create_refund(service, scope)
    advance_to_awaiting(
        scope.store,
        organization_id=scope.org_a.organization_id,
        run_id=outcome.creation.run.run_id,
    )
    decide(
        service,
        scope,
        approval_id=outcome.creation.approval.approval_id,
    )
    runner.resume_error = None
    resume_outcome = service.resume_run(
        organization_id=scope.org_a.organization_id,
        run_id=outcome.creation.run.run_id,
        requested_by_user_id=scope.alice.user_id,
    )
    assert resume_outcome.resume_ok is True
    assert resume_outcome.error_code is None
    logs = scope.store.list_audit_logs(
        organization_id=scope.org_a.organization_id,
        run_id=outcome.creation.run.run_id,
    )
    assert any(
        log.event_type == EVENT_RUN_RESUME_REQUESTED for log in logs
    )
    # 恢复失败的自动恢复也记录了调用。
    assert runner.resumed == [
        (scope.org_a.organization_id, outcome.creation.run.run_id),
        (scope.org_a.organization_id, outcome.creation.run.run_id),
    ]


def test_resume_run_returns_latest_status_after_runner_progress(tmp_path):
    # 保护行为：显式恢复成功推进 Run 后，返回值必须展示恢复后
    # 的最新状态，不能返回调用运行器之前的 awaiting_approval 快照。
    service, scope = build_service(tmp_path)
    runner = StateChangingWorkflowRunner(
        store=scope.store,
        organization_id=scope.org_a.organization_id,
    )
    service._runner = runner
    outcome = create_refund(service, scope)
    runner.resume_error = CheckpointUnavailableError("checkpoint 不可用")
    decide(
        service,
        scope,
        approval_id=outcome.creation.approval.approval_id,
    )
    runner.resume_error = None
    resume_outcome = service.resume_run(
        organization_id=scope.org_a.organization_id,
        run_id=outcome.creation.run.run_id,
        requested_by_user_id=scope.alice.user_id,
    )
    assert resume_outcome.resume_ok is True
    assert resume_outcome.run.status is ActionRunStatus.RUNNING
    assert resume_outcome.result is None


def test_resume_run_failure_returns_stable_code(tmp_path):
    # 边界情况：显式恢复遇到基础设施失败时返回稳定错误码，业务事实保留。
    runner = FakeWorkflowRunner(resume_error=CheckpointUnavailableError("checkpoint 不可用"))
    service, scope = build_service(tmp_path, runner=runner)
    outcome = create_refund(service, scope)
    advance_to_awaiting(
        scope.store,
        organization_id=scope.org_a.organization_id,
        run_id=outcome.creation.run.run_id,
    )
    decide(
        service,
        scope,
        approval_id=outcome.creation.approval.approval_id,
    )
    resume_outcome = service.resume_run(
        organization_id=scope.org_a.organization_id,
        run_id=outcome.creation.run.run_id,
        requested_by_user_id=scope.alice.user_id,
    )
    assert resume_outcome.resume_ok is False
    assert resume_outcome.error_code == "CHECKPOINT_UNAVAILABLE"
    # 决定仍然可查询，不需要重新审批。
    decision = scope.store.get_decision(
        organization_id=scope.org_a.organization_id,
        approval_id=outcome.creation.approval.approval_id,
    )
    assert decision is not None


def test_resume_run_cross_tenant_not_found(tmp_path):
    # 边界情况：跨租户显式恢复统一表现为 Run 不存在。
    runner = FakeWorkflowRunner()
    service, scope = build_service(tmp_path, runner=runner)
    outcome = create_refund(service, scope)
    with pytest.raises(RunNotFoundError) as exc_info:
        service.resume_run(
            organization_id=scope.org_b.organization_id,
            run_id=outcome.creation.run.run_id,
            requested_by_user_id=scope.bob.user_id,
        )
    assert exc_info.value.code == "RUN_NOT_FOUND"


# —— 错误映射 ——


def test_error_mapping_covers_design_status_codes():
    # 保护行为：设计 13.4 的主要错误码都能映射到建议 HTTP 状态码，
    # 未知错误码兜底为 500。
    assert http_status_for_action_error(ActionOrderNotFoundError("x")) == 404
    assert http_status_for_action_error(ActionInvalidAmountError("x")) == 422
    assert http_status_for_action_error(ActionCurrencyMismatchError("x")) == 422
    assert (
        http_status_for_action_error(ActionActiveProposalExistsError("x"))
        == 409
    )
    assert http_status_for_action_error(ApprovalNotFoundError("x")) == 404
    assert http_status_for_action_error(ApprovalAdminRequiredError("x")) == 403
    assert http_status_for_action_error(ApprovalAlreadyDecidedError("x")) == 409
    assert http_status_for_action_error(ApprovalInvalidChangesError("x")) == 422
    assert http_status_for_action_error(RunNotFoundError("x")) == 404
    assert http_status_for_action_error(RunNotResumableError("x")) == 409
    assert http_status_for_action_error(ExecutionDataIntegrityError("x")) == 500
    assert http_status_for_action_error(CheckpointUnavailableError("x")) == 503

    class UnknownError(ActionError):
        """未知错误码测试异常。"""

        code = "UNKNOWN_ERROR"

    assert http_status_for_action_error(UnknownError("x")) == 500
    assert "APPROVAL_INVALID_CHANGES" in HTTP_STATUS_BY_ERROR_CODE


# —— 完整服务流程 ——


def test_full_service_flow_create_list_detail_decide_status(tmp_path):
    # 保护行为：完整流程——创建提案、列表与详情查询、批准、
    # 执行成功后的状态查询，各环节业务事实一致。
    runner = FakeWorkflowRunner()
    service, scope = build_service(tmp_path, runner=runner)
    outcome = create_refund(service, scope)
    approval_id = outcome.creation.approval.approval_id
    run_id = outcome.creation.run.run_id

    pending = service.list_approvals(
        organization_id=scope.org_a.organization_id,
        status=ApprovalStatus.PENDING,
    )
    assert [item.approval.approval_id for item in pending] == [approval_id]
    assert pending[0].run_status is ActionRunStatus.QUEUED

    detail = service.get_approval_detail(
        organization_id=scope.org_a.organization_id,
        approval_id=approval_id,
    )
    assert detail.requested_version.amount_cents == 6000
    assert detail.decision is None

    decision_outcome = decide(
        service,
        scope,
        approval_id=approval_id,
        decision=ApprovalDecisionType.APPROVED_WITH_CHANGES,
        changes=ApprovalChanges(amount_cents=4000),
    )
    assert decision_outcome.result.decided_version.amount_cents == 4000

    advance_to_awaiting(
        scope.store,
        organization_id=scope.org_a.organization_id,
        run_id=run_id,
    )
    complete_refund_execution(
        scope.store,
        scope,
        creation=outcome.creation,
        amount_cents=4000,
        version_id=decision_outcome.result.decided_version.version_id,
    )
    status = service.get_run_status(
        organization_id=scope.org_a.organization_id,
        run_id=run_id,
    )
    assert status.run.status is ActionRunStatus.SUCCEEDED
    assert status.current_version.amount_cents == 4000
    assert status.execution.status is ToolExecutionStatus.SUCCEEDED
    assert status.result["order_marked_refunded"] is False
