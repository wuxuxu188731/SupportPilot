import pytest

from app.application.organization_service import (
    AdminRoleRequiredError,
    InvalidOrganizationNameError,
    OrganizationAccessDeniedError,
    OrganizationMemberConflictError,
    OrganizationService,
)
from app.organizations.base import MembershipNotFoundError, MembershipRole
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


def test_list_members_returns_username_and_role_for_every_member(tmp_path):
    service, users = build_service(tmp_path)
    alice = users.create_user(username="alice", password_hash="hash")
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

    members = service.list_members(
        actor_user_id=bob.user_id,
        organization_id=organization.organization_id,
    )
    by_user = {item.user_id: item for item in members}

    # 保护行为：成员列表带用户名（供页面展示），管理员与客服的加入顺序无关
    assert by_user[alice.user_id].username == "alice"
    assert by_user[alice.user_id].role is MembershipRole.ADMIN
    assert by_user[bob.user_id].username == "bob"
    assert by_user[bob.user_id].role is MembershipRole.AGENT
    assert all(item.created_at for item in members)


def test_agent_can_read_member_list_but_not_mutate(tmp_path):
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
    service.add_member(
        actor_user_id=alice.user_id,
        organization_id=organization.organization_id,
        username=charlie.username,
        role=MembershipRole.AGENT,
    )

    # 读：agent 可看同事目录（与审批列表同为「企业内成员可读」）
    members = service.list_members(
        actor_user_id=bob.user_id,
        organization_id=organization.organization_id,
    )
    assert len(members) == 3

    # 写：agent 改角色 / 移除成员都必须被拒绝
    with pytest.raises(AdminRoleRequiredError):
        service.change_member_role(
            actor_user_id=bob.user_id,
            organization_id=organization.organization_id,
            target_user_id=charlie.user_id,
            role=MembershipRole.ADMIN,
        )
    with pytest.raises(AdminRoleRequiredError):
        service.remove_member(
            actor_user_id=bob.user_id,
            organization_id=organization.organization_id,
            target_user_id=charlie.user_id,
        )


def test_non_member_cannot_read_member_list(tmp_path):
    service, users = build_service(tmp_path)
    alice = users.create_user(username="alice", password_hash="hash")
    bob = users.create_user(username="bob", password_hash="hash")
    organization = service.create_organization(
        user_id=alice.user_id,
        name="Acme",
    )

    with pytest.raises(OrganizationAccessDeniedError):
        service.list_members(
            actor_user_id=bob.user_id,
            organization_id=organization.organization_id,
        )


def test_admin_can_change_other_member_role(tmp_path):
    service, users = build_service(tmp_path)
    alice = users.create_user(username="alice", password_hash="hash")
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

    updated = service.change_member_role(
        actor_user_id=alice.user_id,
        organization_id=organization.organization_id,
        target_user_id=bob.user_id,
        role=MembershipRole.ADMIN,
    )

    assert updated.role is MembershipRole.ADMIN
    assert service.get_tenant_context(
        user_id=bob.user_id,
        organization_id=organization.organization_id,
    ).role is MembershipRole.ADMIN


def test_admin_cannot_change_own_role_or_remove_self(tmp_path):
    # 保护行为：禁止 admin 对自己改角色/移除，确保企业始终至少保留一名管理员
    # （唯一管理员无法自我降级或退出企业）。
    service, users = build_service(tmp_path)
    alice = users.create_user(username="alice", password_hash="hash")
    organization = service.create_organization(
        user_id=alice.user_id,
        name="Acme",
    )

    with pytest.raises(OrganizationMemberConflictError):
        service.change_member_role(
            actor_user_id=alice.user_id,
            organization_id=organization.organization_id,
            target_user_id=alice.user_id,
            role=MembershipRole.AGENT,
        )
    with pytest.raises(OrganizationMemberConflictError):
        service.remove_member(
            actor_user_id=alice.user_id,
            organization_id=organization.organization_id,
            target_user_id=alice.user_id,
        )
    # 冲突被拒绝后成员关系保持不变
    assert service.get_tenant_context(
        user_id=alice.user_id,
        organization_id=organization.organization_id,
    ).role is MembershipRole.ADMIN


def test_change_role_or_remove_unknown_member_raises_not_found(tmp_path):
    # 边界情况：目标用户不是本企业成员时（含跨企业 id），统一报成员不存在，
    # 由 HTTP 层映射为 404，不泄露对方是否存在于其它企业。
    service, users = build_service(tmp_path)
    alice = users.create_user(username="alice", password_hash="hash")
    bob = users.create_user(username="bob", password_hash="hash")
    organization = service.create_organization(
        user_id=alice.user_id,
        name="Acme",
    )

    with pytest.raises(MembershipNotFoundError):
        service.change_member_role(
            actor_user_id=alice.user_id,
            organization_id=organization.organization_id,
            target_user_id=bob.user_id,
            role=MembershipRole.AGENT,
        )
    with pytest.raises(MembershipNotFoundError):
        service.remove_member(
            actor_user_id=alice.user_id,
            organization_id=organization.organization_id,
            target_user_id=bob.user_id,
        )


def test_remove_member_revokes_tenant_access(tmp_path):
    # 保护行为：移除成员后，该用户立即失去企业上下文（后续租户请求按 404 处理）。
    service, users = build_service(tmp_path)
    alice = users.create_user(username="alice", password_hash="hash")
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

    service.remove_member(
        actor_user_id=alice.user_id,
        organization_id=organization.organization_id,
        target_user_id=bob.user_id,
    )

    with pytest.raises(OrganizationAccessDeniedError):
        service.get_tenant_context(
            user_id=bob.user_id,
            organization_id=organization.organization_id,
        )
    assert service.list_organizations(user_id=bob.user_id) == []
