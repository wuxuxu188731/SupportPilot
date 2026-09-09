import importlib
import sys

from fastapi.testclient import TestClient

def test_main_wires_sqlite_service_without_global_messages(
  monkeypatch,
  tmp_path
):
  # 保护行为：主应用应装配完整工具面与动作 API，且不保留全局聊天消息状态。
  from app.agent.support_runner import CustomerSupportAgentRunner
  from app.tools.support_gateway import CustomerSupportToolGateway

  database_path = tmp_path / "min-chat.db"
  monkeypatch.setenv("DEEPSEEK_API_KEY","test-only-key")
  monkeypatch.setenv("CHAT_DB_PATH", str(database_path))
  monkeypatch.setenv("AUTH_SECRET_KEY", "test-only-auth-secret-key-abcdefghijklmnopqrstuvwxyz")
  monkeypatch.setenv("DASHSCOPE_API_KEY", "test-only-key")
  monkeypatch.setenv("QDRANT_URL", "http://qdrant.invalid:6333")

  sys.modules.pop("main",None)
  main = importlib.import_module("main")

  paths = {route.path for route in main.app.routes}
  assert isinstance(
      main.support_tool_gateway,
      CustomerSupportToolGateway,
  )
  assert isinstance(
      main.support_agent_runner,
      CustomerSupportAgentRunner,
  )
  assert {
      item["function"]["name"]
      for item in main.support_tool_gateway.definitions
  } == {
      "get_order",
      "get_logistics",
      "create_ticket",
      "add_ticket_note",
  }
  assert [
      item["function"]["name"]
      for item in main.composite_tool_gateway.definitions
  ] == [
      "get_order", "get_logistics", "create_ticket",
      "add_ticket_note", "search_knowledge",
      "propose_refund", "propose_compensation", "get_action_status",
  ]
  assert not hasattr(main, "messages")
  assert not hasattr(main, "TOOL_FUNCTIONS")
  assert not hasattr(main, "TOOL_DEFINITIONS")
  assert database_path.exists()
  assert "/conversations/" in paths
  assert "/conversations/{conversation_id}/chat/" in paths
  assert "/organizations/" in paths
  assert "/organizations/{organization_id}/members/" in paths
  assert "/approvals/" in paths
  assert "/approvals/{approval_id}/" in paths
  assert "/approvals/{approval_id}/decisions/" in paths
  assert "/action-runs/{run_id}/" in paths
  assert "/action-runs/{run_id}/resume/" in paths

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
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-only-key")
    monkeypatch.setenv("QDRANT_URL", "http://qdrant.invalid:6333")
    sys.modules.pop("main", None)
    return importlib.import_module("main").app


def test_knowledge_routes_registered_without_network(monkeypatch, tmp_path):
    """The knowledge router's 7 management routes are registered, and importing
    main with an unresolvable QDRANT_URL does NOT connect: collection init is
    deferred, so no DNS/connection attempt happens at import time."""
    database_path = tmp_path / "knowledge-app.db"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-key")
    monkeypatch.setenv(
        "AUTH_SECRET_KEY",
        "test-secret-key-that-is-at-least-32-characters",
    )
    monkeypatch.setenv("CHAT_DB_PATH", str(database_path))
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-only-key")
    monkeypatch.setenv("QDRANT_URL", "http://qdrant.invalid:6333")

    sys.modules.pop("main", None)
    main = importlib.import_module("main")

    paths = {route.path for route in main.app.routes}
    assert "/knowledge/documents/" in paths
    assert "/knowledge/documents/{document_id}/" in paths
    assert "/knowledge/documents/{document_id}/versions/" in paths
    assert "/knowledge/documents/{document_id}/disable/" in paths
    assert "/knowledge/documents/{document_id}/enable/" in paths
    assert "/knowledge/ingestion-jobs/{job_id}/" in paths
    # 7 management routes: POST+GET share /knowledge/documents/ (one path,
    # two routes), so count the route objects, not the deduplicated paths.
    knowledge_routes = [
        route.path for route in main.app.routes if route.path.startswith("/knowledge/")
    ]
    assert len(knowledge_routes) == 7
    assert knowledge_routes.count("/knowledge/documents/") == 2

    # Importing main must not have touched the qdrant URL (it would raise, since
    # qdrant.invalid does not resolve). The fact that we got this far proves the
    # factory deferred every network call.
    assert database_path.exists()


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


