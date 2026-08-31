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
