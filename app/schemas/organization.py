from pydantic import BaseModel, ConfigDict, Field

from app.organizations.base import (
    Membership,
    MembershipRole,
    Organization,
    OrganizationAccess,
)


class CreateOrganizationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)


class AddMemberRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=3, max_length=32)
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
