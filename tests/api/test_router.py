from fastapi.testclient import TestClient
from app.api.router import creat_conversation_router
from fastapi import FastAPI

from app.sessions.base import Conversation,ConversationNotFoundError
from app.schemas.chat import LLMResponse
from app.users.base import User

CURRENT_USER = User(
    user_id="authenticated-user",
    username="alice",
    password_hash="not-exposed",
    created_at="2026-07-24 00:00:00",
)

class FakeChatService:
  def __init__(self):
    self.calls = []

  def create_conversation(self,*,user_id,system_prompt=None)->Conversation:
    self.calls.append(("create",user_id,system_prompt))
    return Conversation(conversation_id="conversation-1",user_id=user_id,system_prompt=system_prompt)
  
  def chat(self,*,user_id,conversation_id,question)->LLMResponse:
    self.calls.append(("chat",user_id,conversation_id,question))
    if conversation_id=="missing":
      raise ConversationNotFoundError("conversation not found")
    return LLMResponse(llm_answer="answer")
  
  def update_system_prompt(
      self, *, user_id, conversation_id, system_prompt
  ):
    self.calls.append(("prompt",user_id,conversation_id,system_prompt))

def build_client():
  def get_current_user():
    return CURRENT_USER
  service = FakeChatService()
  app = FastAPI()
  app.include_router(creat_conversation_router(chat_service = service, get_current_user=get_current_user))
  return TestClient(app), service

#API的端到端单元测试，验证“创建对话”接口在 HTTP 协议层、业务逻辑层和数据层的全链路行为是否正确
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
  assert service.calls==[("create","authenticated-user","be helpful")]

#验证API层能正确地从HTTP请求的不同位置（URL 路径和 JSON 请求体）提取参数，并准确地组装后传递给service层。
def test_chat_passes_user_and_conversation_to_service():
  client, service = build_client()
  response = client.post(
    "/conversations/conversation-1/chat/",
    json={"question":"Hello"}
  )
  assert response.status_code==200
  assert response.json()["llm_answer"]=="answer"
  assert service.calls==[("chat","authenticated-user","conversation-1","Hello")]

#验证 API 在面对不存在的会话时，能否正确地返回统一的 404 状态码
def test_missing_or_unowned_conversation_returns_404():
  client, _ = build_client()
  response = client.post(
    "/conversations/missing/chat/",
    json={"question":"hello"}
  )
  assert response.status_code==404
  assert response.json()=={"detail":"conversation not found error"}

#验证更新系统提示词是在单独的conversation里面，不越界更改
def test_update_system_prompt_is_conversation_scoped():
  client, service = build_client()
  response = client.put(
    "/conversations/conversation-1/system-prompt/",
    json={"system_prompt":"new prompt"}
  )
  assert response.status_code==200
  assert response.json()=={"updated": True}
  assert service.calls[0]==("prompt","authenticated-user","conversation-1","new prompt")

#测试空问题和空prompt返回422
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