from fastapi.testclient import TestClient
from app.api.router import creat_conversation_router
from fastapi import FastAPI

from app.application.organization_service import TenantContext
from app.organizations.base import MembershipRole
from app.sessions.base import Conversation,ConversationNotFoundError
from app.schemas.chat import LLMResponse

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
