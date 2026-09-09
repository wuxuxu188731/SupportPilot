from fastapi import APIRouter, HTTPException, Depends, Query
from collections.abc import Callable

from app.application.organization_service import TenantContext


from app.schemas.chat import (
  ChatRequest,
  ConversationCreated,
  ConversationHistoryResponse,
  ConversationListItem,
  CreateConversationRequest,
  LLMResponse,
  SystemPromptUpdated,
  UpdateSystemPromptRequest,
)
from app.sessions.base import ConversationNotFoundError
from app.application.chat_service import ChatService


def creat_conversation_router(
  *,
  chat_service : ChatService,
  get_current_tenant : Callable[..., TenantContext]
) -> APIRouter:
    router = APIRouter()

    @router.post("/conversations/", response_model=ConversationCreated, status_code=201, tags=["创建新会话","设置会话的系统提示词"])
    def create_conversation(
      request : CreateConversationRequest,
      context : TenantContext = Depends(get_current_tenant)
    )->ConversationCreated:

      system_prompt = (
        request.system_prompt.strip() if request.system_prompt else None
      )
      conversation = chat_service.create_conversation(
        context=context,
        system_prompt=system_prompt
      )
      return ConversationCreated(conversation_id=conversation.conversation_id)

    @router.get("/conversations/", response_model=list[ConversationListItem], tags=["查询会话列表"])
    def list_conversations(
      context : TenantContext = Depends(get_current_tenant),
      limit: int = Query(default=50, ge=1, le=100),
      offset: int = Query(default=0, ge=0),
    )->list[ConversationListItem]:
      # 分页参数由 FastAPI Query 校验（limit 1–100，offset ≥0），
      # 只返回当前企业与当前用户自己的会话，标题由服务端实时推导。
      return chat_service.list_conversations(
        context=context,
        limit=limit,
        offset=offset,
      )

    @router.get(
      "/conversations/{conversation_id}/messages/",
      response_model=ConversationHistoryResponse,
      tags=["查询会话历史消息"]
    )
    def get_conversation_messages(
      conversation_id : str,
      context : TenantContext = Depends(get_current_tenant)
    )->ConversationHistoryResponse:
      conversation_id = conversation_id.strip()
      if not conversation_id:
        raise HTTPException(status_code=422, detail="conversation_id must not be blank")
      try:
        return chat_service.get_history(
          context=context,
          conversation_id=conversation_id
        )
      except ConversationNotFoundError as exc:
        # 会话不存在、跨用户或跨企业统一按 404 返回，不泄露归属信息
        raise HTTPException(status_code=404, detail="conversation not found error") from exc

    @router.post("/conversations/{conversation_id}/chat/", response_model=LLMResponse, tags=["向模型聊天"])
    def chat(
      conversation_id : str,
      request : ChatRequest,
      context : TenantContext = Depends(get_current_tenant)
    )->LLMResponse:
      conversation_id = conversation_id.strip()

      question = request.question.strip()
      if not conversation_id:
        raise HTTPException(status_code=422, detail="conversation_id must not be blank")
      if not question:
        raise HTTPException(status_code=422, detail="question must not be blank")
      try:
        return chat_service.chat(
          context=context,
          conversation_id=conversation_id,
          question=question
        )
      except ConversationNotFoundError as exc:
        raise HTTPException(status_code=404, detail="conversation not found error") from exc

    @router.put("/conversations/{conversation_id}/system-prompt/")
    def update_system_prompt(
      conversation_id : str,
      request : UpdateSystemPromptRequest,
      context : TenantContext = Depends(get_current_tenant)
    )->SystemPromptUpdated:
      conversation_id = conversation_id.strip()
      prompt = request.system_prompt.strip()

      if not conversation_id:
        raise HTTPException(status_code=422, detail="conversation_id must not be blank")
      if not prompt:
        raise HTTPException(status_code=422, detail="prompt must be not blank")

      try:
        chat_service.update_system_prompt(
          context=context,
          conversation_id=conversation_id,
          system_prompt=prompt
        )
      except ConversationNotFoundError as exc:
        raise HTTPException(status_code=404, detail="conversation not found error") from exc

      return SystemPromptUpdated(updated=True)

    return router
