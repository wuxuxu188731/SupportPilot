from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.organization_router import create_organization_router
from app.application.organization_service import OrganizationService
from app.organizations.base import MembershipRole
from app.organizations.sqlite_store import SQLiteOrganizationStore
from app.users.base import User
from app.users.sqlite_store import SQLiteUserStore


CURRENT_USER = User(
    user_id="authenticated-user",
    username="alice",
    password_hash="hash",
    created_at="2026-07-26 00:00:00",
)


def build_client(tmp_path):
    database_path = tmp_path / "app.db"
    users = SQLiteUserStore(database_path)
    users.create_user(
        username=CURRENT_USER.username,
        password_hash=CURRENT_USER.password_hash,
    )
    stored_alice = users.get_by_username(username="alice")
    current_user = User(
        user_id=stored_alice.user_id,
        username=stored_alice.username,
        password_hash=stored_alice.password_hash,
        created_at=stored_alice.created_at,
    )
    service = OrganizationService(
        organization_store=SQLiteOrganizationStore(database_path),
        user_store=users,
    )

    def get_current_user():
        return current_user

    app = FastAPI()
    app.include_router(
        create_organization_router(
            organization_service=service,
            get_current_user=get_current_user,
        )
    )
    return TestClient(app), service, users, current_user


def test_create_and_list_organizations(tmp_path):
    client, _, _, _ = build_client(tmp_path)

    created = client.post(
        "/organizations/",
        json={"name": "  Acme Support  "},
    )
    listed = client.get("/organizations/")

    assert created.status_code == 201
    assert created.json()["name"] == "Acme Support"
    assert listed.status_code == 200
    assert listed.json() == [
        {
            "organization_id": created.json()["organization_id"],
            "name": "Acme Support",
            "role": "admin",
        }
    ]


def test_admin_can_add_member_and_duplicate_returns_409(tmp_path):
    client, _, users, _ = build_client(tmp_path)
    users.create_user(username="bob", password_hash="hash")
    organization_id = client.post(
        "/organizations/",
        json={"name": "Acme"},
    ).json()["organization_id"]

    first = client.post(
        f"/organizations/{organization_id}/members/",
        json={"username": "bob", "role": "agent"},
    )
    duplicate = client.post(
        f"/organizations/{organization_id}/members/",
        json={"username": "bob", "role": "agent"},
    )

    assert first.status_code == 201
    assert first.json()["role"] == "agent"
    assert duplicate.status_code == 409
    assert duplicate.json() == {
        "detail": "user is already an organization member"
    }


def test_unknown_member_username_returns_404(tmp_path):
    client, _, _, _ = build_client(tmp_path)
    organization_id = client.post(
        "/organizations/",
        json={"name": "Acme"},
    ).json()["organization_id"]

    response = client.post(
        f"/organizations/{organization_id}/members/",
        json={"username": "missing", "role": "agent"},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "user not found"}


def test_agent_cannot_add_member(tmp_path):
    client, service, users, alice = build_client(tmp_path)
    bob = users.create_user(username="bob", password_hash="hash")
    users.create_user(username="charlie", password_hash="hash")
    organization = service.create_organization(
        user_id=alice.user_id,
        name="Acme",
    )
    service.add_member(
        actor_user_id=alice.user_id,
        organization_id=organization.organization_id,
        username=bob.username,
        role=MembershipRole.AGENT,
    )

    app = FastAPI()

    def get_bob():
        return bob

    app.include_router(
        create_organization_router(
            organization_service=service,
            get_current_user=get_bob,
        )
    )
    response = TestClient(app).post(
        f"/organizations/{organization.organization_id}/members/",
        json={"username": "charlie", "role": "agent"},
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "admin role required"}


def build_admin_scenario(tmp_path):
    """构造「alice 为 admin、bob 为 agent」的场景，返回客户端与关键 id。"""
    client, service, users, alice = build_client(tmp_path)
    bob = users.create_user(username="bob", password_hash="hash")
    organization = service.create_organization(
        user_id=alice.user_id,
        name="Acme",
    )
    service.add_member(
        actor_user_id=alice.user_id,
        organization_id=organization.organization_id,
        username=bob.username,
        role=MembershipRole.AGENT,
    )
    return client, service, users, alice, bob, organization


def test_list_members_returns_usernames_without_credentials(tmp_path):
    client, _, _, alice, bob, organization = build_admin_scenario(tmp_path)

    response = client.get(f"/organizations/{organization.organization_id}/members/")

    assert response.status_code == 200
    body = response.json()
    assert {(item["user_id"], item["username"], item["role"]) for item in body} == {
        (alice.user_id, "alice", "admin"),
        (bob.user_id, "bob", "agent"),
    }
    # 安全边界：成员对象不得携带密码哈希等凭据字段
    assert all("password_hash" not in item for item in body)
    assert all(item["created_at"] for item in body)


