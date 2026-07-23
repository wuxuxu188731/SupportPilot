from pydantic import BaseModel,Field

from app.agent.events import AgentEvent

class LLMResponse(BaseModel):
  """llm_tool_call is function name which llm called"""
  llm_answer : str | None = None
  llm_reasoning_content : str | None = None
  events : list[AgentEvent] = Field(default_factory=list) 


class CreateConversationRequest(BaseModel):
  user_id : str = Field(min_length=1)
  system_prompt : str | None = None 

class ConversationCreated(BaseModel):
  conversation_id : str

class ChatRequest(BaseModel):
  user_id : str = Field(min_length=1)
  question : str = Field(min_length=1)

class UpdateSystemPromptRequest(BaseModel):
  user_id : str = Field(min_length=1)
  system_prompt : str = Field(min_length=1)

class SystemPromptUpdated(BaseModel):
  updated : bool