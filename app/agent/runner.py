from typing import Any, Callable, Protocol
from dataclasses import dataclass
import re

from app.agent.events import AgentEvent
from app.core.config import MODEL_NAME
from app.schemas.chat import LLMResponse
from app.knowledge.results import Citation, RetrievalSummary

import json
import time


DEFAULT_MAX_TOOL_ROUNDS = 20
_CITATION_PATTERN = re.compile(r"\[(C[1-9]\d*)\]")

# 会创建待审批提案的工具名，用于归一化 LLMResponse.pending_approvals
PENDING_APPROVAL_TOOLS = ("propose_refund", "propose_compensation")
# 提案结果中必须存在的展示字段（设计 12.5），缺失时不做归一化
_PENDING_APPROVAL_FIELDS = (
    "run_id",
    "proposal_id",
    "approval_id",
    "action_type",
    "status",
    "amount_cents",
    "currency",
    "resume_required",
    "error_code",
)

_MODEL_RESPONSE_UNAVAILABLE_MESSAGE = (
    "提案已创建，但模型暂时无法生成完整回复。"
    "请根据结构化待审批信息查看当前状态，必要时由管理员恢复工作流。"
)


def extract_pending_approval(
    tool_name: str,
    result: Any,
) -> dict | None:
    """从提案工具结果中提取结构化待审批摘要。

    只认 ``propose_refund`` / ``propose_compensation`` 的成功结果，
    且结果必须携带设计 12.5 的全部展示字段；其他工具与失败结果返回
    ``None``，不影响既有事件与消息处理。
    """
    if tool_name not in PENDING_APPROVAL_TOOLS:
        return None
    if not isinstance(result, dict) or result.get("ok") is not True:
        return None
    data = result.get("data")
    if not isinstance(data, dict):
        return None
    if not all(field in data for field in _PENDING_APPROVAL_FIELDS):
        return None
    return {field: data[field] for field in _PENDING_APPROVAL_FIELDS}


@dataclass(frozen=True)
class CitationValidationResult:
  citations: tuple[Citation, ...]
  retrieval_summary: RetrievalSummary | None
  unknown_citation_ids: tuple[str, ...]
  missing_required_citation: bool
  answer_incomplete: bool


def validate_final_citations(
  answer: str | None,
  knowledge_payload: dict | None,
) -> CitationValidationResult:
  found: list[str] = []
  for citation_id in _CITATION_PATTERN.findall(answer or ""):
    if citation_id not in found:
      found.append(citation_id)
  data = (knowledge_payload or {}).get("data") or {}
  allowed = {
    item.get("citation_id"): item
    for item in data.get("citations", [])
    if isinstance(item, dict) and item.get("citation_id")
  }
  citations = tuple(
    Citation(**allowed[citation_id])
    for citation_id in found
    if citation_id in allowed
  )
  unknown = tuple(
    citation_id for citation_id in found if citation_id not in allowed
  )
  summary_payload = data.get("retrieval_summary")
  summary = (
    RetrievalSummary(**summary_payload)
    if isinstance(summary_payload, dict)
    else None
  )
  missing_required = (
    data.get("evidence_status") == "sufficient"
    and bool(allowed)
    and not citations
  )
  incomplete = bool(unknown) or missing_required
  return CitationValidationResult(
    citations=citations,
    retrieval_summary=summary,
    unknown_citation_ids=unknown,
    missing_required_citation=missing_required,
    answer_incomplete=incomplete,
  )


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
  knowledge_payload: dict | None = None
  knowledge_call_id: str | None = None
  pending_approvals: list[dict] = []

  # 统一收集 Agent 事件，并按需通知外部监听器。
  def emit(event : AgentEvent):
    events.append(event)
    # 外部监听器可用于数据库记录、SSE 或 WebSocket 推送。
    if(on_event is not None):
      on_event(event)

  def pending_approval_fallback() -> LLMResponse:
    """在提案已持久化后返回确定性降级答复。"""
    messages.append(
      {
        "role": "assistant",
        "content": _MODEL_RESPONSE_UNAVAILABLE_MESSAGE,
        "reasoning_content": None,
      }
    )
    return LLMResponse(
      llm_answer=_MODEL_RESPONSE_UNAVAILABLE_MESSAGE,
      events=events,
      answer_incomplete=True,
      pending_approvals=pending_approvals,
    )


  while True:
    try:
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
    except Exception:
      # 提案属于已经提交的持久化业务事实。提案创建后的模型调用失败时，
      # 返回确定性降级答复，避免客户端收到错误却无法取得 Run/Approval ID。
      if not pending_approvals:
        raise
      return pending_approval_fallback()

    llm_res = response.choices[0].message.content
    reason_content = response.choices[0].message.reasoning_content

    # 模型未调用工具，本轮结束并校验知识引用。
    if not response.choices[0].message.tool_calls:
      messages.append(
        {
          "role":"assistant",
          "content":llm_res,
          "reasoning_content":reason_content,
        }
      )
      validation = validate_final_citations(llm_res, knowledge_payload)
      if validation.answer_incomplete:
        emit(AgentEvent(
          type="citation.invalid",
          tool_call_id=knowledge_call_id or "final-answer",
          tool_call_name=(
            "search_knowledge" if knowledge_call_id else "citation_validation"
          ),
          result={
            "unknown_citation_ids": list(validation.unknown_citation_ids),
            "missing_required_citation": validation.missing_required_citation,
          },
        ))
      return LLMResponse(
        llm_answer=llm_res,
        llm_reasoning_content=reason_content,
        events=events,
        citations=list(validation.citations),
        retrieval_summary=validation.retrieval_summary,
        answer_incomplete=validation.answer_incomplete,
        pending_approvals=pending_approvals,
      )
    
    if tool_rounds >= max_tool_rounds:
      # 达到工具轮次上限时，已创建的提案仍必须返回给调用方。
      if pending_approvals:
        return pending_approval_fallback()
      raise AgentToolRoundLimitError("maximum tool rounds exceeded")
    tool_rounds += 1

    # 模型调用了工具，依次解析、执行并回填结果。
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
      # 工具未注册时返回稳定失败结果。
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

      # 工具已注册，记录开始事件后执行。
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
      except Exception:
        # 未预期异常只返回稳定错误码，禁止向模型和客户端泄漏底层细节。
        error_message = "TOOL_EXECUTION_FAILED"
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
      if (
        func_name == "search_knowledge"
        and knowledge_payload is None
        and isinstance(func_result, dict)
        and isinstance((func_result.get("data") or {}).get("retrieval_summary"), dict)
      ):
        knowledge_payload = func_result
        knowledge_call_id = tool_call.id

      # 提案工具成功结果归一化为结构化待审批摘要（设计 12.5），
      # 调用方不需要从自然语言或事件中解析 Approval ID。
      pending = extract_pending_approval(func_name, func_result)
      if pending is not None:
        pending_approvals.append(pending)
      
      # 工具正常完成后记录事件，并把结构化结果加入消息历史。
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
