import sqlite3

from fastapi.testclient import TestClient
from app.api.router import creat_conversation_router
from fastapi import FastAPI

from app.agent.prompts import SUPPORT_SYSTEM_PROMPT
from app.application.chat_service import ChatService
from app.application.organization_service import TenantContext
from app.concurrency.conversation_locks import ConversationLockRegistry
from app.knowledge.results import Citation
from app.organizations.base import MembershipRole
from app.schemas.chat import (
  ConversationHistoryResponse,
  ConversationListItem,
  LLMResponse,
)
from app.sessions.base import Conversation,ConversationNotFoundError
from app.sessions.sqlite_store import SQLiteSessionStore

CURRENT_TENANT = TenantContext(
    user_id="authenticated-user",
    organization_id="organization-a",
    role=MembershipRole.AGENT,
)

class FakeChatService:
  def __init__(self):
    self.calls = []

  def create_conversation(self,*,context,system_prompt=None)->Conversation:
    self.calls.append(("create",context.organization_id,context.user_id,system_prompt))
    return Conversation(
      conversation_id="conversation-1",
      organization_id=context.organization_id,
      user_id=context.user_id,
      system_prompt=system_prompt,
    )

  def list_conversations(self,*,context,limit,offset)->list[ConversationListItem]:
    self.calls.append(("list",context.organization_id,context.user_id,limit,offset))
    return [
      ConversationListItem(
        conversation_id="conversation-1",
        title="标题一",
        created_at="2026-09-01 08:00:00",
        updated_at="2026-09-02 08:00:00",
      ),
      ConversationListItem(
        conversation_id="conversation-2",
        title="新会话",
        created_at="2026-09-03 08:00:00",
        updated_at="2026-09-03 08:00:00",
      ),
    ]

  def get_history(self,*,context,conversation_id)->ConversationHistoryResponse:
    self.calls.append(("history",context.organization_id,context.user_id,conversation_id))
    if conversation_id=="missing":
      raise ConversationNotFoundError("conversation not found")
    return ConversationHistoryResponse(
      conversation_id=conversation_id,
      system_prompt=None,
      created_at="2026-09-01 08:00:00",
      updated_at="2026-09-02 08:00:00",
      messages=[],
    )

  def chat(self,*,context,conversation_id,question)->LLMResponse:
    self.calls.append(("chat",context.organization_id,context.user_id,conversation_id,question))
    if conversation_id=="missing":
      raise ConversationNotFoundError("conversation not found")
    return LLMResponse(llm_answer="answer")

  def update_system_prompt(
      self, *, context, conversation_id, system_prompt
  ):
    self.calls.append(("prompt",context.organization_id,context.user_id,conversation_id,system_prompt))

def build_client():
  def get_current_tenant():
    return CURRENT_TENANT
  service = FakeChatService()
  app = FastAPI()
  app.include_router(
    creat_conversation_router(
      chat_service=service,
      get_current_tenant=get_current_tenant,
    )
  )
  return TestClient(app), service


def test_create_conversation_returns_server_id():
  client, service = build_client()
  response = client.post(
    "/conversations/",
    json={
      "system_prompt":"be helpful",
    }
  )
  assert response.status_code==201
  assert response.json()=={"conversation_id": "conversation-1"}
  assert service.calls==[("create","organization-a","authenticated-user","be helpful")]


def test_chat_passes_user_and_conversation_to_service():
  client, service = build_client()
  response = client.post(
    "/conversations/conversation-1/chat/",
    json={"question":"Hello"}
  )
  assert response.status_code==200
  assert response.json()["llm_answer"]=="answer"
  assert service.calls==[("chat","organization-a","authenticated-user","conversation-1","Hello")]


def test_missing_or_unowned_conversation_returns_404():
  client, _ = build_client()
  response = client.post(
    "/conversations/missing/chat/",
    json={"question":"hello"}
  )
  assert response.status_code==404
  assert response.json()=={"detail":"conversation not found error"}


def test_update_system_prompt_is_conversation_scoped():
  client, service = build_client()
  response = client.put(
    "/conversations/conversation-1/system-prompt/",
    json={"system_prompt":"new prompt"}
  )
  assert response.status_code==200
  assert response.json()=={"updated": True}
  assert service.calls[0]==("prompt","organization-a","authenticated-user","conversation-1","new prompt")


def test_blank_question_and_prompt_return_422():
  client, service = build_client()
  response_1 = client.post(
    "conversations/conversation-1/chat/",
    json={"question":"    "}
  )
  response_2 = client.put(
    "/conversations/conversation-1/system-prompt/",
    json={"system_prompt":"    "}
  )
  assert response_1.status_code==422
  assert response_2.status_code==422


