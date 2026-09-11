from dataclasses import dataclass

from app.organizations.base import (
    Membership,
    MembershipNotFoundError,
    MembershipRole,
    Organization,
    OrganizationAccess,
    OrganizationMember,
    OrganizationStore,
)
from app.users.base import UserStore


MIN_ORGANIZATION_NAME_LENGTH = 2
MAX_ORGANIZATION_NAME_LENGTH = 100


@dataclass(frozen=True)
class TenantContext:
    user_id: str
    organization_id: str
    role: MembershipRole


class InvalidOrganizationNameError(ValueError):
    pass


class OrganizationAccessDeniedError(LookupError):
    pass


class AdminRoleRequiredError(PermissionError):
    pass


class OrganizationMemberConflictError(ValueError):
    """成员管理中的状态冲突：admin 对自己执行改角色/移除。"""


SELF_MANAGEMENT_FORBIDDEN_MESSAGE = (
    "administrators cannot change or remove their own membership"
)


class OrganizationService:
    def __init__(
        self,
        *,
        organization_store: OrganizationStore,
        user_store: UserStore,
    ):
        self._organization_store = organization_store
        self._user_store = user_store

    @staticmethod
    def _normalize_name(name: str) -> str:
        normalized = name.strip()
        if not (
            MIN_ORGANIZATION_NAME_LENGTH
            <= len(normalized)
            <= MAX_ORGANIZATION_NAME_LENGTH
        ):
            raise InvalidOrganizationNameError(
                "organization name must contain 2-100 characters"
            )
        return normalized

    def create_organization(
        self,
        *,
        user_id: str,
        name: str,
    ) -> Organization:
        return self._organization_store.create_with_admin(
            name=self._normalize_name(name),
            admin_user_id=user_id,
        )

    def list_organizations(
        self,
        *,
        user_id: str,
    ) -> list[OrganizationAccess]:
        return self._organization_store.list_for_user(user_id=user_id)

    def get_tenant_context(
        self,
        *,
        user_id: str,
        organization_id: str,
    ) -> TenantContext:
        try:
            membership = self._organization_store.get_membership(
                organization_id=organization_id,
                user_id=user_id,
            )
        except MembershipNotFoundError as exc:
            raise OrganizationAccessDeniedError(
                "organization not found"
            ) from exc
        return TenantContext(
            user_id=user_id,
            organization_id=organization_id,
            role=membership.role,
        )

    def add_member(
        self,
        *,
        actor_user_id: str,
        organization_id: str,
        username: str,
        role: MembershipRole,
    ) -> Membership:
        self._require_organization_admin(
            actor_user_id=actor_user_id,
            organization_id=organization_id,
        )

        target_user = self._user_store.get_by_username( #寻找待添加进组织的成员是否存在
            username=username.strip().casefold()
        )
        return self._organization_store.add_membership(
            organization_id=organization_id,
            user_id=target_user.user_id,
            role=role,
        )

    def list_members(
        self,
        *,
        actor_user_id: str,
        organization_id: str,
    ) -> list[OrganizationMember]:
        """列出企业全部成员；企业内任何角色都可读（成员目录不是敏感信息）。"""
        self._require_organization_member(
            actor_user_id=actor_user_id,
            organization_id=organization_id,
        )
        memberships = self._organization_store.list_members(
            organization_id=organization_id
        )
        if not memberships:
            return []
        # 批量取用户名，避免逐个成员查询；用户被删除时用占位名兜底，不影响列表可用
        users = {
            user.user_id: user
            for user in self._user_store.get_by_ids(
                user_ids=[item.user_id for item in memberships]
            )
        }
        return [
            OrganizationMember(
                user_id=item.user_id,
                username=users[item.user_id].username
                if item.user_id in users
                else item.user_id,
                role=item.role,
                created_at=item.created_at,
            )
            for item in memberships
        ]

    def change_member_role(
        self,
        *,
        actor_user_id: str,
        organization_id: str,
        target_user_id: str,
        role: MembershipRole,
    ) -> Membership:
        """修改成员角色（仅 admin）。不允许对自己操作，保证企业始终留有管理员。"""
        self._require_organization_admin(
            actor_user_id=actor_user_id,
            organization_id=organization_id,
        )
        self._reject_self_management(
            actor_user_id=actor_user_id,
            target_user_id=target_user_id,
        )
        # 先确认目标成员存在（不存在 → MembershipNotFoundError → 404），
        # 再交给 Store 更新，避免把“成员不存在”与“角色未变化”混为一谈
        self._require_target_membership(
            organization_id=organization_id,
            target_user_id=target_user_id,
        )
        return self._organization_store.update_membership_role(
            organization_id=organization_id,
            user_id=target_user_id,
            role=role,
        )

    def remove_member(
        self,
        *,
        actor_user_id: str,
        organization_id: str,
        target_user_id: str,
    ) -> None:
        """移除成员（仅 admin）。同样禁止对自己操作；被移除者已产生的业务数据保留。"""
        self._require_organization_admin(
            actor_user_id=actor_user_id,
            organization_id=organization_id,
        )
        self._reject_self_management(
            actor_user_id=actor_user_id,
            target_user_id=target_user_id,
        )
        self._require_target_membership(
            organization_id=organization_id,
            target_user_id=target_user_id,
        )
        self._organization_store.remove_membership(
            organization_id=organization_id,
            user_id=target_user_id,
        )

    def _require_organization_member(
        self,
        *,
        actor_user_id: str,
        organization_id: str,
    ) -> TenantContext:
        """要求操作者是该企业成员，否则按“企业不存在”处理（防跨企业枚举）。"""
        return self.get_tenant_context(
            user_id=actor_user_id,
            organization_id=organization_id,
        )

    def _require_organization_admin(
        self,
        *,
        actor_user_id: str,
        organization_id: str,
    ) -> TenantContext:
        """要求操作者是该企业 admin：非成员伪装 404，成员但非 admin 报 403。"""
        context = self._require_organization_member(
            actor_user_id=actor_user_id,
            organization_id=organization_id,
        )
        if context.role is not MembershipRole.ADMIN:
            raise AdminRoleRequiredError("admin role required")
        return context

    @staticmethod
    def _reject_self_management(
        *,
        actor_user_id: str,
        target_user_id: str,
    ) -> None:
        """禁止 admin 修改或移除自己的成员关系。

        这样即使企业存在多个 admin，也始终至少保留一名 admin（唯一 admin 无法
        自我降级/退出；多个 admin 时相互操作的下限也由该规则兜住），
        不会出现企业无人可管理的状态。
        """
        if actor_user_id == target_user_id:
            raise OrganizationMemberConflictError(
                SELF_MANAGEMENT_FORBIDDEN_MESSAGE
            )

    def _require_target_membership(
        self,
        *,
        organization_id: str,
        target_user_id: str,
    ) -> Membership:
        """目标用户必须是本企业成员，否则抛出 MembershipNotFoundError（404）。"""
        return self._organization_store.get_membership(
            organization_id=organization_id,
            user_id=target_user_id,
        )
