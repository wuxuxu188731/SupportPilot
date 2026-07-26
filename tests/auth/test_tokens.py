from datetime import datetime, timedelta, timezone

import jwt
import pytest

from app.auth.tokens import (
    AccessTokenService,
    InvalidAccessTokenError,
)


SECRET = "test-secret-key-that-is-at-least-32-characters"


def test_access_token_round_trip():
    service = AccessTokenService(
        secret_key=SECRET,
        ttl_seconds=1800,
    )

    token = service.issue(user_id="user-123")

    assert service.decode(token).user_id == "user-123"


#这个函数验证的是当payload里面的type不符合预期，或者payload里面的字段名不完整的时候会抛出异常InvalidAccessTokenError
@pytest.mark.parametrize( #让同一个测试函数针对多组不同的输入数据，分别独立运行
    "payload", #测试函数里面接收的不同参数
    [
        {"sub": "user-123", "type": "refresh"},
        {"type": "access"},
    ],
) #sub type iss iat exp
def test_rejects_wrong_type_or_missing_subject(payload):
    payload.update(
        {
            "iss": "supportpilot",
            "iat": datetime.now(timezone.utc),
            "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        }
    )
    token = jwt.encode(payload, SECRET, algorithm="HS256")
    service = AccessTokenService(secret_key=SECRET, ttl_seconds=1800)

    with pytest.raises(InvalidAccessTokenError):
        service.decode(token)


#测试token过期能否如期抛出异常InvalidAccessTokenError
def test_rejects_expired_or_tampered_token():
    service = AccessTokenService(secret_key=SECRET, ttl_seconds=1800)
    expired = jwt.encode(
        {
            "sub": "user-123",
            "type": "access",
            "iss": "supportpilot",
            "iat": datetime.now(timezone.utc) - timedelta(minutes=10), #令牌的发放时间比过期时间还晚
            "exp": datetime.now(timezone.utc) - timedelta(minutes=5),
        },
        SECRET,
        algorithm="HS256",
    )

    with pytest.raises(InvalidAccessTokenError):
        service.decode(expired)

    token = service.issue(user_id="user-123")
    with pytest.raises(InvalidAccessTokenError):
        service.decode(token + "tampered") #验证错误的token不能解析出正常的用户id