"""审批与 Run HTTP API 测试（设计 18.5，Task 6）。

覆盖：可信租户上下文下的列表、详情、决定与恢复；跨租户访问统一 404；
非管理员决定/恢复 403；Pydantic 拒绝未知字段；决定状态码 201/202/200/409；
恢复状态码 200/409/503；并发不同决定只有一个成功；以及使用真实 LangGraph
运行器验证批准执行、拒绝终态与重复恢复的稳定结果。
"""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.actions.base import (
    ActionError,
    ActionRunStatus,
    ActionStore,
    ActionType,
    AuditActorType,
    CheckpointUnavailableError,
    RefundScope,
)
from app.actions.factory import build_action_executor
from app.actions.service import ActionWorkflowService
from app.actions.sqlite_store import SQLiteActionStore
from app.api.action_router import create_action_router
from app.application.organization_service import (
    OrganizationService,
    TenantContext,
)
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
class RouterScope:
    """API 测试所需的完整企业范围数据。"""

    store: ActionStore  # 动作工作流存储
    database_path: Path  # 业务数据库文件路径
    org_a: Organization  # 企业 A
    org_b: Organization  # 企业 B
    alice: User  # 企业 A 管理员，兼提案人
    dave: User  # 企业 A 第二位管理员，用于并发决定测试
    bob: User  # 企业 A 的 agent 成员与企业 B 管理员
    carol: User  # 不属于企业 A 的用户，用于非成员访问测试
    order_a: Order  # 企业 A 订单，总额 100 元
    order_a2: Order  # 企业 A 第二笔订单，总额 100 元（列表分页测试用）
    order_b: Order  # 企业 B 订单


class FakeWorkflowRunner:
    """测试用状态推进运行器：记录调用并模拟工作流状态转换。

    只模拟 Task 5 运行器的调用边界与状态推进，不模拟 LangGraph：
    start 把 Run 从 queued 推进到 awaiting_approval；resume 把等待
    审批的 Run 推进到 running；可注入恢复失败，便于覆盖 202/503 路径。
    """

    def __init__(
        self,
        *,
        store: ActionStore,
        organization_id: str,
        resume_error: ActionError | None = None,
    ):
        self._store = store  # 被测动作存储，用于推进 Run 状态
        self._organization_id = organization_id  # 状态推进所属企业
        self.started: list[tuple[str, str]] = []  # 已启动的（企业, Run）标识
        self.resumed: list[tuple[str, str]] = []  # 已恢复的（企业, Run）标识
        self.resume_error = resume_error  # 恢复注入的失败，可运行中修改

    def start(self, *, organization_id: str, run_id: str) -> None:
        self.started.append((organization_id, run_id))
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
        self.resumed.append((organization_id, run_id))
        if self.resume_error is not None:
            raise self.resume_error
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
        """把 Run 推进到下一个业务阶段并追加系统审计。"""
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


def build_scope(tmp_path) -> RouterScope:
    """构造用户、企业与订单，并返回动作存储。"""
    database_path = tmp_path / "app.db"
    users = SQLiteUserStore(database_path)
    organizations = SQLiteOrganizationStore(database_path)
    customers = SQLiteCustomerStore(database_path)
    orders = SQLiteOrderStore(database_path)
    actions = SQLiteActionStore(database_path)

    alice = users.create_user(username="alice", password_hash="hash")
    dave = users.create_user(username="dave", password_hash="hash")
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
        user_id=dave.user_id,
        role=MembershipRole.ADMIN,
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
    return RouterScope(
        store=actions,
        database_path=database_path,
        org_a=org_a,
        org_b=org_b,
        alice=alice,
        dave=dave,
        bob=bob,
        carol=carol,
        order_a=order_a,
        order_a2=order_a2,
        order_b=order_b,
    )


def _make_client(
    action_service: ActionWorkflowService,
    *,
    user_id: str,
    organization_id: str,
    role: MembershipRole,
) -> TestClient:
    """为指定用户与企业的组合构造仅含动作路由的测试客户端。"""

    def get_current_tenant() -> TenantContext:
        return TenantContext(
            user_id=user_id,
            organization_id=organization_id,
            role=role,
        )

    app = FastAPI()
    app.include_router(
        create_action_router(
            action_service=action_service,
            get_current_tenant=get_current_tenant,
        )
    )
    return TestClient(app)