def test_agent_can_read_member_list(tmp_path):
    # 保护行为：成员目录对企业内所有角色可读（与审批列表口径一致）。
    client, service, _, _, bob, organization = build_admin_scenario(tmp_path)
    app = FastAPI()

    def get_bob():
        return bob

    app.include_router(
        create_organization_router(
            organization_service=service,
            get_current_user=get_bob,
        )
    )

    response = TestClient(app).get(
        f"/organizations/{organization.organization_id}/members/"
    )

    assert response.status_code == 200
    assert len(response.json()) == 2


def test_non_member_cannot_read_member_list(tmp_path):
    # 边界情况：非成员读成员列表按「企业不存在」处理（404，防跨企业枚举）。
    client, service, users, _, _, organization = build_admin_scenario(tmp_path)
    stranger = users.create_user(username="stranger", password_hash="hash")
    app = FastAPI()

    def get_stranger():
        return stranger

    app.include_router(
        create_organization_router(
            organization_service=service,
            get_current_user=get_stranger,
        )
    )

    response = TestClient(app).get(
        f"/organizations/{organization.organization_id}/members/"
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "organization not found"}


def test_admin_can_change_member_role(tmp_path):
    client, service, _, _, bob, organization = build_admin_scenario(tmp_path)

    response = client.patch(
        f"/organizations/{organization.organization_id}/members/{bob.user_id}/",
        json={"role": "admin"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "organization_id": organization.organization_id,
        "user_id": bob.user_id,
        "role": "admin",
    }
    assert service.get_tenant_context(
        user_id=bob.user_id,
        organization_id=organization.organization_id,
    ).role is MembershipRole.ADMIN


def test_admin_can_remove_member(tmp_path):
    client, service, _, _, bob, organization = build_admin_scenario(tmp_path)

    response = client.delete(
        f"/organizations/{organization.organization_id}/members/{bob.user_id}/"
    )

    assert response.status_code == 204
    assert response.content == b""
    assert service.list_organizations(user_id=bob.user_id) == []


def test_agent_cannot_change_role_or_remove_member(tmp_path):
    # 保护行为：写操作仅 admin；agent 一律 403（前端隐藏按钮只是体验优化）。
    client, service, users, alice, bob, organization = build_admin_scenario(tmp_path)
    carol = users.create_user(username="carol", password_hash="hash")
    service.add_member(
        actor_user_id=alice.user_id,
        organization_id=organization.organization_id,
        username=carol.username,
        role=MembershipRole.AGENT,
    )
    app = FastAPI()

    def get_bob():
        return bob

    app.include_router(
        create_organization_router(
            organization_service=service,
            get_current_user=get_bob,
        )
    )
    agent_client = TestClient(app)

    patched = agent_client.patch(
        f"/organizations/{organization.organization_id}/members/{carol.user_id}/",
        json={"role": "admin"},
    )
    removed = agent_client.delete(
        f"/organizations/{organization.organization_id}/members/{carol.user_id}/"
    )

    assert patched.status_code == 403
    assert patched.json() == {"detail": "admin role required"}
    assert removed.status_code == 403
    assert removed.json() == {"detail": "admin role required"}
    assert len(service.list_organizations(user_id=carol.user_id)) == 1


def test_admin_cannot_change_own_role_or_remove_self(tmp_path):
    client, _, _, alice, _, organization = build_admin_scenario(tmp_path)

    patched = client.patch(
        f"/organizations/{organization.organization_id}/members/{alice.user_id}/",
        json={"role": "agent"},
    )
    removed = client.delete(
        f"/organizations/{organization.organization_id}/members/{alice.user_id}/"
    )

    assert patched.status_code == 409
    assert patched.json() == {
        "detail": "administrators cannot change their own role"
    }
    assert removed.status_code == 409
    assert removed.json() == {
        "detail": "administrators cannot remove their own membership"
    }


def test_unknown_member_id_returns_404(tmp_path):
    client, _, _, _, _, organization = build_admin_scenario(tmp_path)

    patched = client.patch(
        f"/organizations/{organization.organization_id}/members/missing-user/",
        json={"role": "admin"},
    )
    removed = client.delete(
        f"/organizations/{organization.organization_id}/members/missing-user/"
    )

    assert patched.status_code == 404
    assert patched.json() == {"detail": "organization member not found"}
    assert removed.status_code == 404
    assert removed.json() == {"detail": "organization member not found"}


def test_member_endpoints_reject_unknown_role(tmp_path):
    # 边界情况：role 只接受 admin/agent，其它取值由请求模型拦截为 422。
    client, _, _, _, bob, organization = build_admin_scenario(tmp_path)

    response = client.patch(
        f"/organizations/{organization.organization_id}/members/{bob.user_id}/",
        json={"role": "owner"},
    )

    assert response.status_code == 422
