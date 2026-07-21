from pydantic import BaseModel,Field

from app.agent.events import AgentEvent

class LLMResponse(BaseModel):
  """llm_tool_call is function name which llm called"""
  llm_answer : str | None = None
  llm_reasoning_content : str | None = None
  events : list[AgentEvent] = Field(default_factory=list) 


class SystemPrompt(BaseModel):
  prompt : str


class UserQuestion(BaseModel):
  question : str