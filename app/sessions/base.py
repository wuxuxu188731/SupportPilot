from dataclasses import dataclass
from typing import Protocol

@dataclass(frozen = True)
class Conversation:
  conversation_id : str
  organization_id : str
  user_id : str
  system_prompt : str | None


@dataclass(frozen = True)
class ConversationRecord:
  """会话持久化记录（含时间与首条用户消息正文），供会话列表与历史头部使用。"""

  conversation_id : str  # 会话唯一标识
  system_prompt : str | None  # 会话级附加系统提示词；未设置时为 None
  created_at : str  # 会话创建时间（UTC 文本，供界面展示）
  updated_at : str  # 会话最近活动时间（UTC 文本，用于倒序排序与展示）
  first_user_content : str | None  # 首条用户消息正文（标题推导用）；尚无消息时为 None


@dataclass(frozen = True)
class MessageRecord:
  """一条持久化消息的原始记录（含序号、角色与完整内部载荷）。

  内部载荷可能包含 reasoning_content、tool_calls、工具结果等
  仅供模型上下文使用的字段，禁止未经过滤直接暴露给 HTTP。
  """

  sequence : int  # 消息在会话内的原始序号（seq），从 1 开始递增
  role : str  # 消息角色（payload 中 role 的冗余列，便于 SQL 过滤）
  payload : dict  # 原始消息载荷（内部存储形态，可能含内部字段）
  created_at : str  # 消息写入时间（UTC 文本）


class ConversationNotFoundError(LookupError):
  pass

class SessionStore(Protocol):
  def create_conversation(
    self,
    *,
    organization_id : str,
    user_id : str,
    system_prompt : str | None = None
  )->Conversation:
    raise NotImplementedError

  def get_conversation(
    self,
    *,
    organization_id : str,
    user_id : str,
    conversation_id : str
  )->Conversation:
    raise NotImplementedError

  def list_conversations(
    self,
    *,
    organization_id : str,
    user_id : str,
    limit : int,
    offset : int,
  )->list[ConversationRecord]:
    """列出当前企业、当前用户自己的会话（按更新时间倒序、分页）。"""
    raise NotImplementedError

  def get_conversation_record(
    self,
    *,
    organization_id : str,
    user_id : str,
    conversation_id : str
  )->ConversationRecord:
    """读取单会话的持久化记录；不属于当前用户/企业时抛 NotFound。"""
    raise NotImplementedError

  def load_message_records(
    self,
    *,
    organization_id : str,
    user_id : str,
    conversation_id : str,
  )->list[MessageRecord]:
    """读取某会话的全部原始消息记录（按 seq 升序）；归属不符时抛 NotFound。"""
    raise NotImplementedError

  def update_system_prompt(
    self,
    *,
    organization_id : str,
    user_id : str,
    conversation_id : str,
    system_prompt : str
  )->None:
    raise NotImplementedError

  def load_messages(
    self,
    *,
    organization_id : str,
    user_id : str,
    conversation_id : str,
  )->list[dict]:
    raise NotImplementedError

  def append_messages(
    self,
    *,
    organization_id : str,
    user_id : str,
    conversation_id : str,
    messages : list[dict],
  )->None:
    raise NotImplementedError
