from dataclasses import dataclass
from typing import Protocol

@dataclass(frozen = True)
class Conversation:
  conversation_id : str
  user_id : str
  system_prompt : str


class ConversationNotFoundError(LookupError):
  pass

class SessionStore(Protocol):
  def create_conversation(
    self,
    *,
    user_id : str,
    system_prompt : str | None = None
  )->Conversation:
    raise NotImplementedError
  
  def get_conversation(
    self,
    *,
    user_id: str,
    conversation_id : str
  )->Conversation:
    raise NotImplementedError
  
  def update_system_prompt(
    self,
    *,
    user_id : str,
    conversation_id : str,
    system_prompt : str
  )->None:
    raise NotImplementedError
  
  def load_messages(
    self,
    *,
    conversation_id : str ,
    user_id : str,
  )->list[dict]:
    raise NotImplementedError
  
  def append_messages(
    self,
    *,
    conversation_id : str,
    user_id : str,
    messages : list[dict],
  )->None:
    raise NotImplementedError
