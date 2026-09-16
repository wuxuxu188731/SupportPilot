import importlib
import sys

from fastapi.testclient import TestClient


def _app_paths(app) -> set[str]:
    """通过公开 OpenAPI 数据取全部已注册路径。

    FastAPI 0.141+ 对 include_router 采用惰性展开（app.routes 里是内部
    _IncludedRouter 占位对象，不再扁平暴露 APIRoute），因此这里不迭代
    app.routes 内部结构，而是读取 openapi()['paths'] 这一公开文档数据；
    生成 OpenAPI 不需要外部网络（知识库集合初始化被推迟到首次使用时）。
    """
    return set(app.openapi()["paths"])


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

  paths = _app_paths(main.app)
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
    """知识库路由注册与导入不触网：7 条管理路径（含正文读取）已注册，且导入带
    不可解析 QDRANT_URL 的 main 不会发起连接：集合初始化被推迟，导入期无 DNS/连接。"""
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

    spec = main.app.openapi()
    paths = spec["paths"]
    assert "/knowledge/documents/" in paths
    assert "/knowledge/documents/{document_id}/" in paths
    assert "/knowledge/documents/{document_id}/versions/" in paths
    assert "/knowledge/documents/{document_id}/versions/{version_id}/content/" in paths
    assert "/knowledge/documents/{document_id}/disable/" in paths
    assert "/knowledge/documents/{document_id}/enable/" in paths
    assert "/knowledge/ingestion-jobs/{job_id}/" in paths
    # 7 条管理路径共 8 个操作：/knowledge/documents/ 同时有 POST 与 GET
    http_methods = {"get", "post", "put", "patch", "delete"}
    knowledge_paths = [path for path in paths if path.startswith("/knowledge/")]
    assert len(knowledge_paths) == 7
    assert (
        sum(
            len([method for method in paths[path] if method in http_methods])
            for path in knowledge_paths
        )
        == 8
    )
    assert sorted(method for method in paths["/knowledge/documents/"] if method in http_methods) == [
        "get",
        "post",
    ]

    # 导入 main 不得触碰 qdrant 地址（若触碰会抛错，因为 qdrant.invalid
    # 不可解析）；走到这里即证明工厂推迟了全部网络调用。
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


