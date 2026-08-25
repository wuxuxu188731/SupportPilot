from pydantic import BaseModel,Field,ConfigDict

from app.application.auth_service import LoginResult
from app.users.base import User

class RegisterRequest(BaseModel):
  model_config = ConfigDict(extra="forbid")

  username: str = Field(min_length=3, max_length=32)
  password: str = Field(min_length=8, max_length=72)

class LoginRequest(BaseModel):
  model_config = ConfigDict(extra="forbid")

  username: str = Field(min_length=3, max_length=32)
  password: str = Field(min_length=8, max_length=72)

class UserResponse(BaseModel):
  user_id: str
  username: str
  created_at: str

  @classmethod
  def from_user(cls, user: User) -> "UserResponse":
    return cls(
      user_id = user.user_id,
      username = user.username,
      created_at = user.created_at
    )

class TokenResponse(BaseModel):
  access_token: str
  token_type: str
  expires_in: int

  @classmethod
  def from_result(cls, login_result: LoginResult) -> "TokenResponse":
    return cls(
      access_token = login_result.access_token,
      token_type = login_result.token_type,
      expires_in = login_result.expire_in
    )