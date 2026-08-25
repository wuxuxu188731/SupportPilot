from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException

from app.application.auth_service import (
    AuthService,
    InvalidCredentialsError,
    InvalidUsernameError,
)
from app.auth.passwords import PasswordPolicyError
from app.schemas.auth import (
    LoginRequest,
    RegisterRequest,
    TokenResponse,
    UserResponse,
)
from app.users.base import User, UserAlreadyExistError, UserNotFoundError

def create_auth_router(
  *,
  auth_service: AuthService,
  get_current_user: Callable[..., User]
)-> APIRouter:
  router = APIRouter(prefix="/auth", tags=["authentication"])

  @router.post("/register", response_model=UserResponse, status_code=201)
  def register(request: RegisterRequest) -> UserResponse:
    try:
      user = auth_service.registry(
        username=request.username, 
        password=request.password
      )
    except UserAlreadyExistError as exc:
      raise HTTPException(
        status_code=409,
        detail="username already exists"
      ) from exc
    except (InvalidUsernameError, PasswordPolicyError) as exc:
      raise HTTPException(
        status_code=422,
        detail=str(exc)
      )
    return UserResponse.from_user(user)

  @router.post("/login", response_model=TokenResponse)
  def login(request:LoginRequest)->TokenResponse:
    try:
      result = auth_service.login(
        username=request.username,
        password=request.password
      )
    except InvalidCredentialsError as exc:
      raise HTTPException(
        status_code=401,
        detail="invalid username or password",
        headers={"WWW-Authenticate": "Bearer"}
      )
    return TokenResponse.from_result(result)

  @router.get("/me", response_model=UserResponse)
  def me(current_user: User = Depends(get_current_user))->UserResponse:
    return UserResponse.from_user(current_user)
    #前端页面刷新后，用这个接口快速判断"用户是否还处于登录状态"

  return router