def _make_denied_client(
    action_service: ActionWorkflowService,
) -> TestClient:
    """构造租户依赖直接拒绝的客户端。

    模拟真实应用对非成员的依赖行为：``get_current_tenant`` 在到达
    路由前抛出 404，路由层根本不会看到请求（设计 14）。
    """

    def get_current_tenant() -> TenantContext:
        raise HTTPException(
            status_code=404,
            detail={"code": "ORGANIZATION_NOT_FOUND", "message": "企业不存在"},
        )

    app = FastAPI()
    app.include_router(
        create_action_router(
            action_service=action_service,
            get_current_tenant=get_current_tenant,
        )
    )
    return TestClient(app)


def build_fake_clients(tmp_path):
    """构造测试范围、假运行器应用服务与各用户角色的客户端集合。

    返回 (scope, clients, service, runner)；clients 键名含义：
    alice_a 企业 A 管理员、dave_a 企业 A 管理员、bob_a 企业 A agent、
    bob_b 企业 B 管理员、carol_denied 企业 A 非成员（依赖层拒绝）。
    """
    scope = build_scope(tmp_path)
    organization_service = OrganizationService(
        organization_store=SQLiteOrganizationStore(scope.database_path),
        user_store=SQLiteUserStore(scope.database_path),
    )
    runner = FakeWorkflowRunner(
        store=scope.store,
        organization_id=scope.org_a.organization_id,
    )
    service = ActionWorkflowService(
        store=scope.store,
        organization_service=organization_service,
        runner=runner,
    )
    clients = {
        "alice_a": _make_client(
            service,
            user_id=scope.alice.user_id,
            organization_id=scope.org_a.organization_id,
            role=MembershipRole.ADMIN,
        ),
        "dave_a": _make_client(
            service,
            user_id=scope.dave.user_id,
            organization_id=scope.org_a.organization_id,
            role=MembershipRole.ADMIN,
        ),
        "bob_a": _make_client(
            service,
            user_id=scope.bob.user_id,
            organization_id=scope.org_a.organization_id,
            role=MembershipRole.AGENT,
        ),
        "bob_b": _make_client(
            service,
            user_id=scope.bob.user_id,
            organization_id=scope.org_b.organization_id,
            role=MembershipRole.ADMIN,
        ),
        "carol_denied": _make_denied_client(service),
    }
    return scope, clients, service, runner


def build_real_clients(tmp_path):
    """装配使用真实 LangGraph 运行器的应用服务与客户端集合。"""
    scope = build_scope(tmp_path)
    organization_service = OrganizationService(
        organization_store=SQLiteOrganizationStore(scope.database_path),
        user_store=SQLiteUserStore(scope.database_path),
    )
    executor = build_action_executor(scope.store)
    checkpointer = create_sqlite_checkpointer(tmp_path / "checkpoints.db")
    graph = build_action_graph(
        store=scope.store,
        executor=executor,
        checkpointer=checkpointer,
    )
    runner = LangGraphActionWorkflowRunner(
        store=scope.store,
        graph=graph,
    )
    service = ActionWorkflowService(
        store=scope.store,
        organization_service=organization_service,
        runner=runner,
    )
    alice_a = _make_client(
        service,
        user_id=scope.alice.user_id,
        organization_id=scope.org_a.organization_id,
        role=MembershipRole.ADMIN,
    )
    return scope, {"alice_a": alice_a}, service


def create_refund_proposal(
    service: ActionWorkflowService,
    scope: RouterScope,
    *,
    user_id: str | None = None,
    order_id: str | None = None,
    amount_cents: int = 6000,
    reason_code: str = "quality_issue",
    reason_text: str = "商品存在质量问题",
    refund_scope: RefundScope = RefundScope.PARTIAL,
    conversation_id: str = "conv-refund",
) -> dict:
    """通过应用服务创建退款提案，返回 Run/提案/审批标识。"""
    outcome = service.create_proposal(
        organization_id=scope.org_a.organization_id,
        user_id=user_id or scope.alice.user_id,
        conversation_id=conversation_id,
        turn_id="turn-1",
        order_id=order_id or scope.order_a.order_id,
        action_type=ActionType.REFUND,
        amount_cents=amount_cents,
        currency="CNY",
        reason_code=reason_code,
        reason_text=reason_text,
        refund_scope=refund_scope,
    )
    return {
        "run_id": outcome.creation.run.run_id,
        "proposal_id": outcome.creation.proposal.proposal_id,
        "approval_id": outcome.creation.approval.approval_id,
    }