def test_list_conversations_returns_items_and_passes_pagination():
  # 保护行为：会话列表路由应把 limit/offset 分页参数传给服务层，
  # 并以服务层组装好的列表项原样返回（200）。
  client, service = build_client()
  response = client.get("/conversations/", params={"limit": 20, "offset": 40})

  assert response.status_code == 200
  body = response.json()
  assert len(body) == 2
  assert body[0] == {
    "conversation_id": "conversation-1",
    "title": "标题一",
    "created_at": "2026-09-01 08:00:00",
    "updated_at": "2026-09-02 08:00:00",
  }
  assert ("list", "organization-a", "authenticated-user", 20, 40) in service.calls


def test_list_conversations_uses_default_pagination_values():
  # 边界情况：未传分页参数时应使用默认 limit=50、offset=0。
  client, service = build_client()
  response = client.get("/conversations/")

  assert response.status_code == 200
  assert ("list", "organization-a", "authenticated-user", 50, 0) in service.calls


def test_list_conversations_pagination_validation_returns_422():
  # 边界情况：limit 小于 1、大于 100 或 offset 小于 0 时必须返回 422，
  # 由路由层 Query 约束兜底，不能进入服务层。
  client, service = build_client()
  assert client.get("/conversations/", params={"limit": 0}).status_code == 422
  assert client.get("/conversations/", params={"limit": 101}).status_code == 422
  assert client.get("/conversations/", params={"offset": -1}).status_code == 422
  assert client.get("/conversations/", params={"limit": "abc"}).status_code == 422


def test_get_conversation_messages_returns_history():
  # 保护行为：历史消息路由应把会话 id 传给服务层并返回安全历史响应。
  client, service = build_client()
  response = client.get("/conversations/conversation-1/messages/")

  assert response.status_code == 200
  body = response.json()
  assert body == {
    "conversation_id": "conversation-1",
    "system_prompt": None,
    "created_at": "2026-09-01 08:00:00",
    "updated_at": "2026-09-02 08:00:00",
    "messages": [],
  }
  assert ("history", "organization-a", "authenticated-user", "conversation-1") in service.calls


def test_get_conversation_messages_missing_or_unowned_returns_404():
  # 安全边界：会话不存在、跨用户或跨企业读取历史统一返回 404，
  # 且错误体与聊天接口一致（不泄露资源归属）。
  client, _ = build_client()
  response = client.get("/conversations/missing/messages/")
  assert response.status_code == 404
  assert response.json() == {"detail": "conversation not found error"}


def test_get_conversation_messages_blank_id_returns_422():
  # 边界情况：路径中的会话 id 全空白时应返回 422，而不是去查库。
  client, _ = build_client()
  response = client.get("/conversations/%20%20/messages/")
  assert response.status_code == 422


def build_real_service_client(tmp_path, runner=None):
  """用真实 SQLite SessionStore 与 ChatService 组装迷你应用（不触发模型）。"""
  database_path = tmp_path / "chat.db"
  # 先实例化 store 触发数据库迁移（建表），再插入种子数据
  store = SQLiteSessionStore(database_path)
  with sqlite3.connect(database_path) as conn:
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
      "INSERT INTO users(id, username, password_hash) VALUES (?, ?, ?)",
      ("real-user", "realuser", "hash"),
    )
    conn.execute(
      "INSERT INTO organizations(id, name) VALUES (?, ?)",
      ("real-org", "Real Org"),
    )
    conn.execute(
      "INSERT INTO memberships(organization_id, user_id, role) VALUES (?, ?, ?)",
      ("real-org", "real-user", "agent"),
    )

  def fake_runner(*, messages, context):
    # 测试用假运行器：只返回固定回答，绝不调用真实模型
    messages.append({"role": "assistant", "content": "ok"})
    return LLMResponse(llm_answer="ok")

  service = ChatService(
    store=store,
    run_agent=runner or fake_runner,
    locks=ConversationLockRegistry(),
    base_system_prompt=SUPPORT_SYSTEM_PROMPT,
  )
  context = TenantContext(
    user_id="real-user",
    organization_id="real-org",
    role=MembershipRole.AGENT,
  )
  conversation = service.create_conversation(context=context)
  service.chat(context=context, conversation_id=conversation.conversation_id, question="你好")

  def get_current_tenant():
    return context
  app = FastAPI()
  app.include_router(
    creat_conversation_router(
      chat_service=service,
      get_current_tenant=get_current_tenant,
    )
  )
  return TestClient(app), conversation.conversation_id


