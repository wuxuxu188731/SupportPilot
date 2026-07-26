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
  assert "/organizations/" in paths
  assert "/organizations/{organization_id}/members/" in paths

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


def create_organization(client, token, name):
    response = client.post(
        "/organizations/",
        json={"name": name},
        headers=bearer(token),
    )
    assert response.status_code == 201
    return response.json()["organization_id"]


def tenant_headers(token, organization_id):
    return {
        "Authorization": f"Bearer {token}",
        "X-Organization-ID": organization_id,
    }


def test_conversation_requires_organization_context(
    monkeypatch,
    tmp_path,
):
    client = TestClient(load_app(monkeypatch, tmp_path))
    alice_token = register_and_login(client, "alice")

    missing_context = client.post(
        "/conversations/",
        json={},
        headers=bearer(alice_token),
    )

    assert missing_context.status_code == 400


def test_register_login_and_conversation_ownership(
    monkeypatch,
    tmp_path,
):
    client = TestClient(load_app(monkeypatch, tmp_path))
    alice_token = register_and_login(client, "alice")
    bob_token = register_and_login(client, "bob")

    org_a = create_organization(client, alice_token, "Company A")
    org_b = create_organization(client, bob_token, "Company B")

    created = client.post(
        "/conversations/",
        json={"system_prompt": "be helpful"},
        headers=tenant_headers(alice_token, org_a),
    )
    conversation_id = created.json()["conversation_id"]
    cross_user = client.post(
        f"/conversations/{conversation_id}/chat/",
        json={"question": "steal history"},
        headers=tenant_headers(bob_token, org_a),
    )

    assert created.status_code == 201
    assert cross_user.status_code == 404


def test_tenant_header_is_verified_and_user_ownership_is_preserved(
    monkeypatch,
    tmp_path,
):
    client = TestClient(load_app(monkeypatch, tmp_path))
    alice_token = register_and_login(client, "alice")
    bob_token = register_and_login(client, "bob")
    org_a = create_organization(client, alice_token, "Company A")
    org_b = create_organization(client, bob_token, "Company B")

    missing_context = client.post(
        "/conversations/",
        json={},
        headers=bearer(alice_token),
    )
    created = client.post(
        "/conversations/",
        json={"system_prompt": "be helpful"},
        headers=tenant_headers(alice_token, org_a),
    )
    conversation_id = created.json()["conversation_id"]
    wrong_tenant = client.post(
        f"/conversations/{conversation_id}/chat/",
        json={"question": "steal history"},
        headers=tenant_headers(bob_token, org_a),
    )
    add_alice_to_org_b = client.post(
        f"/organizations/{org_b}/members/",
        json={"username": "alice", "role": "agent"},
        headers=bearer(bob_token),
    )
    own_other_tenant = client.post(
        f"/conversations/{conversation_id}/chat/",
        json={"question": "wrong company"},
        headers=tenant_headers(alice_token, org_b),
    )

    assert missing_context.status_code == 400
    assert created.status_code == 201
    assert wrong_tenant.status_code == 404
    assert add_alice_to_org_b.status_code == 201
    assert own_other_tenant.status_code == 404


def test_two_members_share_tenant_but_not_private_conversations(
    monkeypatch,
    tmp_path,
):
    client = TestClient(load_app(monkeypatch, tmp_path))
    alice_token = register_and_login(client, "alice")
    bob_token = register_and_login(client, "bob")
    org_a = create_organization(client, alice_token, "Company A")

    added = client.post(
        f"/organizations/{org_a}/members/",
        json={"username": "bob", "role": "agent"},
        headers=bearer(alice_token),
    )
    alice_conversation = client.post(
        "/conversations/",
        json={},
        headers=tenant_headers(alice_token, org_a),
    )
    bob_conversation = client.post(
        "/conversations/",
        json={},
        headers=tenant_headers(bob_token, org_a),
    )
    cross_user = client.put(
        (
            "/conversations/"
            f"{alice_conversation.json()['conversation_id']}"
            "/system-prompt/"
        ),
        json={"system_prompt": "unauthorized"},
        headers=tenant_headers(bob_token, org_a),
    )

    assert added.status_code == 201
    assert alice_conversation.status_code == 201
    assert bob_conversation.status_code == 201
    assert cross_user.status_code == 404