def count_rows(database_path: Path, table: str) -> int:
    """统计指定业务表的行数。"""
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    return int(row[0])


# —— 查询：列表与详情 ——


def test_agent_can_list_and_read_approval_detail(tmp_path):
    # 保护行为：当前企业的 agent 可以读取审批列表与详情，
    # 列表项包含 Run 状态、当前版本与决定摘要（设计 13.1）。
    scope, clients, service, runner = build_fake_clients(tmp_path)
    ids = create_refund_proposal(service, scope)

    listed = clients["bob_a"].get("/approvals/")
    detail = clients["bob_a"].get(
        f"/approvals/{ids['approval_id']}/"
    )

    assert listed.status_code == 200
    assert len(listed.json()) == 1
    item = listed.json()[0]
    assert item["approval_id"] == ids["approval_id"]
    assert item["run_status"] == "awaiting_approval"
    assert item["approval_status"] == "pending"
    assert item["current_version"]["amount_cents"] == 6000
    assert item["current_version"]["refund_scope"] == "partial"
    assert item["version_count"] == 1
    assert item["decision"] is None
    assert detail.status_code == 200
    body = detail.json()
    assert body["run"]["status"] == "awaiting_approval"
    assert body["requested_version"]["version_no"] == 1
    assert body["versions"][0]["version_no"] == 1
    assert body["decision"] is None


def test_cross_tenant_access_returns_404(tmp_path):
    # 边界情况：企业 B 管理员使用企业 A 的 Approval ID / Run ID 时，
    # 所有读取、决定与恢复接口统一返回 404，不泄露资源存在性；
    # 企业 A 非成员在租户依赖层即被拒绝（设计 19 场景 F / 14）。
    scope, clients, service, runner = build_fake_clients(tmp_path)
    ids = create_refund_proposal(service, scope)

    bob_b = clients["bob_b"]
    read_approval = bob_b.get(
        f"/approvals/{ids['approval_id']}/"
    )
    read_run = bob_b.get(f"/action-runs/{ids['run_id']}/")
    decide = bob_b.post(
        f"/approvals/{ids['approval_id']}/decisions/",
        json={"decision": "approved"},
    )
    resume = bob_b.post(f"/action-runs/{ids['run_id']}/resume/")

    assert read_approval.status_code == 404
    assert read_run.status_code == 404
    assert decide.status_code == 404
    assert resume.status_code == 404
    assert read_approval.json()["detail"]["code"] == "APPROVAL_NOT_FOUND"
    assert read_run.json()["detail"]["code"] == "RUN_NOT_FOUND"
    # 企业 A 非成员的所有请求在依赖层返回 404，路由层不可达。
    denied = clients["carol_denied"]
    for response in (
        denied.get(f"/approvals/{ids['approval_id']}/"),
        denied.get(f"/action-runs/{ids['run_id']}/"),
        denied.post(
            f"/approvals/{ids['approval_id']}/decisions/",
            json={"decision": "approved"},
        ),
        denied.post(f"/action-runs/{ids['run_id']}/resume/"),
    ):
        assert response.status_code == 404
    # 跨租户尝试没有产生任何副作用。
    assert count_rows(scope.database_path, "approval_decisions") == 0
    assert runner.resumed == []


def test_run_status_query_returns_view_without_internal_ids(tmp_path):
    # 保护行为：Run 状态查询返回提案、审批与当前版本视图，
    # 且不暴露 thread_id 与幂等键等内部标识（设计 12.6）。
    scope, clients, service, runner = build_fake_clients(tmp_path)
    ids = create_refund_proposal(service, scope)

    response = clients["bob_a"].get(f"/action-runs/{ids['run_id']}/")

    assert response.status_code == 200
    body = response.json()
    assert body["run"]["status"] == "awaiting_approval"
    assert body["proposal"]["order_id"] == scope.order_a.order_id
    assert body["approval"]["status"] == "pending"
    assert body["decision"] is None
    assert body["execution"] is None
    assert body["result"] is None
    assert "thread_id" not in body["run"]


