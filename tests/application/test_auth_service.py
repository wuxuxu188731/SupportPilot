import pytest

from app.application.auth_service import (
    AuthService,
    InvalidAuthenticationError,
    InvalidCredentialsError,
    InvalidUsernameError,
)
from app.auth.tokens import AccessTokenService
from app.users.sqlite_store import SQLiteUserStore


SECRET = "test-secret-key-that-is-at-least-32-characters"


def build_service(tmp_path):
    store = SQLiteUserStore(tmp_path / "app.db")
    service = AuthService(
        user_store=store,
        token_service=AccessTokenService(
            secret_key=SECRET,
            ttl_seconds=1800,
        ),
    )
    return service, store


def test_register_normalizes_username_and_hashes_password(tmp_path):
    service, store = build_service(tmp_path)

    user = service.registry(
        username="  Alice_01  ",
        password="correct-horse-42",
    )

    assert user.username == "alice_01"
    stored = store.get_by_id(user_id=user.user_id)
    assert stored.password_hash != "correct-horse-42"


@pytest.mark.parametrize(
    "username",
    ["ab", "contains-space", "中文用户", "a" * 33],
)
def test_register_rejects_invalid_username(tmp_path, username):
    service, _ = build_service(tmp_path)

    with pytest.raises(InvalidUsernameError):
        service.registry(
            username=username,
            password="correct-horse-42",
        )


def test_login_returns_token_for_correct_password(tmp_path):
    service, _ = build_service(tmp_path)
    user = service.registry(
        username="alice",
        password="correct-horse-42",
    )

    result = service.login(
        username="ALICE",
        password="correct-horse-42",
    )

    assert result.token_type == "bearer"
    assert result.expire_in == 1800
    assert (
        service.get_user_from_token(token=result.access_token).user_id
        == user.user_id
    )

#参数名，待填充参数的值
@pytest.mark.parametrize(
    ("username", "password"),
    [
        ("missing", "correct-horse-42"),
        ("alice", "wrong-password"),
    ],
)
def test_login_uses_one_error_for_unknown_user_and_wrong_password(
    tmp_path,
    username,
    password,
):
    service, _ = build_service(tmp_path)
    service.registry(username="alice", password="correct-horse-42")

    with pytest.raises(InvalidCredentialsError):
        service.login(username=username, password=password)


def test_deleted_or_invalid_token_user_is_rejected(tmp_path):
    service, _ = build_service(tmp_path)

    with pytest.raises(InvalidAuthenticationError):
        service.get_user_from_token(token="not-a-jwt")