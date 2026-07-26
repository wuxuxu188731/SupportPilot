from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class MembershipRole(str, Enum):
    ADMIN = "admin"
    AGENT = "agent"


@dataclass(frozen=True)
class Organization:
    organization_id: str
    name: str
    created_at: str


@dataclass(frozen=True)
class Membership:
    organization_id: str
    user_id: str
    role: MembershipRole
    created_at: str


@dataclass(frozen=True)
class OrganizationAccess:
    organization_id: str
    name: str
    role: MembershipRole


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
