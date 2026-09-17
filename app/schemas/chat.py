from typing import Literal

from pydantic import BaseModel,Field,ConfigDict

from app.agent.events import AgentEvent
from app.knowledge.results import Citation, RetrievalSummary


class PendingApproval(BaseModel):
  """聊天响应中的结构化待审批提案摘要（设计 12.5）。

  调用方不应从自然语言或通用事件中解析 Approval ID，应以本结构为准。
  """
  run_id : str  # 动作 Run 标识，可传给 get_action_status 查询
  proposal_id : str  # 提案标识
  approval_id : str  # 审批标识，管理员通过审批 API 处理
  action_type : str  # 动作类型：refund 或 compensation
  status : str  # Run 当前状态，等待审批时为 awaiting_approval
  amount_cents : int  # 提案金额（分）
  currency : str  # 币种
  resume_required : bool = False  # 工作流首次启动失败时是否需要管理员显式恢复
  error_code : str | None = None  # 首次启动失败的稳定错误码，正常等待审批时为空


class LLMResponse(BaseModel):
  """单轮 Agent 调用响应，包含自然语言、事件、引用与待审批摘要。"""
  llm_answer : str | None = None
  llm_reasoning_content : str | None = None
  events : list[AgentEvent] = Field(default_factory=list) 
  citations: list[Citation] = Field(default_factory=list)
  retrieval_summary: RetrievalSummary | None = None
  answer_incomplete: bool = False
  pending_approvals: list[PendingApproval] = Field(default_factory=list)


class CreateConversationRequest(BaseModel):
  model_config = ConfigDict({"extra":"forbid"})
  system_prompt : str | None = None 

class ConversationCreated(BaseModel):
  conversation_id : str

class ChatRequest(BaseModel):
  model_config = ConfigDict({"extra":"forbid"})
  question : str = Field(min_length=1)

class UpdateSystemPromptRequest(BaseModel):
  model_config = ConfigDict({"extra":"forbid"})
  system_prompt : str = Field(min_length=1)

class SystemPromptUpdated(BaseModel):
  updated : bool


class ConversationListItem(BaseModel):
  """会话列表项（GET /conversations/ 响应元素）。

  title 不在数据库落库，而是由会话第一条用户消息实时推导，
  因此本模型不包含内部消息或任何原始 payload 字段。
  """
  conversation_id : str  # 会话唯一标识（服务端生成）
  title : str  # 会话标题：由第一条用户消息推导；空会话显示“新会话”
  created_at : str  # 会话创建时间（UTC 文本）
  updated_at : str  # 会话最近活动时间（UTC 文本）


class ConversationHistoryMessage(BaseModel):
  """历史消息中的单条安全消息（仅限用户问题与 Agent 最终回答）。

  该模型只承载适合最终用户查看的内容，绝不包含推理过程、
  工具调用、工具结果或任何内部 payload 字段。
  """
  sequence : int  # 消息在会话中的原始序号（seq），用于稳定排序展示
  role : Literal["user","assistant"]  # 消息角色：用户问题或助手最终回答
  content : str  # 消息正文（原文保留，仅保证非空；不含任何内部字段）
  created_at : str  # 消息写入时间（UTC 文本）
  citations : list[Citation] = Field(default_factory=list)
  # 该回答的知识引用（回答生成时随回答持久化）；顺序与实时响应一致
  # （即 [C#] 在回答正文中首次出现的顺序），字段与实时响应完全同形。
  # 用户消息与无引用回答恒为空数组。
  answer_incomplete : bool = False
  # 该回答生成时的引用完整性标记；True 时界面须给出「谨慎采用」提示。
  # 升级前的历史回答没有记录，按 False（即不提示）返回。


class ConversationHistoryResponse(BaseModel):
  """指定会话的安全历史消息响应（GET /conversations/{id}/messages/）。"""
  conversation_id : str  # 会话唯一标识
  system_prompt : str | None  # 会话当前的附加系统提示词；未设置为 null
  created_at : str  # 会话创建时间（UTC 文本）
  updated_at : str  # 会话最近活动时间（UTC 文本）
  messages : list[ConversationHistoryMessage]  # 按 seq 升序的安全历史消息，可为空数组
