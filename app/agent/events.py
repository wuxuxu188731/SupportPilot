from typing import Literal,Any
from pydantic import BaseModel,Field
from datetime import datetime

class AgentEvent(BaseModel):
  type : Literal[
    "tool_call.requested",
    "tool_call.started",
    "tool_call.completed",
    "tool_call.failed",
    "citation.invalid"
  ]
  timestamp : datetime = Field(default_factory=datetime.now)
  tool_call_id : str
  tool_call_name : str
  tool_call_arguments : dict[str,Any] = Field(default_factory=dict)
  result : Any | None = None
  error : str | None = None
  duration_ms : float | None = None
