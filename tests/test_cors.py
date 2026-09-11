"""CORS 装配与配置读取的测试。

保护行为：
- 未配置 CORS_ALLOW_ORIGINS 时默认放行本机前端开发服务器来源；
- 预检请求（OPTIONS）返回正确的允许方法/允许头，且包含多租户必需的
  X-Organization-ID 与 Authorization；
- 未列入白名单的来源不会拿到 Allow-Origin 响应头；
- 环境变量可以覆盖默认来源，并支持通配 "*"。
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.core.config import (
    DEFAULT_CORS_ALLOW_ORIGINS,
    get_cors_allow_origins,
    is_cors_wildcard_origin,
)
from app.core.cors import configure_cors


DEV_ORIGIN = "http://127.0.0.1:5173"


def build_client() -> TestClient:
    """构造最小应用：只验证中间件行为，不加载真实业务装配。"""
    app = FastAPI()

    @app.get("/ping")
    def ping() -> dict[str, str]:
        return {"status": "ok"}

    configure_cors(app)
    return TestClient(app)


def test_default_origins_cover_local_vite_dev_server():
    # 保护行为：未配置环境变量时，默认来源必须包含 Vite 开发服务器的
    # 两个本机写法（127.0.0.1 与 localhost），否则本地直连后端会被浏览器拦截。
    assert get_cors_allow_origins() == list(DEFAULT_CORS_ALLOW_ORIGINS)
    assert DEV_ORIGIN in DEFAULT_CORS_ALLOW_ORIGINS


def test_env_origins_override_default(monkeypatch):
    # 保护行为：CORS_ALLOW_ORIGINS 生效，且会去除空白项。
    monkeypatch.setenv(
        "CORS_ALLOW_ORIGINS",
        " https://console.example.com , http://127.0.0.1:4173 ,",
    )

    assert get_cors_allow_origins() == [
        "https://console.example.com",
        "http://127.0.0.1:4173",
    ]


def test_blank_env_value_falls_back_to_default(monkeypatch):
    # 边界情况：环境变量只写了分隔符（无有效来源）时必须回退默认值，
    # 不能静默变成「拒绝所有跨域来源」。
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", " , ")

    assert get_cors_allow_origins() == list(DEFAULT_CORS_ALLOW_ORIGINS)
    assert is_cors_wildcard_origin(get_cors_allow_origins()) is False


def test_wildcard_origin_detected(monkeypatch):
    # 保护行为：通配来源能被识别，供装配层判断凭据策略。
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "*")

    assert get_cors_allow_origins() == ["*"]
    assert is_cors_wildcard_origin(get_cors_allow_origins()) is True


def test_simple_request_from_allowed_origin_gets_cors_header(monkeypatch):
    # 保护行为：白名单来源的普通请求返回 Allow-Origin 响应头。
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", DEV_ORIGIN)
    client = build_client()

    response = client.get("/ping", headers={"Origin": DEV_ORIGIN})

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == DEV_ORIGIN


def test_simple_request_from_unknown_origin_has_no_cors_header(monkeypatch):
    # 边界情况：未列入白名单的来源不得拿到 Allow-Origin，
    # 否则浏览器侧的白名单等于失效。
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", DEV_ORIGIN)
    client = build_client()

    response = client.get("/ping", headers={"Origin": "https://evil.example.com"})

    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_preflight_allows_tenant_and_auth_headers(monkeypatch):
    # 保护行为：预检响应必须放行前端真实发送的 Authorization、
    # Content-Type 与 X-Organization-ID 三个请求头，并列出实际用到的 HTTP 方法。
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", DEV_ORIGIN)
    client = build_client()

    response = client.options(
        "/ping",
        headers={
            "Origin": DEV_ORIGIN,
            "Access-Control-Request-Method": "DELETE",
            "Access-Control-Request-Headers": "authorization,x-organization-id",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == DEV_ORIGIN
    allowed_headers = response.headers["access-control-allow-headers"].lower()
    assert "authorization" in allowed_headers
    assert "x-organization-id" in allowed_headers
    assert "content-type" in allowed_headers
    allowed_methods = response.headers["access-control-allow-methods"]
    for method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        assert method in allowed_methods


def test_credentials_not_enabled_for_header_based_auth(monkeypatch):
    # 保护行为：鉴权使用 Bearer 头而非 Cookie，装配层必须关闭
    # allow_credentials，以兼容通配来源并避免携带凭据的跨域请求。
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "*")
    client = build_client()

    response = client.get("/ping", headers={"Origin": DEV_ORIGIN})

    assert response.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in response.headers
