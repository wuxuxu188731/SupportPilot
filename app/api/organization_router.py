from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException

from app.application.organization_service import (
    AdminRoleRequiredError,
    InvalidOrganizationNameError,
    OrganizationAccessDeniedError,
    OrganizationService,
)
from app.organizations.base import MembershipAlreadyExistsError
from app.schemas.organization import (
    AddMemberRequest,
    CreateOrganizationRequest,
    MembershipResponse,
    OrganizationAccessResponse,
    OrganizationResponse,
)
from app.users.base import User, UserNotFoundError


def create_organization_router(
    *,
    organization_service: OrganizationService,
    get_current_user: Callable[..., User],
) -> APIRouter:
    router = APIRouter(
        prefix="/organizations",
        tags=["organizations"],
    )

    @router.post("/", response_model=OrganizationResponse, status_code=201)
    def create_organization(
        request: CreateOrganizationRequest,
        current_user: User = Depends(get_current_user),
    ) -> OrganizationResponse:
        try:
            organization = organization_service.create_organization(
                user_id=current_user.user_id,
                name=request.name,
            )
        except InvalidOrganizationNameError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return OrganizationResponse.from_domain(organization)

    @router.get("/", response_model=list[OrganizationAccessResponse])
    def list_organizations(
        current_user: User = Depends(get_current_user),
    ) -> list[OrganizationAccessResponse]:
        result = organization_service.list_organizations(
            user_id=current_user.user_id
        )
        return [
            OrganizationAccessResponse.from_domain(item)
            for item in result
        ]

    @router.post(
        "/{organization_id}/members/",
        response_model=MembershipResponse,
        status_code=201,
    )
    def add_member(
        organization_id: str,
        request: AddMemberRequest,
        current_user: User = Depends(get_current_user),
    ) -> MembershipResponse:
        try:
            membership = organization_service.add_member(
                actor_user_id=current_user.user_id,
                organization_id=organization_id,
                username=request.username,
                role=request.role,
            )
        except OrganizationAccessDeniedError as exc:
            raise HTTPException(
                status_code=404,
                detail="organization not found",
            ) from exc
        except AdminRoleRequiredError as exc:
            raise HTTPException(
                status_code=403,
                detail="admin role required",
            ) from exc
        except UserNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail="user not found",
            ) from exc
        except MembershipAlreadyExistsError as exc:
            raise HTTPException(
                status_code=409,
                detail="user is already an organization member",
            ) from exc
        return MembershipResponse.from_domain(membership)

    return router
