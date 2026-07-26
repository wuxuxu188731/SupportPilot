import importlib
import sys

from fastapi.testclient import TestClient

def test_main_wires_sqlite_service_without_global_messages(
  monkeypatch,
  tmp_path
):
  database_path = tmp_path / "min-chat.db"
  monkeypatch.setenv("DEEPSEEK_API_KEY","test-only-key")
  monkeypatch.setenv("CHAT_DB_PATH", str(database_path))
  monkeypatch.setenv("AUTH_SECRET_KEY", "test-only-auth-secret-key-abcdefghijklmnopqrstuvwxyz")

  sys.modules.pop("main",None)
  main = importlib.import_module("main")

  paths = {route.path for route in main.app.routes}
  assert not hasattr(main, "messages")
  assert database_path.exists()
  assert "/conversations/" in paths
  assert "/conversations/{conversation_id}/chat/" in paths

def load_app(monkeypatch, tmp_path):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-key")
    monkeypatch.setenv(
        "AUTH_SECRET_KEY",
        "test-secret-key-that-is-at-least-32-characters",
    )
    monkeypatch.setenv(
        "CHAT_DB_PATH",
        str(tmp_path / "app.db"),
    )
    sys.modules.pop("main", None)
    return importlib.import_module("main").app


def register_and_login(client, username):
    register_response = client.post(
        "/auth/register",
        json={
            "username": username,
            "password": "correct-horse-42",
        },
    )
    assert register_response.status_code == 201

    login_response = client.post(
        "/auth/login",
        json={
            "username": username,
            "password": "correct-horse-42",
        },
    )
    assert login_response.status_code == 200
    return login_response.json()["access_token"]


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_register_login_and_conversation_ownership(
    monkeypatch,
    tmp_path,
):
    client = TestClient(load_app(monkeypatch, tmp_path))
    alice_token = register_and_login(client, "alice")
    bob_token = register_and_login(client, "bob")

    unauthenticated = client.post("/conversations/", json={})
    created = client.post(
        "/conversations/",
        json={"system_prompt": "be helpful"},
        headers=bearer(alice_token),
    )
    conversation_id = created.json()["conversation_id"]
    cross_user = client.post(
        f"/conversations/{conversation_id}/chat/",
        json={"question": "steal history"},
        headers=bearer(bob_token),
    )

    assert unauthenticated.status_code == 401
    assert created.status_code == 201
    assert cross_user.status_code == 404
