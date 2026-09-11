from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class MembershipRole(str, Enum):
    ADMIN = "admin"  # 企业管理员：可管理成员，并可执行知识库写入、审批决定与 Run 恢复
    AGENT = "agent"  # 客服：只能读写业务会话与企业内只读资源，不能执行管理类写操作


@dataclass(frozen=True)
class Organization:
    organization_id: str  # 企业唯一标识（uuid4 字符串），前端用作 X-Organization-ID
    name: str  # 企业名称，去首尾空白后 2–100 个字符
    created_at: str  # 企业创建时间（UTC isoformat 文本）


@dataclass(frozen=True)
class Membership:
    organization_id: str  # 成员关系所属的企业标识
    user_id: str  # 成员的用户标识
    role: MembershipRole  # 该用户在此企业的角色
    created_at: str  # 成为该企业成员的时间（UTC 文本，用于成员列表展示与排序）


@dataclass(frozen=True)
class OrganizationAccess:
    organization_id: str  # 企业唯一标识
    name: str  # 企业名称
    role: MembershipRole  # 当前用户在该企业的角色


@dataclass(frozen=True)
class OrganizationMember:
    """企业成员视图：成员关系 + 展示所需的用户名（不含任何凭据字段）。"""

    user_id: str  # 成员用户标识，同时作为改角色/移除接口的路径参数
    username: str  # 成员用户名（仅用于展示与重名区分）
    role: MembershipRole  # 该成员在此企业的角色
    created_at: str  # 加入企业的时间（UTC 文本）


class MembershipNotFoundError(LookupError):
    pass


class MembershipAlreadyExistsError(ValueError):
    pass


class OrganizationStore(Protocol):
    def create_with_admin(
        self,
        *,
        name: str,
        admin_user_id: str,
    ) -> Organization:
        raise NotImplementedError

    def list_for_user(self, *, user_id: str) -> list[OrganizationAccess]:
        raise NotImplementedError

    def get_membership(
        self,
        *,
        organization_id: str,
        user_id: str,
    ) -> Membership:
        raise NotImplementedError

    def add_membership(
        self,
        *,
        organization_id: str,
        user_id: str,
        role: MembershipRole,
    ) -> Membership:
        raise NotImplementedError

    def list_members(
        self,
        *,
        organization_id: str,
    ) -> list[Membership]:
        raise NotImplementedError

    def update_membership_role(
        self,
        *,
        organization_id: str,
        user_id: str,
        role: MembershipRole,
    ) -> Membership:
        raise NotImplementedError

    def remove_membership(
        self,
        *,
        organization_id: str,
        user_id: str,
    ) -> None:
        raise NotImplementedError
