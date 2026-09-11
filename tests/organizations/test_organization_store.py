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


def test_list_members_returns_all_memberships_of_organization(tmp_path):
    users, organizations = build_stores(tmp_path)
    alice = users.create_user(username="alice", password_hash="hash")
    bob = users.create_user(username="bob", password_hash="hash")
    carol = users.create_user(username="carol", password_hash="hash")
    other = users.create_user(username="other", password_hash="hash")
    organization = organizations.create_with_admin(
        name="Acme",
        admin_user_id=alice.user_id,
    )
    organizations.add_membership(
        organization_id=organization.organization_id,
        user_id=bob.user_id,
        role=MembershipRole.AGENT,
    )
    organizations.add_membership(
        organization_id=organization.organization_id,
        user_id=carol.user_id,
        role=MembershipRole.ADMIN,
    )
    # 另一个企业的成员不得出现在本企业列表中（租户隔离）
    other_organization = organizations.create_with_admin(
        name="Other",
        admin_user_id=other.user_id,
    )

    members = organizations.list_members(
        organization_id=organization.organization_id,
    )

    assert {item.user_id for item in members} == {
        alice.user_id,
        bob.user_id,
        carol.user_id,
    }
    assert all(
        item.organization_id == organization.organization_id for item in members
    )
    assert other_organization.organization_id != organization.organization_id


def test_update_membership_role_changes_role(tmp_path):
    users, organizations = build_stores(tmp_path)
    alice = users.create_user(username="alice", password_hash="hash")
    bob = users.create_user(username="bob", password_hash="hash")
    organization = organizations.create_with_admin(
        name="Acme",
        admin_user_id=alice.user_id,
    )
    organizations.add_membership(
        organization_id=organization.organization_id,
        user_id=bob.user_id,
        role=MembershipRole.AGENT,
    )

    updated = organizations.update_membership_role(
        organization_id=organization.organization_id,
        user_id=bob.user_id,
        role=MembershipRole.ADMIN,
    )

    assert updated.role is MembershipRole.ADMIN
    assert organizations.get_membership(
        organization_id=organization.organization_id,
        user_id=bob.user_id,
    ).role is MembershipRole.ADMIN


def test_update_membership_role_missing_member_raises(tmp_path):
    # 边界情况：对不存在的成员改角色必须抛出 MembershipNotFoundError，
    # 而不是静默成功（HTTP 层据此返回 404）。
    users, organizations = build_stores(tmp_path)
    alice = users.create_user(username="alice", password_hash="hash")
    bob = users.create_user(username="bob", password_hash="hash")
    organization = organizations.create_with_admin(
        name="Acme",
        admin_user_id=alice.user_id,
    )

    with pytest.raises(MembershipNotFoundError):
        organizations.update_membership_role(
            organization_id=organization.organization_id,
            user_id=bob.user_id,
            role=MembershipRole.ADMIN,
        )


def test_remove_membership_deletes_only_target_membership(tmp_path):
    users, organizations = build_stores(tmp_path)
    alice = users.create_user(username="alice", password_hash="hash")
    bob = users.create_user(username="bob", password_hash="hash")
    organization = organizations.create_with_admin(
        name="Acme",
        admin_user_id=alice.user_id,
    )
    organizations.add_membership(
        organization_id=organization.organization_id,
        user_id=bob.user_id,
        role=MembershipRole.AGENT,
    )

    organizations.remove_membership(
        organization_id=organization.organization_id,
        user_id=bob.user_id,
    )

    with pytest.raises(MembershipNotFoundError):
        organizations.get_membership(
            organization_id=organization.organization_id,
            user_id=bob.user_id,
        )
    # 移除成员不应影响同企业其他成员
    assert organizations.get_membership(
        organization_id=organization.organization_id,
        user_id=alice.user_id,
    ).role is MembershipRole.ADMIN


def test_remove_membership_missing_member_raises(tmp_path):
    # 边界情况：移除不存在的成员必须报错，避免把「操作成功」误报给调用方。
    users, organizations = build_stores(tmp_path)
    alice = users.create_user(username="alice", password_hash="hash")
    bob = users.create_user(username="bob", password_hash="hash")
    organization = organizations.create_with_admin(
        name="Acme",
        admin_user_id=alice.user_id,
    )

    with pytest.raises(MembershipNotFoundError):
        organizations.remove_membership(
            organization_id=organization.organization_id,
            user_id=bob.user_id,
        )
