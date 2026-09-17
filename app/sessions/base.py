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
class MessageDisplay:
  """一条回答的**展示数据**（与模型上下文载荷完全分离）。

  仅供展示与历史读取使用，**绝不进入模型上下文**：它不写进 messages.payload_json，
  也不会出现在 load_messages() 的返回值里（那条路径只服务模型请求）。
  """

  citations : tuple[dict, ...] = ()
  # 该回答的知识引用，每个 dict 的键等于 MESSAGE_CITATION_COLUMNS
  # （即 Citation.public_dict() 的全集，含证据片段快照 content）；
  # 顺序即展示次序，写入时由 ordinal 记录。无引用时为空元组。
  answer_incomplete : bool = False
  # 该回答生成时的引用完整性标记：True 表示证据引用可能不完整，
  # 界面须给出「谨慎采用」提示；无检索的纯聊天回答写 False。


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
  citations : tuple[dict, ...] = ()
  # 该回答持久化的知识引用（来自 message_citations 表，按 ordinal 升序）；
  # 非最终回答（用户消息、中间助手消息、system/tool）与升级前的历史行均为空元组。
  answer_incomplete : bool = False
  # 该回答生成时的引用完整性标记；库里为 NULL（没有记录）或 0 时都是 False。


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
    display : list[MessageDisplay | None] | None = None,
  )->None:
    """追加一轮消息，可选附带同长度的展示数据。

    ``display`` 与 ``messages`` **等长**（第 i 项对应第 i 条消息），并且与消息
    **同一事务**写入；为 None 或该项为 None 表示该消息没有展示数据。
    展示数据不进入 ``payload_json``，因此不会污染模型上下文。
    """
    raise NotImplementedError
