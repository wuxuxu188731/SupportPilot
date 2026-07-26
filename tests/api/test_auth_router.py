from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.auth_router import create_auth_router
from app.api.dependencies import create_current_user_dependency
from app.application.auth_service import AuthService
from app.auth.tokens import AccessTokenService
from app.users.sqlite_store import SQLiteUserStore


SECRET = "test-secret-key-that-is-at-least-32-characters"


def build_client(tmp_path):
    service = AuthService(
        user_store=SQLiteUserStore(tmp_path / "app.db"),
        token_service=AccessTokenService(
            secret_key=SECRET,
            ttl_seconds=1800,
        ),
    )
    get_current_user = create_current_user_dependency(service)
    app = FastAPI()
    app.include_router(
        create_auth_router(
            auth_service=service,
            get_current_user=get_current_user,
        )
    )
    return TestClient(app)


def register(client, username="alice", password="correct-horse-42"):
    return client.post(
        "/auth/register",
        json={"username": username, "password": password},
    )


def login(client, username="alice", password="correct-horse-42"):
    return client.post(
        "/auth/login",
        json={"username": username, "password": password},
    )

#测试正常注册流程的响应码是否正确，不返回password字段和password_hash字段
def test_register_returns_public_user_without_password(tmp_path):
    client = build_client(tmp_path)

    response = register(client)

    assert response.status_code == 201
    assert response.json()["username"] == "alice"
    assert "user_id" in response.json()
    assert "password" not in response.json()
    assert "password_hash" not in response.json()


#测试重复的注册返回409资源冲突
def test_duplicate_registration_returns_409(tmp_path):
    client = build_client(tmp_path)
    register(client)

    response = register(client, username="ALICE")

    assert response.status_code == 409
    assert response.json() == {"detail": "username already exists"}


#测试登录后返回的token里面的载荷payload是否符合预期
def test_login_and_me_round_trip(tmp_path):
    client = build_client(tmp_path)
    registered = register(client).json() #先注册一个用户

    login_response = login(client)  #登录刚刚注册的用户
    token_body = login_response.json()  
    me_response = client.get(   #获取登录后接口返回的token
        "/auth/me",
        headers={
            "Authorization": f"Bearer {token_body['access_token']}"
        },
    )

    assert login_response.status_code == 200
    assert token_body["token_type"] == "bearer"
    assert token_body["expires_in"] == 1800
    assert me_response.status_code == 200
    assert me_response.json() == registered


def test_login_does_not_reveal_whether_username_exists(tmp_path):
    client = build_client(tmp_path)
    register(client) #username="alice", password="correct-horse-42"

    wrong_password = login(client, password="wrong-password") #使用错误密码登录，
    missing_user = login(
        client,
        username="missing",
        password="wrong-password",
    ) #使用错误的用户名和密码进行登录

    assert wrong_password.status_code == 401
    assert missing_user.status_code == 401
    assert wrong_password.json() == missing_user.json()
    assert wrong_password.json() == {
        "detail": "invalid username or password"
    }


def test_me_rejects_missing_and_invalid_bearer_token(tmp_path):
    client = build_client(tmp_path)

    missing = client.get("/auth/me")
    invalid = client.get(
        "/auth/me",
        headers={"Authorization": "Bearer invalid-token"},
    )

    assert missing.status_code == 401
    assert invalid.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"