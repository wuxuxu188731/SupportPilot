import pytest

from app.application.organization_service import (
    AdminRoleRequiredError,
    InvalidOrganizationNameError,
    OrganizationAccessDeniedError,
    OrganizationService,
)
from app.organizations.base import MembershipRole
from app.organizations.sqlite_store import SQLiteOrganizationStore
from app.users.sqlite_store import SQLiteUserStore


def build_service(tmp_path):
    database_path = tmp_path / "app.db"
    users = SQLiteUserStore(database_path)
    organizations = SQLiteOrganizationStore(database_path)
    return OrganizationService(
        organization_store=organizations,
        user_store=users,
    ), users


def test_create_normalizes_name_and_grants_admin_role(tmp_path):
    service, users = build_service(tmp_path)
    alice = users.create_user(username="alice", password_hash="hash")

    organization = service.create_organization(
        user_id=alice.user_id,
        name="  Acme Support  ",
    )
    context = service.get_tenant_context(
        user_id=alice.user_id,
        organization_id=organization.organization_id,
    )

    assert organization.name == "Acme Support"
    assert context.role is MembershipRole.ADMIN


@pytest.mark.parametrize("name", [" ", "A", "x" * 101])
def test_create_rejects_invalid_name(tmp_path, name):
    service, users = build_service(tmp_path)
    alice = users.create_user(username="alice", password_hash="hash")

    with pytest.raises(InvalidOrganizationNameError):
        service.create_organization(user_id=alice.user_id, name=name)


def test_non_member_cannot_obtain_tenant_context(tmp_path):
    service, users = build_service(tmp_path)
    alice = users.create_user(username="alice", password_hash="hash")
    bob = users.create_user(username="bob", password_hash="hash")
    organization = service.create_organization(
        user_id=alice.user_id,
        name="Acme",
    )

    with pytest.raises(OrganizationAccessDeniedError):
        service.get_tenant_context(
            user_id=bob.user_id,
            organization_id=organization.organization_id,
        )


def test_admin_can_add_existing_user_as_agent(tmp_path):
    service, users = build_service(tmp_path)
    alice = users.create_user(username="alice", password_hash="hash")
    bob = users.create_user(username="bob", password_hash="hash")
    organization = service.create_organization(
        user_id=alice.user_id,
        name="Acme",
    )

    membership = service.add_member(
        actor_user_id=alice.user_id,
        organization_id=organization.organization_id,
        username=bob.username,
        role=MembershipRole.AGENT,
    )

    assert membership.user_id == bob.user_id
    assert membership.role is MembershipRole.AGENT


def test_agent_cannot_add_another_member(tmp_path):
    service, users = build_service(tmp_path)
    alice = users.create_user(username="alice", password_hash="hash")
    bob = users.create_user(username="bob", password_hash="hash")
    charlie = users.create_user(username="charlie", password_hash="hash")
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

    with pytest.raises(AdminRoleRequiredError):
        service.add_member(
            actor_user_id=bob.user_id,
            organization_id=organization.organization_id,
            username=charlie.username,
            role=MembershipRole.AGENT,
        )


def test_non_member_add_member_hides_organization(tmp_path):
    service, users = build_service(tmp_path)
    alice = users.create_user(username="alice", password_hash="hash")
    bob = users.create_user(username="bob", password_hash="hash")
    organization = service.create_organization(
        user_id=alice.user_id,
        name="Acme",
    )

    with pytest.raises(OrganizationAccessDeniedError):
        service.add_member(
            actor_user_id=bob.user_id,
            organization_id=organization.organization_id,
            username=alice.username,
            role=MembershipRole.AGENT,
        )
