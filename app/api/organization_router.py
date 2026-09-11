from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, Response

from app.application.organization_service import (
    AdminRoleRequiredError,
    InvalidOrganizationNameError,
    OrganizationAccessDeniedError,
    OrganizationMemberConflictError,
    OrganizationService,
)
from app.organizations.base import (
    MembershipAlreadyExistsError,
    MembershipNotFoundError,
)
from app.schemas.organization import (
    AddMemberRequest,
    CreateOrganizationRequest,
    MembershipResponse,
    OrganizationAccessResponse,
    OrganizationMemberResponse,
    OrganizationResponse,
    UpdateMemberRoleRequest,
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

    @router.get(
        "/{organization_id}/members/",
        response_model=list[OrganizationMemberResponse],
    )
    def list_members(
        organization_id: str,
        current_user: User = Depends(get_current_user),
    ) -> list[OrganizationMemberResponse]:
        """成员列表：企业内成员均可读（agent 也能看到同事目录）。"""
        try:
            members = organization_service.list_members(
                actor_user_id=current_user.user_id,
                organization_id=organization_id,
            )
        except OrganizationAccessDeniedError as exc:
            raise HTTPException(
                status_code=404,
                detail="organization not found",
            ) from exc
        return [
            OrganizationMemberResponse.from_domain(member)
            for member in members
        ]

    @router.patch(
        "/{organization_id}/members/{user_id}/",
        response_model=MembershipResponse,
    )
    def update_member_role(
        organization_id: str,
        user_id: str,
        request: UpdateMemberRoleRequest,
        current_user: User = Depends(get_current_user),
    ) -> MembershipResponse:
        """修改成员角色（仅 admin）；不允许对自己操作。"""
        try:
            membership = organization_service.change_member_role(
                actor_user_id=current_user.user_id,
                organization_id=organization_id,
                target_user_id=user_id,
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
        except MembershipNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail="organization member not found",
            ) from exc
        except OrganizationMemberConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail="administrators cannot change their own role",
            ) from exc
        return MembershipResponse.from_domain(membership)

    @router.delete(
        "/{organization_id}/members/{user_id}/",
        status_code=204,
    )
    def remove_member(
        organization_id: str,
        user_id: str,
        current_user: User = Depends(get_current_user),
    ) -> Response:
        """移除成员（仅 admin）；不允许移除自己。"""
        try:
            organization_service.remove_member(
                actor_user_id=current_user.user_id,
                organization_id=organization_id,
                target_user_id=user_id,
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
        except MembershipNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail="organization member not found",
            ) from exc
        except OrganizationMemberConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail="administrators cannot remove their own membership",
            ) from exc
        return Response(status_code=204)

    return router
