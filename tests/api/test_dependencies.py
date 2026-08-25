from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.api.dependencies import create_current_tenant_dependency
from app.application.organization_service import (
    OrganizationService,
    TenantContext,
)
from app.organizations.sqlite_store import SQLiteOrganizationStore
from app.users.sqlite_store import SQLiteUserStore


def build_client(tmp_path, *, member: bool):
    database_path = tmp_path / "app.db"
    users = SQLiteUserStore(database_path)
    organizations = SQLiteOrganizationStore(database_path)
    service = OrganizationService(
        organization_store=organizations,
        user_store=users,
    )
    alice = users.create_user(username="alice", password_hash="hash")
    bob = users.create_user(username="bob", password_hash="hash")
    organization = service.create_organization(
        user_id=alice.user_id,
        name="Acme",
    )
    current_user = alice if member else bob

    def get_current_user():
        return current_user

    get_current_tenant = create_current_tenant_dependency(
        organization_service=service,
        get_current_user=get_current_user,
    )
    app = FastAPI()

    @app.get("/tenant")
    def tenant(context: TenantContext = Depends(get_current_tenant)):
        return {
            "user_id": context.user_id,
            "organization_id": context.organization_id,
            "role": context.role,
        }

    return TestClient(app), organization


def test_member_header_builds_tenant_context(tmp_path):
    client, organization = build_client(tmp_path, member=True)

    response = client.get(
        "/tenant",
        headers={"X-Organization-ID": organization.organization_id},
    )

    assert response.status_code == 200
    assert response.json()["organization_id"] == organization.organization_id
    assert response.json()["role"] == "admin"


def test_missing_or_blank_header_returns_400(tmp_path):
    client, _ = build_client(tmp_path, member=True)

    missing = client.get("/tenant")
    blank = client.get(
        "/tenant",
        headers={"X-Organization-ID": "   "},
    )

    assert missing.status_code == 400
    assert blank.status_code == 400
    assert missing.json() == {"detail": "organization context required"}


def test_non_member_receives_hidden_404(tmp_path):
    client, organization = build_client(tmp_path, member=False)

    response = client.get(
        "/tenant",
        headers={"X-Organization-ID": organization.organization_id},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "organization not found"}