def test_member_management_end_to_end_with_real_auth(monkeypatch, tmp_path):
    """成员管理走真实 app（真实 JWT 鉴权 + 真实 SQLite）的端到端行为。

    保护行为：
    - 成员列表带用户名且不含凭据字段，创建者自己也在列表里；
    - 加成员 → 改角色 → 移除 的完整链路与角色落库效果；
    - 写操作权限：agent 403，非成员/跨企业 404（不泄露企业是否存在）；
    - 自我管理限制：admin 对自己改角色/移除都是 409。
    """
    client = TestClient(load_app(monkeypatch, tmp_path))
    alice_token = register_and_login(client, "alice")
    bob_token = register_and_login(client, "bob")
    carol_token = register_and_login(client, "carol")

    org_a = create_organization(client, alice_token, "Company A")
    bob_user_id = client.get("/auth/me", headers=bearer(bob_token)).json()["user_id"]

    # 初始成员列表：只有创建者（admin），且不含密码字段
    initial = client.get(
        f"/organizations/{org_a}/members/",
        headers=bearer(alice_token),
    )
    assert initial.status_code == 200
    assert len(initial.json()) == 1
    assert initial.json()[0]["username"] == "alice"
    assert initial.json()[0]["role"] == "admin"
    assert "password_hash" not in initial.json()[0]

    # 添加 bob 为 agent
    added = client.post(
        f"/organizations/{org_a}/members/",
        json={"username": "bob", "role": "agent"},
        headers=bearer(alice_token),
    )
    assert added.status_code == 201

    # bob 现在能读成员列表（读接口不限角色），并且能看到自己与 alice
    bob_view = client.get(
        f"/organizations/{org_a}/members/",
        headers=bearer(bob_token),
    )
    assert bob_view.status_code == 200
    assert {item["username"] for item in bob_view.json()} == {"alice", "bob"}

    # bob（agent）不能改角色、不能移除成员
    assert client.patch(
        f"/organizations/{org_a}/members/{bob_user_id}/",
        json={"role": "admin"},
        headers=bearer(bob_token),
    ).status_code == 403
    assert client.delete(
        f"/organizations/{org_a}/members/{bob_user_id}/",
        headers=bearer(bob_token),
    ).status_code == 403

    # alice（admin）把 bob 提升为 admin，再降回 agent
    promoted = client.patch(
        f"/organizations/{org_a}/members/{bob_user_id}/",
        json={"role": "admin"},
        headers=bearer(alice_token),
    )
    assert promoted.status_code == 200
    assert promoted.json()["role"] == "admin"
    demoted = client.patch(
        f"/organizations/{org_a}/members/{bob_user_id}/",
        json={"role": "agent"},
        headers=bearer(alice_token),
    )
    assert demoted.status_code == 200
    assert demoted.json()["role"] == "agent"

    # admin 不能管理自己
    alice_user_id = client.get("/auth/me", headers=bearer(alice_token)).json()["user_id"]
    assert client.patch(
        f"/organizations/{org_a}/members/{alice_user_id}/",
        json={"role": "agent"},
        headers=bearer(alice_token),
    ).status_code == 409
    assert client.delete(
        f"/organizations/{org_a}/members/{alice_user_id}/",
        headers=bearer(alice_token),
    ).status_code == 409

    # 跨企业（carol 不属于 org_a）读成员列表：404，不泄露企业存在
    assert client.get(
        f"/organizations/{org_a}/members/",
        headers=bearer(carol_token),
    ).status_code == 404
    # 跨企业不可作为操作者
    assert client.delete(
        f"/organizations/{org_a}/members/{bob_user_id}/",
        headers=bearer(carol_token),
    ).status_code == 404

    # 移除 bob：204 无响应体，之后 bob 失去该企业上下文
    removed = client.delete(
        f"/organizations/{org_a}/members/{bob_user_id}/",
        headers=bearer(alice_token),
    )
    assert removed.status_code == 204
    assert removed.content == b""
    assert client.get("/organizations/", headers=bearer(bob_token)).json() == []
    assert client.get(
        f"/organizations/{org_a}/members/",
        headers=bearer(bob_token),
    ).status_code == 404

    # 移除后成员列表回到只有创建者
    final = client.get(
        f"/organizations/{org_a}/members/",
        headers=bearer(alice_token),
    )
    assert [item["username"] for item in final.json()] == ["alice"]


def test_cors_headers_present_on_real_app_response(monkeypatch, tmp_path):
    """真实 app 的 CORS 装配：白名单来源拿到 Allow-Origin，预检放行租户头。"""
    client = TestClient(load_app(monkeypatch, tmp_path))
    origin = "http://127.0.0.1:5173"

    preflight = client.options(
        "/organizations/",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,x-organization-id",
        },
    )
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == origin
    allowed_headers = preflight.headers["access-control-allow-headers"].lower()
    assert "x-organization-id" in allowed_headers
    assert "authorization" in allowed_headers

    # 真实业务响应也带 CORS 头（未被 401 等错误分支吞掉）
    unauthorized = client.get("/organizations/", headers={"Origin": origin})
    assert unauthorized.status_code == 401
    assert unauthorized.headers["access-control-allow-origin"] == origin

    # 未列入白名单的来源没有 Allow-Origin 头
    unknown = client.get(
        "/organizations/",
        headers={"Origin": "https://evil.example.com"},
    )
    assert "access-control-allow-origin" not in unknown.headers


def test_member_paths_registered_in_openapi(monkeypatch, tmp_path):
    """成员管理三条路径已注册，且合计 27 个 HTTP 操作（在第四阶段盘点口径的
    26 个之上，新增知识库正文读取接口 1 个）。"""
    app = load_app(monkeypatch, tmp_path)
    paths = app.openapi()["paths"]
    http_methods = {"get", "post", "put", "patch", "delete"}

    assert sorted(
        method for method in paths["/organizations/{organization_id}/members/"]
        if method in http_methods
    ) == ["get", "post"]
    assert sorted(
        method
        for method in paths[
            "/organizations/{organization_id}/members/{user_id}/"
        ]
        if method in http_methods
    ) == ["delete", "patch"]
    total_operations = sum(
        len([method for method in methods if method in http_methods])
        for methods in paths.values()
    )
    assert total_operations == 27
