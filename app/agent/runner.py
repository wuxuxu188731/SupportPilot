from typing import Any, Callable, Protocol

from app.agent.events import AgentEvent
from app.core.config import MODEL_NAME
from app.schemas.chat import LLMResponse

import json
import time


DEFAULT_MAX_TOOL_ROUNDS = 20


class AgentToolRoundLimitError(RuntimeError):
  pass


class AgentToolFunction(Protocol):
  def __call__(self, **arguments: Any) -> Any:
    raise NotImplementedError


def stringify_tool_result(result : Any)->str:
  if isinstance(result,str):
    return result
  else:
    return json.dumps(result,ensure_ascii=False,default=str)
  
"""解析LLM调用工具时传入的参数"""
def parse_tool_arguments(raw_arguments:str | None)->dict:
  try :
    result = json.loads(raw_arguments or "{}")
  except (json.JSONDecodeError,TypeError) as exc:
    raise ValueError("工具参数解析失败")
  
  if not isinstance(result,dict):
    raise ValueError("工具参数解析失败，参数必须是JSON Object")

  return result

"""
构造错误返回
"""
def build_error_result(tool_call_id,error_message : str)->dict:
  return {
    "role":"tool",
    "tool_call_id":tool_call_id,
    "content":json.dumps(
      {
        "OK":False,
        "error":error_message,
      },ensure_ascii=False,default=str
    )
  }

def run_one_turn(
  *,
  messages: list[dict],
  client: Any,
  tool_definitions: list[dict],
  tool_functions: dict[str, AgentToolFunction],
  on_event: Callable[[AgentEvent], None] | None = None,
  model_name: str = MODEL_NAME,
  max_tool_rounds: int = DEFAULT_MAX_TOOL_ROUNDS,
) -> LLMResponse:
  if max_tool_rounds <= 0:
    raise ValueError("max_tool_rounds must be positive")
  tool_rounds = 0
  events : list[AgentEvent] = []

  #this function is used to add agent_event into events && extension operation
  def emit(event : AgentEvent):
    events.append(event)
    #database connection,SSE,websocket
    if(on_event is not None):
      on_event(event)


  while True:
    response = client.chat.completions.create(
      model=model_name,
      messages=messages,
      tools=tool_definitions,
      extra_body={
        "thinking":{
          "type":"enabled"
        }
      }
    )

    llm_res = response.choices[0].message.content
    reason_content = response.choices[0].message.reasoning_content

    #no tool was called
    if not response.choices[0].message.tool_calls:
      messages.append(
        {
          "role":"assistant",
          "content":llm_res,
          "reasoning_content":reason_content,
        }
      )
      return LLMResponse(llm_answer=llm_res,llm_reasoning_content=reason_content,events=events) 
    
    if tool_rounds >= max_tool_rounds:
      raise AgentToolRoundLimitError("maximum tool rounds exceeded")
    tool_rounds += 1

    #tools were called
    """
    将模型发送的调用信息加入messages->遍历工具列表(对每一个工具进行)
    ->参数解析(异常处理:参数解析失败)
    ->从functionMap里面获取函数对象(异常处理:工具函数不存在)
    ->执行函数(异常处理:函数调用失败)
    ->返回工具结果(将函数的结果append进入messages)
    """
    messages.append(
      {
        "role":"assistant",
        "content":llm_res,
        "reasoning_content":reason_content,
        "tool_calls":[
          {
            "id":tool_call.id,
            "type":"function",
            "function":{
              "name":tool_call.function.name,
              "arguments":tool_call.function.arguments
            },
          }
        for tool_call in response.choices[0].message.tool_calls
        ]
      }
    )
    for tool_call in response.choices[0].message.tool_calls:
      func_name = tool_call.function.name
      try :
        func_arguments = parse_tool_arguments(tool_call.function.arguments)
      except ValueError as exc:
        error_message = f"工具参数解析失败"
        emit(
          AgentEvent(
            type="tool_call.failed",
            tool_call_id=tool_call.id,
            tool_call_name=func_name,
            tool_call_arguments={},
            error=error_message
          )
        )
        messages.append(build_error_result(tool_call_id=tool_call.id,error_message=error_message))
        continue
      emit(
        AgentEvent(
          type="tool_call.requested",
          tool_call_id=tool_call.id,
          tool_call_name=func_name,
          tool_call_arguments=func_arguments
        )
      )

      func = tool_functions.get(func_name)
      #tool is not avaliable
      if func is None:
        error_message = f"未注册的工具"
        emit(
          AgentEvent(
            type="tool_call.failed",
            tool_call_id=tool_call.id,
            tool_call_name=func_name,
            tool_call_arguments=func_arguments,
            error=error_message
          )
        )
        messages.append(build_error_result(tool_call_id=tool_call.id,error_message=error_message))
        continue

      #tool is avaliable
      emit(
        AgentEvent(
          type="tool_call.started",
          tool_call_id=tool_call.id,
          tool_call_name=func_name,
          tool_call_arguments=func_arguments
        )
      )

      start_at = time.perf_counter()
      try:
        func_result = func(**func_arguments)
        duration_ms = (time.perf_counter()-start_at)*1000
      except Exception as exc:
        error_message = f"{type(exc).__name__}:{exc}"
        duration_ms = (time.perf_counter()-start_at)*1000
        emit(AgentEvent(
          type="tool_call.failed", 
          tool_call_id=tool_call.id, 
          tool_call_name=func_name, 
          tool_call_arguments=func_arguments,
          error=error_message,
          duration_ms=duration_ms
          )
        )
        messages.append(build_error_result(tool_call_id=tool_call.id,error_message=error_message))
        continue
      
      #执行工具没有抛出异常，工具正常执行，添加事件，将消息append进入messages
      emit(
        AgentEvent(
          type="tool_call.completed",
          tool_call_id=tool_call.id,
          tool_call_name=func_name,
          tool_call_arguments=func_arguments,
          result=func_result,
          duration_ms=duration_ms
        )
      )
      messages.append({
        "role":"tool",
        "tool_call_id":tool_call.id,
        "content":stringify_tool_result(func_result)
      })
