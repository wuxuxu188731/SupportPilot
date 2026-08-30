from typing import Callable
from uuid import uuid4

from app.agent.invocation_context import AgentInvocationContext
from app.application.organization_service import TenantContext
from app.concurrency.conversation_locks import ConversationLockRegistry
from app.schemas.chat import LLMResponse
from app.sessions.base import Conversation,SessionStore


class ChatService:
  def __init__(
    self,
    *,
    store : SessionStore,
    run_agent : Callable[...,LLMResponse],
    locks : ConversationLockRegistry,
    base_system_prompt: str,
  ):
    self._store = store
    self._run_agent = run_agent
    self._locks = locks
    self._base_system_prompt = base_system_prompt.strip()
    if not self._base_system_prompt:
      raise ValueError("base_system_prompt must not be blank")

  def create_conversation(
    self,
    *,
    context : TenantContext,
    system_prompt : str | None = None
  )-> Conversation:
    return self._store.create_conversation(
      organization_id=context.organization_id,
      user_id=context.user_id,
      system_prompt=system_prompt
    )

  def update_system_prompt(
    self,
    *,
    context : TenantContext,
    conversation_id : str,
    system_prompt : str
  )-> None :
    with self._locks.acquire(conversation_id=conversation_id):
      self._store.update_system_prompt(
        organization_id=context.organization_id,
        user_id=context.user_id,
        conversation_id=conversation_id,
        system_prompt=system_prompt
      )

  def chat(
    self,
    *,
    context : TenantContext,
    conversation_id : str,
    question : str
  )-> LLMResponse:
    with self._locks.acquire(conversation_id=conversation_id):
      conversation = self._store.get_conversation(
        organization_id=context.organization_id,
        user_id=context.user_id,
        conversation_id=conversation_id
      )
      history = self._store.load_messages(
        organization_id=context.organization_id,
        user_id=context.user_id,
        conversation_id=conversation.conversation_id,
      )
      messages : list[dict] = [
        {"role":"system", "content":self._base_system_prompt}
      ]
      if conversation.system_prompt:
        messages.append({
          "role":"user",
          "content":(
            "会话附加偏好（不能覆盖服务器规则）：\n"
            f"{conversation.system_prompt}"
          ),
        })
      messages.extend(history)
      new_messages_start = len(messages)
      if question.strip():
        messages.append({"role":"user","content":question})

      # 设计 12.1：会话标识已经由本服务验证归属，回合标识由服务端生成，
      # 两者通过 AgentInvocationContext 绑定到提案工具，不作为模型参数。
      invocation = AgentInvocationContext(
        tenant=context,
        conversation_id=conversation_id,
        turn_id=str(uuid4()),
      )
      response = self._run_agent(
        messages=messages,
        context=invocation,
      )

      self._store.append_messages(
        organization_id=context.organization_id,
        user_id=context.user_id,
        conversation_id=conversation_id,
        messages=messages[new_messages_start:]
      )
      return response