def test_list_status_filter_and_pagination(tmp_path):
    # 边界情况：列表支持按审批状态过滤与分页，
    # 未知状态枚举由 Pydantic 拒绝为 422。
    scope, clients, service, runner = build_fake_clients(tmp_path)
    first = create_refund_proposal(service, scope)
    second = create_refund_proposal(
        service,
        scope,
        order_id=scope.order_a2.order_id,
        conversation_id="conv-refund-2",
    )
    assert first["approval_id"] != second["approval_id"]

    pending = clients["alice_a"].get(
        "/approvals/",
        params={"status": "pending"},
    )
    decided = clients["alice_a"].get(
        "/approvals/",
        params={"status": "approved"},
    )
    limited = clients["alice_a"].get(
        "/approvals/",
        params={"limit": 1},
    )
    invalid = clients["alice_a"].get(
        "/approvals/",
        params={"status": "decided"},
    )

    assert pending.status_code == 200
    assert len(pending.json()) == 2
    assert decided.status_code == 200
    assert decided.json() == []
    assert limited.status_code == 200
    assert len(limited.json()) == 1
    assert invalid.status_code == 422


# —— 决定接口 ——


def test_agent_cannot_decide_or_resume_returns_403(tmp_path):
    # 保护行为：当前企业 agent 决定与恢复返回 403，
    # 只有 admin 可以作出审批决定与显式恢复（设计 13.2 / 13.3）。
    scope, clients, service, runner = build_fake_clients(tmp_path)
    ids = create_refund_proposal(service, scope)

    decide = clients["bob_a"].post(
        f"/approvals/{ids['approval_id']}/decisions/",
        json={"decision": "approved"},
    )
    resume = clients["bob_a"].post(
        f"/action-runs/{ids['run_id']}/resume/"
    )

    assert decide.status_code == 403
    assert decide.json()["detail"]["code"] == "APPROVAL_ADMIN_REQUIRED"
    assert resume.status_code == 403
    assert resume.json()["detail"]["code"] == "APPROVAL_ADMIN_REQUIRED"
    # 被拒绝的调用没有产生任何决定或恢复副作用。
    assert count_rows(scope.database_path, "approval_decisions") == 0


