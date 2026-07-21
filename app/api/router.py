from typing import Any, Callable

from fastapi import APIRouter, HTTPException

from app.schemas.chat import LLMResponse, SystemPrompt, UserQuestion


def creat_router(
  *,
  messages:list[dict],
  run_agent:Callable[...,LLMResponse],
  save_history:Callable[[list[dict]],None]
) -> APIRouter:
    router = APIRouter()

    @router.post("/setSys/",tags=["设置模型的提示词"])
    async def set_system_prompt(prompt:SystemPrompt) -> bool:
      system_prompt = prompt.prompt.strip()
      if not system_prompt:
          raise HTTPException(status_code=401,detail="系统提示词为空")
      messages.append({"role":"system","content":prompt.prompt})
      return True
    
    @router.post("/chat/",tags=["向模型聊天"],response_model=LLMResponse)
    def chat(question:UserQuestion)->LLMResponse:
      messages.append({"role":"user","content":question.question})
      response = run_agent(messages = messages)
      save_history(messages)
      return response
    
    return router