def test_openapi_schema_matches_real_list_and_history_responses(tmp_path):
  # 保护行为：OpenAPI 响应模型定义的字段必须与真实接口响应一致，
  # 防止文档与实现漂移（列表项与历史响应两个模型都要核对）。
  client, conversation_id = build_real_service_client(tmp_path)
  openapi = client.get("/openapi.json").json()

  list_schema = openapi["components"]["schemas"]["ConversationListItem"]
  list_body = client.get("/conversations/", params={"limit": 50, "offset": 0}).json()
  assert len(list_body) == 1
  assert set(list_body[0].keys()) == set(list_schema["properties"].keys())

  history_schema = openapi["components"]["schemas"]["ConversationHistoryResponse"]
  message_schema = openapi["components"]["schemas"]["ConversationHistoryMessage"]
  history_body = client.get(f"/conversations/{conversation_id}/messages/").json()
  assert set(history_body.keys()) == set(history_schema["properties"].keys())
  assert history_body["messages"]
  assert set(history_body["messages"][0].keys()) == set(message_schema["properties"].keys())
  # 真实响应的 role 取值与 schema 枚举一致（只允许 user/assistant）
  assert history_body["messages"][0]["role"] in {"user", "assistant"}
  assert message_schema["properties"]["role"]["enum"] == ["user", "assistant"]


def build_citation_runner(answer, citations, *, answer_incomplete=False):
  """构造返回固定回答与引用的假运行器（不调用真实模型）。"""

  def runner(*, messages, context):
    messages.append({
      "role": "assistant",
      "content": answer,
      "reasoning_content": "内部推理不得返回",
    })
    return LLMResponse(
      llm_answer=answer,
      llm_reasoning_content="内部推理不得返回",
      citations=list(citations),
      answer_incomplete=answer_incomplete,
    )

  return runner


def _router_citation(citation_id, **overrides):
  """构造一条带偏移的引用（默认值与知识库真实响应同形）。"""
  values = {
    "citation_id": citation_id,
    "document_id": "doc-1",
    "version_id": "ver-1",
    "chunk_id": f"chunk-{citation_id}",
    "title": "退货政策",
    "heading_path": "3.2 退货流程",
    "content": "七天无理由退货",
    "start_offset": 5,
    "end_offset": 13,
  }
  values.update(overrides)
  return Citation(**values)


def test_history_response_carries_persisted_citations(tmp_path):
  # 保护行为：真实 SQLite 迷你应用的历史响应必须包含新字段，
  # 且引用的每一个字段（含证据片段正文 content）都与生成本轮回答时一致。
  client, conversation_id = build_real_service_client(
    tmp_path,
    build_citation_runner("回答 [C1]", [_router_citation("C1")]),
  )

  body = client.get(f"/conversations/{conversation_id}/messages/").json()

  assistant = body["messages"][1]
  assert assistant["role"] == "assistant"
  assert assistant["answer_incomplete"] is False
  assert assistant["citations"] == [{
    "citation_id": "C1",
    "document_id": "doc-1",
    "version_id": "ver-1",
    "chunk_id": "chunk-C1",
    "title": "退货政策",
    "heading_path": "3.2 退货流程",
    "content": "七天无理由退货",
    "start_offset": 5,
    "end_offset": 13,
  }]
  # 用户消息恒为空引用（字段必须存在，前端无需处理缺失）
  assert body["messages"][0]["citations"] == []
  assert body["messages"][0]["answer_incomplete"] is False


def test_history_response_carries_answer_incomplete_without_citations(tmp_path):
  # 边界情况：漏引场景下引用为空但完整性标记为 true，
  # 刷新后界面仍能给出「谨慎采用」提示。
  client, conversation_id = build_real_service_client(
    tmp_path,
    build_citation_runner("没有引用的回答", [], answer_incomplete=True),
  )

  body = client.get(f"/conversations/{conversation_id}/messages/").json()

  assert body["messages"][1]["citations"] == []
  assert body["messages"][1]["answer_incomplete"] is True


def test_history_response_never_leaks_internal_payload_fields(tmp_path):
  # 安全边界（防泄漏回归）：历史响应不得出现 reasoning_content、tool_calls、
  # 工具参数与结果、raw_text 等内部字段——新增引用字段不得顺带扩大暴露面。
  client, conversation_id = build_real_service_client(
    tmp_path,
    build_citation_runner("回答 [C1]", [_router_citation("C1")]),
  )

  response = client.get(f"/conversations/{conversation_id}/messages/")
  raw_text = response.text

  assert response.status_code == 200
  assert "reasoning_content" not in raw_text
  assert "tool_calls" not in raw_text
  assert "raw_text" not in raw_text
  assert "payload_json" not in raw_text
  for message in response.json()["messages"]:
    assert set(message.keys()) == {
      "sequence",
      "role",
      "content",
      "created_at",
      "citations",
      "answer_incomplete",
    }


def test_history_citation_fields_match_openapi_schema(tmp_path):
  # 保护行为：OpenAPI 声明的引用字段集合必须与真实响应完全一致，
  # 防止引用契约在文档与实现之间漂移。
  client, conversation_id = build_real_service_client(
    tmp_path,
    build_citation_runner("回答 [C1]", [_router_citation("C1")]),
  )
  openapi = client.get("/openapi.json").json()
  citation_schema = openapi["components"]["schemas"]["Citation"]

  citation = client.get(
    f"/conversations/{conversation_id}/messages/"
  ).json()["messages"][1]["citations"][0]

  assert set(citation.keys()) == set(citation_schema["properties"].keys())
