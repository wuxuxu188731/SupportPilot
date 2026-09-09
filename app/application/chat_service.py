from typing import Callable
from uuid import uuid4

from app.agent.invocation_context import AgentInvocationContext
from app.application.organization_service import TenantContext
from app.concurrency.conversation_locks import ConversationLockRegistry
from app.schemas.chat import (
  ConversationHistoryMessage,
  ConversationHistoryResponse,
  ConversationListItem,
  LLMResponse,
)
from app.sessions.base import (
  Conversation,
  MessageRecord,
  SessionStore,
)


# 会话标题的最大字符数（超出部分截断，适合会话侧栏一行展示）
MAX_CONVERSATION_TITLE_LENGTH = 30
# 尚无用户消息的空会话默认标题
DEFAULT_CONVERSATION_TITLE = "新会话"


def derive_conversation_title(first_user_content: str | None) -> str:
  """由会话首条用户消息正文推导侧栏标题。

  规则：去首尾空白、连续空白折叠为单个空格、超长截断；
  没有消息或内容为空时返回“新会话”。
  """
  if not first_user_content:
    return DEFAULT_CONVERSATION_TITLE
  collapsed = " ".join(first_user_content.split())
  if not collapsed:
    return DEFAULT_CONVERSATION_TITLE
  if len(collapsed) <= MAX_CONVERSATION_TITLE_LENGTH:
    return collapsed
  return collapsed[:MAX_CONVERSATION_TITLE_LENGTH]


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

  def list_conversations(
    self,
    *,
    context : TenantContext,
    limit : int,
    offset : int,
  )-> list[ConversationListItem]:
    """列出当前企业与当前用户自己的会话，标题由首条用户消息实时推导。"""
    records = self._store.list_conversations(
      organization_id=context.organization_id,
      user_id=context.user_id,
      limit=limit,
      offset=offset,
    )
    return [
      ConversationListItem(
        conversation_id=record.conversation_id,
        title=derive_conversation_title(record.first_user_content),
        created_at=record.created_at,
        updated_at=record.updated_at,
      )
      for record in records
    ]

  def get_history(
    self,
    *,
    context : TenantContext,
    conversation_id : str,
  )-> ConversationHistoryResponse:
    """读取单个会话的安全历史：只返回用户问题与 Agent 最终回答。

    会话不存在、跨用户或跨企业时由 Store 抛出 NotFoundError，
    上层统一转换为 404，避免泄露资源归属信息。
    """
    record = self._store.get_conversation_record(
      organization_id=context.organization_id,
      user_id=context.user_id,
      conversation_id=conversation_id,
    )
    message_records = self._store.load_message_records(
      organization_id=context.organization_id,
      user_id=context.user_id,
      conversation_id=conversation_id,
    )
    messages = [
      visible
      for stored in message_records
      if (visible := self._to_visible_message(stored)) is not None
    ]
    return ConversationHistoryResponse(
      conversation_id=record.conversation_id,
      system_prompt=record.system_prompt,
      created_at=record.created_at,
      updated_at=record.updated_at,
      messages=messages,
    )

  @staticmethod
  def _to_visible_message(stored: MessageRecord) -> ConversationHistoryMessage | None:
    """把一条内部消息转换为可展示消息；不可展示时返回 None。

    安全过滤规则：
    - 只允许 user 问题与无 tool_calls 的 assistant 最终回答；
    - 空内容（含全空白）一律排除；
    - system/tool 消息、带工具调用的中间 assistant 消息、
      reasoning_content、tool_calls、工具参数/结果等内部字段全部不返回。
    """
    payload = stored.payload
    role = payload.get("role") or stored.role
    content = payload.get("content")
    if not isinstance(content, str) or not content.strip():
      return None
    if role == "user":
      return ConversationHistoryMessage(
        sequence=stored.sequence,
        role="user",
        content=content,
        created_at=stored.created_at,
      )
    if role == "assistant":
      # 带工具调用的中间助手消息不是最终回答，不向用户展示
      if payload.get("tool_calls"):
        return None
      return ConversationHistoryMessage(
        sequence=stored.sequence,
        role="assistant",
        content=content,
        created_at=stored.created_at,
      )
    # system / tool 等内部消息一律不向最终用户展示
    return None

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
