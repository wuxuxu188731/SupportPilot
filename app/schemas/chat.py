from pydantic import BaseModel,Field,ConfigDict

from app.agent.events import AgentEvent
from app.knowledge.results import Citation, RetrievalSummary

class LLMResponse(BaseModel):
  """llm_tool_call is function name which llm called"""
  llm_answer : str | None = None
  llm_reasoning_content : str | None = None
  events : list[AgentEvent] = Field(default_factory=list) 
  citations: list[Citation] = Field(default_factory=list)
  retrieval_summary: RetrievalSummary | None = None
  answer_incomplete: bool = False


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
