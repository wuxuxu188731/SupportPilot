from collections.abc import Callable

from fastapi import Depends, HTTPException
from fastapi.security import(
  HTTPAuthorizationCredentials,
  HTTPBearer
)

from app.application.auth_service import AuthService,InvalidAuthenticationError
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
