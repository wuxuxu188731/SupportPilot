import pytest

from app.organizations.base import (
    MembershipAlreadyExistsError,
    MembershipNotFoundError,
    MembershipRole,
)
from app.organizations.sqlite_store import SQLiteOrganizationStore
from app.users.sqlite_store import SQLiteUserStore


def build_stores(tmp_path):
    database_path = tmp_path / "app.db"
    return (
        SQLiteUserStore(database_path),
        SQLiteOrganizationStore(database_path),
    )


def test_create_organization_also_creates_admin_membership(tmp_path):
    users, organizations = build_stores(tmp_path)
    alice = users.create_user(username="alice", password_hash="hash")

    organization = organizations.create_with_admin(
        name="Acme Support",
        admin_user_id=alice.user_id,
    )

    membership = organizations.get_membership(
        organization_id=organization.organization_id,
        user_id=alice.user_id,
    )
    assert organization.name == "Acme Support"
    assert membership.role is MembershipRole.ADMIN


def test_list_for_user_returns_only_joined_organizations(tmp_path):
    users, organizations = build_stores(tmp_path)
    alice = users.create_user(username="alice", password_hash="hash")
    bob = users.create_user(username="bob", password_hash="hash")
    first = organizations.create_with_admin(
        name="Alice Company",
        admin_user_id=alice.user_id,
    )
    organizations.create_with_admin(
        name="Bob Company",
        admin_user_id=bob.user_id,
    )

    result = organizations.list_for_user(user_id=alice.user_id)

    assert [(item.organization_id, item.name, item.role) for item in result] == [
        (first.organization_id, "Alice Company", MembershipRole.ADMIN)
    ]


def test_add_membership_round_trip_and_duplicate_rejected(tmp_path):
    users, organizations = build_stores(tmp_path)
    alice = users.create_user(username="alice", password_hash="hash")
    bob = users.create_user(username="bob", password_hash="hash")
    organization = organizations.create_with_admin(
        name="Acme",
        admin_user_id=alice.user_id,
    )

    created = organizations.add_membership(
        organization_id=organization.organization_id,
        user_id=bob.user_id,
        role=MembershipRole.AGENT,
    )

    assert organizations.get_membership(
        organization_id=organization.organization_id,
        user_id=bob.user_id,
    ) == created
    with pytest.raises(MembershipAlreadyExistsError):
        organizations.add_membership(
            organization_id=organization.organization_id,
            user_id=bob.user_id,
            role=MembershipRole.ADMIN,
        )


def test_missing_membership_raises_domain_error(tmp_path):
    users, organizations = build_stores(tmp_path)
    alice = users.create_user(username="alice", password_hash="hash")
    bob = users.create_user(username="bob", password_hash="hash")
    organization = organizations.create_with_admin(
        name="Acme",
        admin_user_id=alice.user_id,
    )

    with pytest.raises(MembershipNotFoundError):
        organizations.get_membership(
            organization_id=organization.organization_id,
            user_id=bob.user_id,
        )
