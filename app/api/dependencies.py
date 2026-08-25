from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, Header, HTTPException
from fastapi.security import(
  HTTPAuthorizationCredentials,
  HTTPBearer
)

from app.application.auth_service import AuthService,InvalidAuthenticationError
from app.application.organization_service import (
  OrganizationAccessDeniedError,
  OrganizationService,
  TenantContext,
)
from app.users.base import User

bearer_scheme = HTTPBearer(auto_error=False)

def create_current_user_dependency(
  auth_service: AuthService,
)->Callable[..., User]:
  def get_current_user(credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme))->User:
    if credentials is None:
      raise HTTPException(
        status_code=401,
        detail="authentication required",
        headers={"WWW-Authenticate": "Bearer"}
      )
    try:
      return auth_service.get_user_from_token(token=credentials.credentials)
    except InvalidAuthenticationError as exc:
      raise HTTPException(
        status_code=401,
        detail="invalid or expired access token",
        headers={"WWW-Authenticate": "Bearer"}
      ) from exc

  return get_current_user


def create_current_tenant_dependency(
  *,
  organization_service: OrganizationService,
  get_current_user: Callable[..., User],
) -> Callable[..., TenantContext]:
  def get_current_tenant(
    current_user: User = Depends(get_current_user),
    organization_id: Annotated[
      str | None,
      Header(alias="X-Organization-ID"),
    ] = None,
  ) -> TenantContext:
    normalized = (organization_id or "").strip()
    if not normalized:
      raise HTTPException(
        status_code=400,
        detail="organization context required",
      )
    try:
      return organization_service.get_tenant_context(
        user_id=current_user.user_id,
        organization_id=normalized,
      )
    except OrganizationAccessDeniedError as exc:
      raise HTTPException(
        status_code=404,
        detail="organization not found",
      ) from exc

  return get_current_tenant
