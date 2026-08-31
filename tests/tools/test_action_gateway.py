"""Action Tool Gateway 测试（设计 12.2 / 12.3 / 12.4 / 12.6）。

覆盖：提案工具把会话与回合标识从 AgentInvocationContext 传入应用服务；
订单解析与稳定错误映射；只读状态工具；绑定上下文类型约束；工具暴露边界。
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
    AuditActorType,
    RefundScope,
)
from app.actions.service import ActionWorkflowService
from app.actions.sqlite_store import SQLiteActionStore
from app.agent.invocation_context import AgentInvocationContext
from app.application.organization_service import (
    OrganizationService,
    TenantContext,
)
from app.customers.sqlite_store import SQLiteCustomerStore
from app.orders.base import Order, OrderStatus
from app.orders.sqlite_store import SQLiteOrderStore
from app.organizations.base import MembershipRole, Organization
from app.organizations.sqlite_store import SQLiteOrganizationStore
from app.tools.action_gateway import ActionToolGateway
from app.users.base import User
from app.users.sqlite_store import SQLiteUserStore


@dataclass
class GatewayScope:
    """Gateway 测试所需的完整企业范围数据。"""

    store: ActionStore  # 动作工作流存储
    database_path: Path  # 业务数据库文件路径
    org_a: Organization  # 企业 A
    org_b: Organization  # 企业 B
    alice: User  # 企业 A 管理员
    order_a: Order  # 企业 A 订单，总额 100 元
    order_b: Order  # 企业 B 订单


class AdvancingRunner:
    """模拟工作流推进的假运行器：start 推进到等待审批，resume 推进到 running。"""

    def __init__(self, *, store: ActionStore, organization_id: str):
        self._store = store  # 被测动作存储
        self._organization_id = organization_id  # 状态推进所属企业

    def start(self, *, organization_id: str, run_id: str) -> None:
        self._transition(
            run_id,
            expected=(ActionRunStatus.QUEUED,),
            new_status=ActionRunStatus.RUNNING,
            event_type="run_started",
        )
        self._transition(
            run_id,
            expected=(ActionRunStatus.RUNNING,),
            new_status=ActionRunStatus.AWAITING_APPROVAL,
            event_type="run_awaiting_approval",
        )

    def resume(self, *, organization_id: str, run_id: str) -> None:
        self._transition(
            run_id,
            expected=(ActionRunStatus.AWAITING_APPROVAL,),
            new_status=ActionRunStatus.RUNNING,
            event_type="run_resumed",
        )

    def _transition(
        self,
        run_id: str,
        *,
        expected: tuple[ActionRunStatus, ...],
        new_status: ActionRunStatus,
        event_type: str,
    ) -> None:
        self._store.transition_run(
            organization_id=self._organization_id,
            run_id=run_id,
            expected_statuses=expected,
            new_status=new_status,
            error_code=None,
            error_retryable=False,
            actor_type=AuditActorType.SYSTEM,
            actor_user_id=None,
            event_type=event_type,
            details_json="{}",
        )


def build_scope(tmp_path) -> GatewayScope:
    """构造用户、企业与订单，并返回动作存储。"""
    database_path = tmp_path / "app.db"
    users = SQLiteUserStore(database_path)
    organizations = SQLiteOrganizationStore(database_path)
    customers = SQLiteCustomerStore(database_path)
    orders = SQLiteOrderStore(database_path)
    actions = SQLiteActionStore(database_path)

    alice = users.create_user(username="alice", password_hash="hash")
    org_a = organizations.create_with_admin(
        name="企业 A",
        admin_user_id=alice.user_id,
    )
    org_b = organizations.create_with_admin(
        name="企业 B",
        admin_user_id=alice.user_id,
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
    return GatewayScope(
        store=actions,
        database_path=database_path,
        org_a=org_a,
        org_b=org_b,
        alice=alice,
        order_a=order_a,
        order_b=order_b,
    )


def build_gateway(tmp_path):
    """装配 ActionToolGateway 及其依赖。"""
    scope = build_scope(tmp_path)
    service = build_service(
        scope,
        runner=AdvancingRunner(
            store=scope.store,
            organization_id=scope.org_a.organization_id,
        ),
    )
    gateway = ActionToolGateway(
        service=service,
        order_store=SQLiteOrderStore(scope.database_path),
    )
    invocation = AgentInvocationContext(
        tenant=TenantContext(
            user_id=scope.alice.user_id,
            organization_id=scope.org_a.organization_id,
            role=MembershipRole.ADMIN,
        ),
        conversation_id="conv-gateway",
        turn_id="turn-gateway-1",
    )
    return scope, gateway, invocation


def build_service(scope: GatewayScope, *, runner):
    """为指定测试范围装配动作应用服务。"""
    organization_service = OrganizationService(
        organization_store=SQLiteOrganizationStore(scope.database_path),
        user_store=SQLiteUserStore(scope.database_path),
    )
    return ActionWorkflowService(
        store=scope.store,
        organization_service=organization_service,
        runner=runner,
    )


def count_rows(database_path: Path, table: str) -> int:
    """统计指定业务表的行数。"""
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    return int(row[0])


def test_propose_refund_creates_proposal_with_context(tmp_path):
    # 保护行为：提案工具通过绑定上下文把会话与回合标识传入应用服务，
    # 返回设计 12.5 的待审批结构，不产生退款副作用。
    scope, gateway, invocation = build_gateway(tmp_path)
    functions = gateway.bind(context=invocation)

    result = functions["propose_refund"](
        order_no=scope.order_a.order_no,
        amount_cents=6000,
        currency="CNY",
        refund_scope="partial",
        reason_code="quality_issue",
        reason_text="商品存在质量问题",
    )

    assert result["ok"] is True
    data = result["data"]
    assert data["action_type"] == "refund"
    assert data["status"] == "awaiting_approval"
    assert data["amount_cents"] == 6000
    assert data["currency"] == "CNY"
    assert data["resume_required"] is False
    assert data["error_code"] is None
    assert data["run_id"]
    assert data["proposal_id"]
    assert data["approval_id"]
    # 会话与回合标识通过 AgentInvocationContext 到达 action_runs。
    with sqlite3.connect(scope.database_path) as connection:
        row = connection.execute(
            """
            SELECT conversation_id, turn_id
            FROM action_runs
            WHERE id = ?
            """,
            (data["run_id"],),
        ).fetchone()
    assert row[0] == "conv-gateway"
    assert row[1] == "turn-gateway-1"
    # 提案创建不产生任何退款记录。
    assert count_rows(scope.database_path, "refund_records") == 0


def test_propose_refund_order_not_found_returns_stable_code(tmp_path):
    # 边界情况：跨租户或不存在的订单统一返回 ACTION_ORDER_NOT_FOUND。
    scope, gateway, invocation = build_gateway(tmp_path)
    functions = gateway.bind(context=invocation)

    missing = functions["propose_refund"](
        order_no="ORD-NO-SUCH",
        amount_cents=6000,
        currency="CNY",
        refund_scope="partial",
        reason_code="quality_issue",
        reason_text="商品存在质量问题",
    )
    cross_tenant = functions["propose_refund"](
        order_no=scope.order_b.order_no,
        amount_cents=6000,
        currency="CNY",
        refund_scope="partial",
        reason_code="quality_issue",
        reason_text="商品存在质量问题",
    )

    assert missing["ok"] is False
    assert missing["error"]["code"] == "ACTION_ORDER_NOT_FOUND"
    assert cross_tenant["ok"] is False
    assert cross_tenant["error"]["code"] == "ACTION_ORDER_NOT_FOUND"
    assert count_rows(scope.database_path, "action_proposals") == 0


def test_propose_refund_currency_mismatch_returns_stable_code(tmp_path):
    # 边界情况：币种与订单不一致返回 ACTION_CURRENCY_MISMATCH，不创建提案。
    scope, gateway, invocation = build_gateway(tmp_path)
    functions = gateway.bind(context=invocation)

    result = functions["propose_refund"](
        order_no=scope.order_a.order_no,
        amount_cents=6000,
        currency="USD",
        refund_scope="partial",
        reason_code="quality_issue",
        reason_text="商品存在质量问题",
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "ACTION_CURRENCY_MISMATCH"
    assert count_rows(scope.database_path, "action_proposals") == 0


def test_propose_compensation_uses_fixed_coupon_days(tmp_path):
    # 保护行为：补偿提案固定使用 30 天券有效期，
    # 模型不能指定 coupon_valid_days（参数中不存在该字段）。
    scope, gateway, invocation = build_gateway(tmp_path)
    functions = gateway.bind(context=invocation)

    result = functions["propose_compensation"](
        order_no=scope.order_a.order_no,
        amount_cents=3000,
        currency="CNY",
        reason_code="delayed_shipment",
        reason_text="发货延迟",
    )

    assert result["ok"] is True
    assert result["data"]["action_type"] == "compensation"
    with sqlite3.connect(scope.database_path) as connection:
        row = connection.execute(
            """
            SELECT parameters_json
            FROM action_proposal_versions
            WHERE proposal_id = ?
            """,
            (result["data"]["proposal_id"],),
        ).fetchone()
    assert json.loads(row[0]) == {"coupon_valid_days": 30}


def test_proposal_start_failure_returns_recovery_metadata(tmp_path):
    # 故障窗口：提案事实已创建但运行器不可用时，工具必须保留标识，
    # 同时明确返回 queued、resume_required 与稳定错误码。
    scope = build_scope(tmp_path)
    gateway = ActionToolGateway(
        service=build_service(scope, runner=None),
        order_store=SQLiteOrderStore(scope.database_path),
    )
    invocation = AgentInvocationContext(
        tenant=TenantContext(
            user_id=scope.alice.user_id,
            organization_id=scope.org_a.organization_id,
            role=MembershipRole.ADMIN,
        ),
        conversation_id="conv-start-failure",
        turn_id="turn-start-failure",
    )

    result = gateway.bind(context=invocation)["propose_refund"](
        order_no=scope.order_a.order_no,
        amount_cents=1000,
        currency="CNY",
        refund_scope="partial",
        reason_code="quality_issue",
        reason_text="质量问题",
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "queued"
    assert result["data"]["resume_required"] is True
    assert result["data"]["error_code"] == "CHECKPOINT_UNAVAILABLE"
    assert result["data"]["run_id"]
    assert result["data"]["approval_id"]


def test_propose_active_proposal_exists_returns_stable_code(tmp_path):
    # 边界情况：同订单同动作类型已有非终态提案时返回
    # ACTION_ACTIVE_PROPOSAL_EXISTS，不创建第二份待审批项。
    scope, gateway, invocation = build_gateway(tmp_path)
    functions = gateway.bind(context=invocation)
    first = functions["propose_refund"](
        order_no=scope.order_a.order_no,
        amount_cents=6000,
        currency="CNY",
        refund_scope="partial",
        reason_code="quality_issue",
        reason_text="商品存在质量问题",
    )
    second = functions["propose_refund"](
        order_no=scope.order_a.order_no,
        amount_cents=2000,
        currency="CNY",
        refund_scope="partial",
        reason_code="quality_issue",
        reason_text="再次申请",
    )

    assert first["ok"] is True
    assert second["ok"] is False
    assert second["error"]["code"] == "ACTION_ACTIVE_PROPOSAL_EXISTS"
    assert count_rows(scope.database_path, "action_proposals") == 1


def test_get_action_status_reads_run_and_hides_unknown(tmp_path):
    # 保护行为：状态工具返回 Run、审批与执行摘要；
    # 跨租户与不存在的 Run 统一返回 RUN_NOT_FOUND（设计 12.4）。
    scope, gateway, invocation = build_gateway(tmp_path)
    functions = gateway.bind(context=invocation)
    created = functions["propose_refund"](
        order_no=scope.order_a.order_no,
        amount_cents=6000,
        currency="CNY",
        refund_scope="partial",
        reason_code="quality_issue",
        reason_text="商品存在质量问题",
    )

    found = functions["get_action_status"](
        run_id=created["data"]["run_id"],
    )
    missing = functions["get_action_status"](run_id="run-missing")

    assert found["ok"] is True
    assert found["data"]["status"] == "awaiting_approval"
    assert found["data"]["order_no"] == scope.order_a.order_no
    assert found["data"]["approval_status"] == "pending"
    assert found["data"]["decision"] is None
    assert found["data"]["decision_comment"] is None
    assert found["data"]["run_error_code"] is None
    assert found["data"]["run_error_retryable"] is False
    assert found["data"]["current_version"] == {
        "version_no": 1,
        "amount_cents": 6000,
        "currency": "CNY",
        "reason_code": "quality_issue",
        "reason_text": "商品存在质量问题",
        "parameters": {"refund_scope": "partial"},
    }
    assert found["data"]["execution_status"] is None
    assert "thread_id" not in found["data"]
    assert missing["ok"] is False
    assert missing["error"]["code"] == "RUN_NOT_FOUND"


def test_get_action_status_exposes_run_level_failure(tmp_path):
    # 故障说明：执行记录产生前的工作流失败必须通过状态工具返回 Run 级
    # 稳定错误码和可重试标记，不能只依赖 execution_error_code。
    scope, gateway, invocation = build_gateway(tmp_path)
    functions = gateway.bind(context=invocation)
    created = functions["propose_refund"](
        order_no=scope.order_a.order_no,
        amount_cents=6000,
        currency="CNY",
        refund_scope="partial",
        reason_code="quality_issue",
        reason_text="商品存在质量问题",
    )
    scope.store.transition_run(
        organization_id=scope.org_a.organization_id,
        run_id=created["data"]["run_id"],
        expected_statuses=(ActionRunStatus.AWAITING_APPROVAL,),
        new_status=ActionRunStatus.FAILED,
        error_code="EXECUTION_DATA_INTEGRITY_ERROR",
        error_retryable=False,
        actor_type=AuditActorType.SYSTEM,
        actor_user_id=None,
        event_type="run_failed",
        details_json="{}",
    )

    status = functions["get_action_status"](
        run_id=created["data"]["run_id"],
    )

    assert status["ok"] is True
    assert status["data"]["status"] == "failed"
    assert status["data"]["run_error_code"] == (
        "EXECUTION_DATA_INTEGRITY_ERROR"
    )
    assert status["data"]["run_error_retryable"] is False
    assert status["data"]["execution_status"] is None


def test_bind_requires_agent_invocation_context(tmp_path):
    # 边界情况：动作工具只接受 AgentInvocationContext，
    # 直接传入 TenantContext 属于编程错误，立即失败。
    scope, gateway, _ = build_gateway(tmp_path)
    tenant = TenantContext(
        user_id=scope.alice.user_id,
        organization_id=scope.org_a.organization_id,
        role=MembershipRole.ADMIN,
    )

    with pytest.raises(TypeError):
        gateway.bind(context=tenant)


def test_execute_unknown_tool_and_invalid_arguments(tmp_path):
    # 边界情况：未知工具与非法参数返回稳定的工具错误，不抛异常。
    scope, gateway, invocation = build_gateway(tmp_path)

    unknown = gateway.execute(
        context=invocation,
        tool_name="approve_action",
        arguments={},
    )
    invalid = gateway.execute(
        context=invocation,
        tool_name="propose_refund",
        arguments={
            "order_no": scope.order_a.order_no,
            "amount_cents": True,
            "currency": "CNY",
            "refund_scope": "partial",
            "reason_code": "quality_issue",
            "reason_text": "商品存在质量问题",
        },
    )

    assert unknown["ok"] is False
    assert unknown["error"]["code"] == "UNKNOWN_TOOL"
    assert invalid["ok"] is False
    assert invalid["error"]["code"] == "INVALID_ARGUMENTS"
    assert count_rows(scope.database_path, "action_proposals") == 0


def test_unexpected_infrastructure_error_is_redacted(tmp_path):
    # 安全边界：未预期数据库异常必须映射为稳定错误码，不能把文件路径、
    # SQL 或原始异常内容返回给模型。
    class FailingOrderStore:
        """始终抛出包含敏感路径的测试订单存储。"""

        def get_by_no(self, **kwargs):
            raise RuntimeError("数据库位于 D:/secret/customer/app.db")

    scope, _, invocation = build_gateway(tmp_path)
    gateway = ActionToolGateway(
        service=build_service(
            scope,
            runner=AdvancingRunner(
                store=scope.store,
                organization_id=scope.org_a.organization_id,
            ),
        ),
        order_store=FailingOrderStore(),
    )

    result = gateway.bind(context=invocation)["propose_refund"](
        order_no=scope.order_a.order_no,
        amount_cents=1000,
        currency="CNY",
        refund_scope="partial",
        reason_code="quality_issue",
        reason_text="质量问题",
    )

    serialized = json.dumps(result, ensure_ascii=False)
    assert result["ok"] is False
    assert result["error"]["code"] == "ACTION_TOOL_UNAVAILABLE"
    assert "D:/secret" not in serialized
    assert "RuntimeError" not in serialized


def test_gateway_exposes_only_propose_and_status_tools(tmp_path):
    # 保护行为：工具定义与绑定函数只包含三个动作工具，
    # 不暴露审批、恢复与执行（设计 12.6）。
    scope, gateway, invocation = build_gateway(tmp_path)

    names = {
        item["function"]["name"] for item in gateway.definitions
    }
    bound = set(gateway.bind(context=invocation).keys())

    assert names == {
        "propose_refund",
        "propose_compensation",
        "get_action_status",
    }
    assert bound == names
    assert not names & {
        "approve_action",
        "resume_run",
        "execute_refund",
        "issue_compensation",
    }
