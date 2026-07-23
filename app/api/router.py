from fastapi import APIRouter, HTTPException

from app.schemas.chat import (
  ChatRequest,
  ConversationCreated,
  CreateConversationRequest,
  LLMResponse, 
  SystemPromptUpdated, 
  UpdateSystemPromptRequest, 
)
from app.sessions.base import ConversationNotFoundError
from app.application.chat_service import ChatService


def creat_router(
  *,
  chat_service : ChatService
) -> APIRouter:
    router = APIRouter()

    @router.post("/conversations/", response_model=ConversationCreated, status_code=201, tags=["创建新会话","设置会话的系统提示词"])
    def create_conversation(request : CreateConversationRequest)->ConversationCreated:
      user_id = request.user_id.strip()
      if not user_id:
        raise HTTPException(status_code=422, detail="user_id must not be blank")
      system_prompt = (
        request.system_prompt.strip() if request.system_prompt else None
      )
      conversation = chat_service.create_conversation(
        user_id = user_id,
        system_prompt = system_prompt
      )
      return ConversationCreated(conversation_id=conversation.conversation_id)
    
    @router.post("/conversations/{conversation_id}/chat/", response_model=LLMResponse, tags=["向模型聊天"])
    def chat(conversation_id : str, request : ChatRequest)->LLMResponse:
      user_id = request.user_id.strip()
      conversation_id = conversation_id.strip()
      question = request.question.strip()
      if not user_id:
        raise HTTPException(status_code=422, detail="user_id must not be blank")
      if not conversation_id:
        raise HTTPException(status_code=422, detail="conversation_id must not be blank")
      if not question:
        raise HTTPException(status_code=422, detail="question must not be blank")
      try:
        return chat_service.chat(
          user_id=user_id,
          conversation_id=conversation_id,
          question=question
        )
      except ConversationNotFoundError as exc:
        raise HTTPException(status_code=404, detail="conversation not found error") from exc
      
    @router.put("/conversations/{conversation_id}/system-prompt/")
    def update_system_prompt(conversation_id : str, request : UpdateSystemPromptRequest)->SystemPromptUpdated:
      user_id = request.user_id.strip()
      conversation_id = conversation_id.strip()
      prompt = request.system_prompt.strip()
      if not user_id:
        raise HTTPException(status_code=422, detail="user_id must not be blank")
      if not conversation_id:
        raise HTTPException(status_code=422, detail="conversation_id must not be blank")
      if not prompt:
        raise HTTPException(status_code=422, detail="prompt must be not blank")
      
      try:
        chat_service.update_system_prompt(
          user_id=user_id,
          conversation_id=conversation_id,
          system_prompt=prompt
        )
      except ConversationNotFoundError as exc:
        raise HTTPException(status_code=404, detail="conversation not found error") from exc
      
      return SystemPromptUpdated(updated=True)
    
    return router