from typing import Callable

from app.concurrency.conversation_locks import ConversationLockRegistry
from app.schemas.chat import LLMResponse
from app.sessions.base import Conversation,SessionStore

#获取会话锁 run_agent 持久化
class ChatService:
  def __init__(
    self,
    *,
    store : SessionStore,
    run_agent : Callable[...,LLMResponse],
    locks : ConversationLockRegistry
  ):
    self._store = store
    self._run_agent = run_agent
    self._locks = locks

  def create_conversation(
    self,
    *,
    user_id : str,
    system_prompt : str | None = None
  )-> Conversation:
    return self._store.create_conversation(
      user_id=user_id,
      system_prompt=system_prompt
    )
  
  def update_system_prompt(
    self,
    *,
    user_id : str,
    conversation_id : str,
    system_prompt : str
  )-> None :
    with self._locks.acquire(conversation_id=conversation_id):
      self._store.update_system_prompt(
        user_id=user_id,
        conversation_id=conversation_id,
        system_prompt=system_prompt
      )

  #聊天 ：获取会话锁->get_conversations->load_messages->append_messages(持久化)
  def chat(
    self,
    *,
    user_id : str,
    conversation_id : str,
    question : str
  )-> LLMResponse:
    with self._locks.acquire(conversation_id=conversation_id):
      #获取会话
      conversation = self._store.get_conversation(
        user_id=user_id,
        conversation_id=conversation_id
      )
      #加载会话的历史记录
      history = self._store.load_messages(
        conversation_id=conversation.conversation_id,
        user_id=user_id,
      )
      messages : list[dict] = []
      if conversation.system_prompt:
        messages.append({"role":"system","content":conversation.system_prompt})
      messages.extend(history)
      new_messages_start = len(messages)
      if question.strip():
        messages.append({"role":"user","content":question})

      response = self._run_agent(messages = messages)

      self._store.append_messages(
        conversation_id=conversation_id,
        user_id=user_id,
        messages=messages[new_messages_start:]
      )
      return response

    

    