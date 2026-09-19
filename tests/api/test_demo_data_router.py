import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.demo_data_router import create_demo_data_router
from app.application.organization_service import TenantContext
from app.customers.sqlite_store import SQLiteCustomerStore
from app.demo_data.seeder import DemoDataSeeder
from app.orders.base import OrderNotFoundError
from app.orders.sqlite_store import SQLiteOrderStore
from app.organizations.base import MembershipRole
from app.organizations.sqlite_store import SQLiteOrganizationStore
from app.shipments.sqlite_store import SQLiteShipmentStore
from app.tickets.sqlite_store import SQLiteTicketStore
from app.users.sqlite_store import SQLiteUserStore


def build_client(tmp_path):
    database_path = tmp_path / "app.db"
    users = SQLiteUserStore(database_path)
    organizations = SQLiteOrganizationStore(database_path)
    admin = users.create_user(username="alice", password_hash="hash")
    agent = users.create_user(username="bob", password_hash="hash")
    organization = organizations.create_with_admin(
        name="Acme Support",
        admin_user_id=admin.user_id,
    )
    organizations.add_membership(
        organization_id=organization.organization_id,
        user_id=agent.user_id,
        role=MembershipRole.AGENT,
    )
    seeder = DemoDataSeeder(
        organization_store=organizations,
        customer_store=SQLiteCustomerStore(database_path),
        order_store=SQLiteOrderStore(database_path),
        shipment_store=SQLiteShipmentStore(database_path),
        ticket_store=SQLiteTicketStore(database_path),
    )
    tenants = {
        "admin": TenantContext(
            user_id=admin.user_id,
            organization_id=organization.organization_id,
            role=MembershipRole.ADMIN,
        ),
        "agent": TenantContext(
            user_id=agent.user_id,
            organization_id=organization.organization_id,
            role=MembershipRole.AGENT,
        ),
    }
    current = {"name": "admin"}

    def get_current_tenant():
        return tenants[current["name"]]

    app = FastAPI()
    app.include_router(
        create_demo_data_router(
            seeder=seeder,
            get_current_tenant=get_current_tenant,
        )
    )
    return {
        "client": TestClient(app),
        "current": current,
        "orders": SQLiteOrderStore(database_path),
        "organization": organization,
        "database_path": database_path,
    }


def test_generate_demo_data_returns_counts(tmp_path):
    # 保护行为：管理员调用一次即生成整套演示数据，响应里带时间基准与
    # 各表新增行数，前端可据此区分「新增」与「已存在」。
    scope = build_client(tmp_path)

    response = scope["client"].post("/demo-data/")

    assert response.status_code == 201
    body = response.json()
    assert body["organization_id"] == (
        scope["organization"].organization_id
    )
    assert body["counts"] == {
        "customers": 2,
        "orders": 3,
        "shipments": 2,
        "tickets": 2,
        "ticket_comments": 3,
    }
    assert body["order_nos"] == [
        "ORD-DELAY-001",
        "ORD-TRANSIT-001",
        "ORD-DELIVERED-001",
    ]
    assert body["reference_at"]


def test_generate_demo_data_is_idempotent_and_refreshes_dates(tmp_path):
    # 保护行为：重复点击不重复建数据（新增计数为 0），但会把「会过期的日期」
    # 重新对齐到当前时间——这正是按钮每次都点得出的原因。
    scope = build_client(tmp_path)
    client = scope["client"]

    first = client.post("/demo-data/").json()
    second = client.post("/demo-data/")

    assert second.status_code == 201
    body = second.json()
    assert body["counts"] == {
        "customers": 0,
        "orders": 0,
        "shipments": 0,
        "tickets": 0,
        "ticket_comments": 0,
    }
    assert body["order_nos"] == first["order_nos"]

    delayed = scope["orders"].get_by_no(
        organization_id=scope["organization"].organization_id,
        order_no="ORD-DELAY-001",
    )
    # 承诺发货时间仍早于本次时间基准（路径 4/5/8/11/20/21 依赖这一点）。
    assert delayed.promised_ship_at < body["reference_at"]


def test_agent_cannot_generate_demo_data(tmp_path):
    # 边界：客服（agent）无权初始化企业级演示数据，必须得到 403，
    # 且不产生任何业务写入。
    scope = build_client(tmp_path)
    scope["current"]["name"] = "agent"

    response = scope["client"].post("/demo-data/")

    assert response.status_code == 403
    try:
        scope["orders"].get_by_no(
            organization_id=scope["organization"].organization_id,
            order_no="ORD-DELAY-001",
        )
    except OrderNotFoundError:
        pass
    else:
        raise AssertionError("agent 调用不应写入演示数据")


def test_generate_demo_data_reports_conflict(tmp_path):
    # 边界：演示订单被业务操作改过状态（例如已退款）时如实返回 409，
    # 不静默覆盖参与过审批的业务记录。
    scope = build_client(tmp_path)
    scope["client"].post("/demo-data/")
    connection = sqlite3.connect(scope["database_path"])
    try:
        with connection:
            connection.execute(
                """
                UPDATE orders
                SET status = 'refunded'
                WHERE organization_id = ?
                  AND order_no = 'ORD-DELIVERED-001'
                """,
                (scope["organization"].organization_id,),
            )
    finally:
        connection.close()

    response = scope["client"].post("/demo-data/")

    assert response.status_code == 409
    assert "ORD-DELIVERED-001" in response.json()["detail"]
