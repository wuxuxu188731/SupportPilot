from pydantic import BaseModel, ConfigDict, Field

from app.organizations.base import (
    Membership,
    MembershipRole,
    Organization,
    OrganizationAccess,
    OrganizationMember,
)


class CreateOrganizationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)


class AddMemberRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=3, max_length=32)
    role: MembershipRole


class UpdateMemberRoleRequest(BaseModel):
    """修改成员角色请求体：只允许改角色，成员由路径参数 user_id 指定。"""

    model_config = ConfigDict(extra="forbid")
    role: MembershipRole


class OrganizationResponse(BaseModel):
    organization_id: str
    name: str

    @classmethod
    def from_domain(
        cls,
        organization: Organization,
    ) -> "OrganizationResponse":
        return cls(
            organization_id=organization.organization_id,
            name=organization.name,
        )


class OrganizationAccessResponse(BaseModel):
    organization_id: str
    name: str
    role: MembershipRole

    @classmethod
    def from_domain(
        cls,
        access: OrganizationAccess,
    ) -> "OrganizationAccessResponse":
        return cls(
            organization_id=access.organization_id,
            name=access.name,
            role=access.role,
        )


class MembershipResponse(BaseModel):
    organization_id: str
    user_id: str
    role: MembershipRole

    @classmethod
    def from_domain(
        cls,
        membership: Membership,
    ) -> "MembershipResponse":
        return cls(
            organization_id=membership.organization_id,
            user_id=membership.user_id,
            role=membership.role,
        )


class OrganizationMemberResponse(BaseModel):
    """成员列表元素：成员关系 + 用户名；不含密码哈希等任何凭据字段。"""

    user_id: str
    username: str
    role: MembershipRole
    created_at: str

    @classmethod
    def from_domain(
        cls,
        member: OrganizationMember,
    ) -> "OrganizationMemberResponse":
        return cls(
            user_id=member.user_id,
            username=member.username,
            role=member.role,
            created_at=member.created_at,
        )
