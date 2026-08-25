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
