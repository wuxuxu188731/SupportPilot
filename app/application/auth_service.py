import re

from dataclasses import dataclass
from app.auth.passwords import hash_password, verify_password
from app.auth.tokens import AccessTokenService, InvalidAccessTokenError
from app.users.base import User, UserNotFoundError, UserStore

#必须匹配整个字符串（不能有多余字符）, 只允许小写字母、数字、下划线,长度必须在 3 到 32 个字符之间
USERNAME_PATTERN = re.compile(r"^[a-z0-9_]{3,32}$") #正则表达式，用于校验用户名格式
DUMMY_PASSWORD_HASH = (
  "$2b$12$C6UzMDM.H6dfI/f/IKcEe."
  "5YhZQxB4rNB/5G9Jd8M7sV3xB8H6G6K"
)#假的 bcrypt 密码哈希值，用来防止用户枚举攻击
"""
用法场景是这样的：当用户用一个不存在的用户名登录时，你仍然需要对"密码"做一次哈希比对操作，而不是直接返回"用户名不
  存在"。否则攻击者可以通过比较响应时间的差异，来判断哪些用户名是真实注册过的。
"""

@dataclass
class LoginResult:
  access_token : str
  token_type : str
  expire_in : int

class InvalidUsernameError(ValueError):
  pass

class InvalidCredentialsError(ValueError):
  pass

class InvalidAuthenticationError(ValueError):
  pass

class AuthService:
  def __init__(self, *, user_store: UserStore, token_service: AccessTokenService):
    self._user_store = user_store
    self._token_service = token_service

  @staticmethod 
  def _normalize_username(username: str) -> str:
    normalized = username.strip().casefold()
    if USERNAME_PATTERN.fullmatch(normalized) is None:
      raise InvalidUsernameError(
        "username must contain 3-32 lowercase letters, digits, or underscores"
      )
    return normalized

  def registry(self, *, username: str, password: str) -> User:
    username = self._normalize_username(username)
    return self._user_store.create_user(
      username=username,
      password_hash=hash_password(password)
    )
    
  def login(self, *, username: str, password: str) -> LoginResult:
    normalized = username.strip().casefold()
    try:
      user = self._user_store.get_by_username(username=normalized)
      password_hash = user.password_hash
    except UserNotFoundError as exc:
      user = None
      password_hash = DUMMY_PASSWORD_HASH
    
    password_is_valid = verify_password(password=password,password_hash=password_hash)
    if user is None or not password_is_valid:
      raise InvalidCredentialsError("invalid username or password")

    return LoginResult(
      access_token=self._token_service.issue(user_id=user.user_id),
      token_type="bearer",
      expire_in=self._token_service.ttl_seconds
    )

  def get_user_from_token(self, *, token) -> User:
    try:
      user_id = self._token_service.decode(token).user_id.strip().casefold()
      return self._user_store.get_by_id(user_id=user_id)
    except (InvalidAccessTokenError, UserNotFoundError) as exc:
      raise InvalidAuthenticationError("invalid or expired access token") from exc
    