def test_admin_approval_returns_201_and_records_self_approved(tmp_path):
    # 保护行为：管理员首次批准返回 201，自动恢复被调用，
    # 响应包含持久化决定、Run 状态与自审标记（设计 13.2 / 14）。
    scope, clients, service, runner = build_fake_clients(tmp_path)
    ids = create_refund_proposal(service, scope)

    response = clients["alice_a"].post(
        f"/approvals/{ids['approval_id']}/decisions/",
        json={"decision": "approved", "comment": "同意退款"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["decision"] == "approved"
    assert body["approval_id"] == ids["approval_id"]
    assert body["run_id"] == ids["run_id"]
    assert body["comment"] == "同意退款"
    assert body["resume_required"] is False
    assert body["resume_error_code"] is None
    assert body["self_approved"] is True
    assert runner.resumed == [(scope.org_a.organization_id, ids["run_id"])]
    # 决定已经持久化，可查询。
    detail = clients["alice_a"].get(
        f"/approvals/{ids['approval_id']}/"
    )
    assert detail.json()["decision"]["decision"] == "approved"


def test_decision_saved_but_resume_failed_returns_202_and_retryable(
    tmp_path,
):
    # 边界情况：决定已保存但工作流恢复失败时返回 202 与
    # resume_required=true、稳定错误码；决定不丢失，排除故障后可
    # 通过恢复 API 继续，不需要重复审批（设计 13.2 / 10.2）。
    scope, clients, service, runner = build_fake_clients(tmp_path)
    runner.resume_error = CheckpointUnavailableError(
        "checkpoint 不可用"
    )
    ids = create_refund_proposal(service, scope)

    decided = clients["alice_a"].post(
        f"/approvals/{ids['approval_id']}/decisions/",
        json={"decision": "approved"},
    )
    failed_resume = clients["alice_a"].post(
        f"/action-runs/{ids['run_id']}/resume/"
    )
    runner.resume_error = None
    retried_resume = clients["alice_a"].post(
        f"/action-runs/{ids['run_id']}/resume/"
    )

    assert decided.status_code == 202
    assert decided.json()["resume_required"] is True
    assert decided.json()["resume_error_code"] == "CHECKPOINT_UNAVAILABLE"
    assert decided.json()["decision"] == "approved"
    # 决定已持久化，不会被回滚或伪装成未审批。
    detail = clients["alice_a"].get(
        f"/approvals/{ids['approval_id']}/"
    )
    assert detail.json()["decision"]["decision"] == "approved"
    # 恢复失败返回 503 与稳定错误码，业务事实保持不变。
    assert failed_resume.status_code == 503
    assert failed_resume.json()["detail"]["code"] == "CHECKPOINT_UNAVAILABLE"
    # 排除故障后显式恢复成功，且不需要再次审批。
    assert retried_resume.status_code == 200
    assert retried_resume.json()["resume_ok"] is True
    assert retried_resume.json()["error_code"] is None


def test_repeat_same_decision_returns_200_with_first_result(tmp_path):
    # 边界情况：相同审批重复提交完全相同的决定时返回首次结果
    # 与当前 Run 状态（200），不产生第二条决定（设计 8.4 / 13.2）。
    scope, clients, service, runner = build_fake_clients(tmp_path)
    ids = create_refund_proposal(service, scope)

    first = clients["alice_a"].post(
        f"/approvals/{ids['approval_id']}/decisions/",
        json={"decision": "approved"},
    )
    repeat = clients["alice_a"].post(
        f"/approvals/{ids['approval_id']}/decisions/",
        json={"decision": "approved"},
    )

    assert first.status_code == 201
    assert repeat.status_code == 200
    assert (
        repeat.json()["decision_id"]
        == first.json()["decision_id"]
    )
    assert count_rows(scope.database_path, "approval_decisions") == 1


def test_different_decision_after_decided_returns_409(tmp_path):
    # 边界情况：审批已决定后提交不同决定返回 409 APPROVAL_ALREADY_DECIDED，
    # 不静默覆盖既有决定（设计 8.4 / 15.1）。
    scope, clients, service, runner = build_fake_clients(tmp_path)
    ids = create_refund_proposal(service, scope)

    first = clients["alice_a"].post(
        f"/approvals/{ids['approval_id']}/decisions/",
        json={"decision": "approved"},
    )
    conflict = clients["dave_a"].post(
        f"/approvals/{ids['approval_id']}/decisions/",
        json={"decision": "rejected", "comment": "不符合"},
    )

    assert first.status_code == 201
    assert conflict.status_code == 409
    assert (
        conflict.json()["detail"]["code"] == "APPROVAL_ALREADY_DECIDED"
    )
    assert count_rows(scope.database_path, "approval_decisions") == 1


def test_approved_with_changes_creates_new_version(tmp_path):
    # 保护行为：修改后批准在一个事务内创建新版本并使决定引用新版本，
    # 详情接口返回全部历史版本（设计 8.4 / 13.2）。
    scope, clients, service, runner = build_fake_clients(tmp_path)
    ids = create_refund_proposal(service, scope)

    response = clients["alice_a"].post(
        f"/approvals/{ids['approval_id']}/decisions/",
        json={
            "decision": "approved_with_changes",
            "changes": {
                "amount_cents": 800,
                "refund_scope": "partial",
                "reason_code": "quality_issue",
                "reason_text": "仅对存在质量问题的商品进行部分退款",
            },
            "comment": "改为部分退款",
        },
    )
    detail = clients["alice_a"].get(
        f"/approvals/{ids['approval_id']}/"
    )

    assert response.status_code == 201
    assert response.json()["decision"] == "approved_with_changes"
    # 假运行器的 resume 把 Run 从等待审批推进到 running。
    assert response.json()["run_status"] == "running"
    body = detail.json()
    assert len(body["versions"]) == 2
    assert body["versions"][0]["amount_cents"] == 6000
    assert body["versions"][1]["amount_cents"] == 800
    assert body["current_version"]["amount_cents"] == 800
    assert body["decision"]["decided_version_id"] == (
        body["versions"][1]["version_id"]
    )


def test_approved_or_rejected_with_changes_returns_422(tmp_path):
    # 边界情况：批准或拒绝携带修改内容、修改后批准缺少修改内容，
    # 均返回 422 APPROVAL_INVALID_CHANGES（设计 13.2 约束）。
    scope, clients, service, runner = build_fake_clients(tmp_path)
    first = create_refund_proposal(service, scope, conversation_id="conv-1")
    second = create_refund_proposal(
        service,
        scope,
        order_id=scope.order_a2.order_id,
        conversation_id="conv-2",
    )

    approved_with_changes = clients["alice_a"].post(
        f"/approvals/{first['approval_id']}/decisions/",
        json={
            "decision": "approved",
            "changes": {"amount_cents": 800},
        },
    )
    missing_changes = clients["alice_a"].post(
        f"/approvals/{second['approval_id']}/decisions/",
        json={"decision": "approved_with_changes"},
    )

    assert approved_with_changes.status_code == 422
    assert (
        approved_with_changes.json()["detail"]["code"]
        == "APPROVAL_INVALID_CHANGES"
    )
    assert missing_changes.status_code == 422
    assert (
        missing_changes.json()["detail"]["code"]
        == "APPROVAL_INVALID_CHANGES"
    )
    # 校验失败不会产生任何决定。
    assert count_rows(scope.database_path, "approval_decisions") == 0


def test_unknown_request_fields_rejected_422(tmp_path):
    # 边界情况：请求体与修改字段出现未知字段时由 Pydantic 拒绝，
    # 返回 422，不静默忽略（设计 14：extra='forbid'）。
    scope, clients, service, runner = build_fake_clients(tmp_path)
    ids = create_refund_proposal(service, scope)

    extra_top = clients["alice_a"].post(
        f"/approvals/{ids['approval_id']}/decisions/",
        json={"decision": "approved", "hacked": True},
    )
    extra_changes = clients["alice_a"].post(
        f"/approvals/{ids['approval_id']}/decisions/",
        json={
            "decision": "approved_with_changes",
            "changes": {
                "amount_cents": 800,
                "refund_scope": "partial",
                "organization_id": scope.org_b.organization_id,
            },
        },
    )

    assert extra_top.status_code == 422
    assert extra_changes.status_code == 422
    assert count_rows(scope.database_path, "approval_decisions") == 0


def test_invalid_changes_amount_rejected_422(tmp_path):
    # 边界情况：修改金额非正数时返回 422，不进入业务校验
    # （设计 13.2：金额必须为正整数，单位分）。
    scope, clients, service, runner = build_fake_clients(tmp_path)
    ids = create_refund_proposal(service, scope)

    response = clients["alice_a"].post(
        f"/approvals/{ids['approval_id']}/decisions/",
        json={
            "decision": "approved_with_changes",
            "changes": {"amount_cents": 0, "refund_scope": "partial"},
        },
    )

    assert response.status_code == 422
    assert count_rows(scope.database_path, "approval_decisions") == 0


@pytest.mark.parametrize("invalid_amount", [800.0, "800", True])
def test_changes_amount_rejects_coercible_non_integer_types(
    tmp_path,
    invalid_amount,
):
    # 边界情况：金额即使可被 Pydantic 转换为整数，只要 JSON 原始类型
    # 不是整数就必须返回 422，禁止浮点、字符串和布尔值进入批准版本。
    scope, clients, service, runner = build_fake_clients(tmp_path)
    ids = create_refund_proposal(service, scope)

    response = clients["alice_a"].post(
        f"/approvals/{ids['approval_id']}/decisions/",
        json={
            "decision": "approved_with_changes",
            "changes": {"amount_cents": invalid_amount},
        },
    )

    assert response.status_code == 422
    assert count_rows(scope.database_path, "approval_decisions") == 0


# —— 恢复接口 ——


def test_resume_missing_run_returns_404(tmp_path):
    # 边界情况：恢复不存在的 Run 返回 404 RUN_NOT_FOUND。
    scope, clients, service, runner = build_fake_clients(tmp_path)

    response = clients["alice_a"].post(
        "/action-runs/missing-run/resume/"
    )

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "RUN_NOT_FOUND"


def test_concurrent_different_decisions_single_winner(tmp_path):
    # 边界情况：两个管理员并发提交不同决定时恰好一个成功
    # （201/202），另一个返回 409，数据库只有一条决定（设计 15.1）。
    scope, clients, service, runner = build_fake_clients(tmp_path)
    ids = create_refund_proposal(service, scope)

    def decide_approve():
        return clients["alice_a"].post(
            f"/approvals/{ids['approval_id']}/decisions/",
            json={"decision": "approved"},
        )

    def decide_reject():
        return clients["dave_a"].post(
            f"/approvals/{ids['approval_id']}/decisions/",
            json={"decision": "rejected", "comment": "不符合"},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        approve_future = pool.submit(decide_approve)
        reject_future = pool.submit(decide_reject)
        statuses = [
            approve_future.result().status_code,
            reject_future.result().status_code,
        ]
    ok_count = sum(
        1 for status in statuses if status in (201, 202)
    )
    conflict_count = sum(
        1 for status in statuses if status == 409
    )
    assert ok_count == 1
    assert conflict_count == 1
    assert count_rows(scope.database_path, "approval_decisions") == 1


def test_concurrent_same_decision_returns_created_and_replayed_once(tmp_path):
    # 边界情况：两个管理员并发提交完全相同的决定时，Store 事务必须
    # 原子区分首次创建与幂等重放，只允许一次自动恢复并返回 201/200。
    scope, clients, service, runner = build_fake_clients(tmp_path)
    ids = create_refund_proposal(service, scope)

    def decide(client_name: str):
        return clients[client_name].post(
            f"/approvals/{ids['approval_id']}/decisions/",
            json={"decision": "approved"},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(decide, "alice_a"),
            pool.submit(decide, "dave_a"),
        ]
        responses = [future.result() for future in futures]

    assert sorted(response.status_code for response in responses) == [200, 201]
    assert len({response.json()["decision_id"] for response in responses}) == 1
    assert len(runner.resumed) == 1
    assert count_rows(scope.database_path, "approval_decisions") == 1


# —— 真实 LangGraph 运行器端到端 ——


def test_real_runner_approval_executes_and_repeat_resume_returns_result(
    tmp_path,
):
    # 保护行为：使用真实 LangGraph 运行器时，批准后自动恢复执行成功，
    # 重复恢复返回相同稳定业务结果，不产生第二条业务记录（设计 19 场景 A/D）。
    scope, clients, service = build_real_clients(tmp_path)
    ids = create_refund_proposal(service, scope)

    decided = clients["alice_a"].post(
        f"/approvals/{ids['approval_id']}/decisions/",
        json={"decision": "approved"},
    )
    assert decided.status_code == 201
    assert decided.json()["run_status"] == "succeeded"
    assert decided.json()["resume_required"] is False

    resumed = clients["alice_a"].post(
        f"/action-runs/{ids['run_id']}/resume/"
    )
    assert resumed.status_code == 200
    assert resumed.json()["resume_ok"] is True
    first_result = resumed.json()["result"]
    assert first_result is not None

    status = clients["alice_a"].get(f"/action-runs/{ids['run_id']}/")
    assert status.status_code == 200
    assert status.json()["execution"]["status"] == "succeeded"
    assert status.json()["result"] == first_result
    assert count_rows(scope.database_path, "refund_records") == 1


def test_real_runner_rejected_run_resume_returns_409(tmp_path):
    # 边界情况：使用真实 LangGraph 运行器时拒绝形成 cancelled 终态，
    # 已终态 Run 不允许恢复，返回 409 RUN_NOT_RESUMABLE（设计 19 场景 C）。
    scope, clients, service = build_real_clients(tmp_path)
    ids = create_refund_proposal(service, scope)

    decided = clients["alice_a"].post(
        f"/approvals/{ids['approval_id']}/decisions/",
        json={"decision": "rejected", "comment": "不符合政策"},
    )
    assert decided.status_code == 201
    assert decided.json()["run_status"] == "cancelled"

    resumed = clients["alice_a"].post(
        f"/action-runs/{ids['run_id']}/resume/"
    )
    assert resumed.status_code == 409
    assert resumed.json()["detail"]["code"] == "RUN_NOT_RESUMABLE"
    # 拒绝不产生任何业务结果。
    assert count_rows(scope.database_path, "refund_records") == 0