def test_list_and_history_endpoints_require_auth_and_tenant(
    monkeypatch,
    tmp_path,
):
    # 保护行为：新增的会话列表与历史消息读取接口必须遵守
    # 「Bearer Token + X-Organization-ID」全局认证/租户规则；
    # 无令牌 401、缺租户头 400、非成员企业 404。
    client = TestClient(load_app(monkeypatch, tmp_path))
    alice_token = register_and_login(client, "alice")
    org_a = create_organization(client, alice_token, "Company A")

    assert client.get("/conversations/").status_code == 401
    assert client.get("/conversations/", headers=bearer(alice_token)).status_code == 400
    assert client.get("/conversations/", headers=tenant_headers(alice_token, org_a)).status_code == 200
    assert client.get("/conversations/", headers=tenant_headers(alice_token, "no-such-org")).status_code == 404

    assert client.get("/conversations/some-id/messages/").status_code == 401
    assert (
        client.get(
            "/conversations/some-id/messages/",
            headers=bearer(alice_token),
        ).status_code
        == 400
    )


def test_list_pagination_validation_and_history_isolation(
    monkeypatch,
    tmp_path,
):
    # 边界与安全：limit/offset 越界返回 422；列表只含本人会话；
    # 历史读取跨用户 404；空历史返回空数组且不泄露内部字段。
    client = TestClient(load_app(monkeypatch, tmp_path))
    alice_token = register_and_login(client, "alice")
    bob_token = register_and_login(client, "bob")
    org_a = create_organization(client, alice_token, "Company A")
    org_b = create_organization(client, bob_token, "Company B")

    alice_conversation = client.post(
        "/conversations/",
        json={},
        headers=tenant_headers(alice_token, org_a),
    )
    alice_id = alice_conversation.json()["conversation_id"]

    # 分页参数校验由路由 Query 约束兜底
    assert client.get("/conversations/", params={"limit": 0}, headers=tenant_headers(alice_token, org_a)).status_code == 422
    assert client.get("/conversations/", params={"limit": 101}, headers=tenant_headers(alice_token, org_a)).status_code == 422
    assert client.get("/conversations/", params={"offset": -1}, headers=tenant_headers(alice_token, org_a)).status_code == 422

    # 会话列表只返回自己的会话：alice 在 org_a 有 1 条，bob 在 org_a 看不到
    alice_list = client.get(
        "/conversations/",
        headers=tenant_headers(alice_token, org_a),
    )
    assert alice_list.status_code == 200
    body = alice_list.json()
    assert len(body) == 1
    assert body[0]["conversation_id"] == alice_id
    # 尚未发送消息的空会话标题为「新会话」，且不包含任何内部字段
    assert body[0]["title"] == "新会话"
    assert set(body[0].keys()) == {"conversation_id", "title", "created_at", "updated_at"}

    # bob 不属于 org_a：即使给出 alice 的会话 id 也只能得到 404
    bob_in_org_a = client.get(
        f"/conversations/{alice_id}/messages/",
        headers=tenant_headers(bob_token, org_a),
    )
    assert bob_in_org_a.status_code == 404
    # alice 在其它企业也看不到该会话（跨企业 404）
    alice_in_org_b = client.get(
        f"/conversations/{alice_id}/messages/",
        headers=tenant_headers(alice_token, org_b),
    )
    assert alice_in_org_b.status_code == 404
    # 空会话历史：空数组 + 头部时间字段齐全
    own_history = client.get(
        f"/conversations/{alice_id}/messages/",
        headers=tenant_headers(alice_token, org_a),
    )
    assert own_history.status_code == 200
    assert own_history.json() == {
        "conversation_id": alice_id,
        "system_prompt": None,
        "created_at": body[0]["created_at"],
        "updated_at": body[0]["updated_at"],
        "messages": [],
    }
