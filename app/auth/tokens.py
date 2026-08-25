from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt

ISSUER = "supportpilot"
ALGORITHM = "HS256"

@dataclass(frozen=True)
class AccessTokenClaims:
  user_id : str

class InvalidAccessTokenError(ValueError):
  pass

class AccessTokenService:

  def __init__(self, *, secret_key: str, ttl_seconds: int):
    if len(secret_key) < 32:
      raise ValueError("secret_key must contain at least 32 characters")
    if ttl_seconds < 0:
      raise ValueError("ttl_seconds must be positive")
    self._secret_key = secret_key
    self._ttl_seconds = ttl_seconds

  @property
  def ttl_seconds(self)->int:
    return self._ttl_seconds

  def issue(self, *, user_id: str)->str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
      {
        "sub": user_id,
        "type": "access",
        "iss": ISSUER,
        "iat": now,
        "exp": now + timedelta(seconds=self._ttl_seconds),
      }, #需要加密的数据
      self._secret_key,  #密钥
      algorithm=ALGORITHM  #加密算法
    )

  def decode(self,token:str)->AccessTokenClaims:
    try:
      payload = jwt.decode(
        token,
        self._secret_key,
        algorithms=[ALGORITHM],
        issuer=ISSUER,
        options={"require":["sub","type","iss","iat","exp"]}
      )
    except jwt.InvalidTokenError as exc:
      raise InvalidAccessTokenError("invalid access token") from exc

    if payload["type"] != "access":
      raise InvalidAccessTokenError("invalid access token type")
    user_id = payload["sub"]
    if not isinstance(user_id,str) or not user_id:
      raise InvalidAccessTokenError("invalid access token subject")
    return AccessTokenClaims(user_id=user_id)