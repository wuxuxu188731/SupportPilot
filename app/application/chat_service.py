import logging
from dataclasses import fields
from typing import Callable
from uuid import uuid4

from app.agent.invocation_context import AgentInvocationContext
from app.application.organization_service import TenantContext
from app.concurrency.conversation_locks import ConversationLockRegistry
from app.knowledge.results import Citation
from app.schemas.chat import (
  ConversationHistoryMessage,
  ConversationHistoryResponse,
  ConversationListItem,
  LLMResponse,
)
from app.sessions.base import (
  Conversation,
  MessageDisplay,
  MessageRecord,
  SessionStore,
)

logger = logging.getLogger(__name__)

# 会话标题的最大字符数（超出部分截断，适合会话侧栏一行展示）
MAX_CONVERSATION_TITLE_LENGTH = 30
# 尚无用户消息的空会话默认标题
DEFAULT_CONVERSATION_TITLE = "新会话"

# Citation 的字段名集合：读路径用它过滤库里多出来的列，
# 避免将来加一列就让整条历史引用被丢弃。
_CITATION_FIELD_NAMES = frozenset(
  field.name for field in fields(Citation)
)


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


def citation_to_stored_dict(citation : Citation) -> dict:
  """把一条 Citation 投影成可落库的 dict（键 == message_citations 业务列）。

  写库前必须把上游数据规整成「库能接受」的形态，否则一次 IntegrityError
  会让整轮对话（含已生成的回答、甚至已创建的提案）一起失败：

  - **偏移同生同灭**：任一偏移为 None、``start >= end`` 或出现负数时，
    两个偏移都置 None——与前端「无法精确定位就降级为只打开文档」的口径一致；
  - ``public_dict()`` 输出的键与 ``Citation`` 字段一一对应，
    store 侧会再校验一次键集合（缺列或多余键都显式失败）。
  """
  stored = citation.public_dict()
  if not _offsets_are_consistent(
    stored.get("start_offset"), stored.get("end_offset")
  ):
    stored["start_offset"] = None
    stored["end_offset"] = None
  return stored


def stored_citation_from_dict(item : dict) -> Citation | None:
  """把一行 message_citations 的 dict 还原成 Citation；非法行返回 None。

  先按 ``Citation`` 的字段名过滤键再构造：库里将来多一列时不会因此整条丢弃。
  字段缺失、类型非法或偏移不成对（只有一端 / 反向 / 负数）时返回 None，
  由调用方丢弃并留诊断日志——一条脏数据不得把整个会话历史变成 500。

  偏移的成对校验在这里复述一遍（写前规整与库级 CHECK 各有一道）：
  读侧兜底防的是历史脏数据，不能假定所有行都经过当前写路径。
  """
  if not isinstance(item, dict):
    return None
  filtered = {
    key: value for key, value in item.items() if key in _CITATION_FIELD_NAMES
  }
  try:
    citation = Citation(**filtered)
  except (TypeError, ValueError):
    return None
  if not _offsets_are_consistent(citation.start_offset, citation.end_offset):
    return None
  return citation


def _offsets_are_consistent(
  start_offset : int | None,
  end_offset : int | None,
) -> bool:
  """判断一对偏移是否可信：同生同灭、非负且区间非空。"""
  if start_offset is None and end_offset is None:
    return True
  if not isinstance(start_offset, int) or not isinstance(end_offset, int):
    return False
  if isinstance(start_offset, bool) or isinstance(end_offset, bool):
    return False
  return start_offset >= 0 and end_offset > start_offset


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
  def _visible_citations(stored : MessageRecord) -> list[Citation]:
    """把库里的引用 dict 逐条还原成 Citation；非法行走丢弃并留诊断日志。"""
    citations : list[Citation] = []
    for item in stored.citations:
      citation = stored_citation_from_dict(item)
      if citation is None:
        logger.warning(
          "历史引用字段不合法，已丢弃该条引用（会话消息 seq %s）",
          stored.sequence,
        )
        continue
      citations.append(citation)
    return citations

  @classmethod
  def _to_visible_message(cls, stored: MessageRecord) -> ConversationHistoryMessage | None:
    """把一条内部消息转换为可展示消息；不可展示时返回 None。

    安全过滤规则：
    - 只允许 user 问题与无 tool_calls 的 assistant 最终回答；
    - 空内容（含全空白）一律排除；
    - system/tool 消息、带工具调用的中间 assistant 消息、
      reasoning_content、tool_calls、工具参数/结果等内部字段全部不返回；
    - 结构化引用（citations）与完整性标记只跟着「可见的最终回答」返回：
      user 消息恒为空引用，中间助手消息即使库里有引用行也不返回。
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
        citations=cls._visible_citations(stored),
        answer_incomplete=stored.answer_incomplete,
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

      new_messages = messages[new_messages_start:]
      self._store.append_messages(
        organization_id=context.organization_id,
        user_id=context.user_id,
        conversation_id=conversation_id,
        messages=new_messages,
        display=self._display_for_turn(new_messages, response),
      )
      return response

  @staticmethod
  def _display_for_turn(
    new_messages : list[dict],
    response : LLMResponse,
  )-> list[MessageDisplay | None]:
    """为本轮消息构造展示数据：只有最终回答带引用与完整性标记。

    从末尾向前找第一条「无 tool_calls 的 assistant」消息——runner 保证它是本轮
    最后一条；找到时**无论有没有引用都填**（引用为空就只写 answer_incomplete，
    不插引用行），找不到（理论上不会发生）时全部传 None，不报错。
    """
    display : list[MessageDisplay | None] = [None] * len(new_messages)
    for index in range(len(new_messages) - 1, -1, -1):
      message = new_messages[index]
      if message.get("role") != "assistant" or message.get("tool_calls"):
        continue
      citations = []
      for citation in response.citations:
        stored = citation_to_stored_dict(citation)
        # 空片段正文会被库级 CHECK 拒绝（document_chunks 有同名约束）：
        # 这种引用直接丢弃，而不是写一条空证据行。
        if not str(stored.get("content") or "").strip():
          logger.warning("引用片段正文为空，已丢弃该条引用")
          continue
        citations.append(stored)
      display[index] = MessageDisplay(
        citations=tuple(citations),
        answer_incomplete=response.answer_incomplete,
      )
      break
    return display
