from dataclasses import dataclass

from app.organizations.base import (
    Membership,
    MembershipNotFoundError,
    MembershipRole,
    Organization,
    OrganizationAccess,
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
        context = self.get_tenant_context(
            user_id=actor_user_id,
            organization_id=organization_id,
        )
        if context.role is not MembershipRole.ADMIN:
            raise AdminRoleRequiredError("admin role required")

        target_user = self._user_store.get_by_username(
            username=username.strip().casefold()
        )
        return self._organization_store.add_membership(
            organization_id=organization_id,
            user_id=target_user.user_id,
            role=role,
        )
