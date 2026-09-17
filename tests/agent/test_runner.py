import pytest
import os
os.environ["DEEPSEEK_API_KEY"]="test-only-key"
from types import SimpleNamespace
from typing import Any



"""
agent编排测试，覆盖路径：
1.模型直接回答
2.单个工具成功。
3.工具返回字典，结果被转换成字符串。
4.参数 JSON 损坏，只产生 failed，不产生 completed。
5.调用了未注册工具。
6.工具函数抛异常，只产生 failed。
7.一次调用多个工具，其中一个成功、一个失败。
8.工具执行后，模型继续调用第二轮工具。
9.每条路径的 messages 顺序和事件顺序都正确。
"""

from app.agent.runner import (
  AgentToolRoundLimitError,
  run_one_turn,
  validate_final_citations,
)


def test_run_one_turn_uses_requested_model_name():
  received = []
  fake_message = SimpleNamespace(
    content="answer",
    reasoning_content="reasoning",
    tool_calls=None,
  )
  fake_response = SimpleNamespace(
    choices=[SimpleNamespace(message=fake_message)]
  )

  def create(**kwargs):
    received.append(kwargs)
    return fake_response

  fake_client = SimpleNamespace(
    chat=SimpleNamespace(
      completions=SimpleNamespace(create=create)
    )
  )

  run_one_turn(
    messages=[{"role": "user", "content": "hello"}],
    client=fake_client,
    tool_definitions=[],
    tool_functions={},
    model_name="test-support-model",
  )

  assert received[0]["model"] == "test-support-model"


def test_run_one_turn_does_not_write_model_or_tool_payloads_to_stdout(capsys):
  tool_result_sentinel = "SENSITIVE-TOOL-RESULT"
  final_answer_sentinel = "SENSITIVE-FINAL-ANSWER"
  reasoning_sentinel = "SENSITIVE-REASONING"
  tool_call = SimpleNamespace(
    id="call-sensitive",
    type="function",
    function=SimpleNamespace(name="sensitive_tool", arguments="{}"),
  )
  responses = iter([
    SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
      content="",
      reasoning_content=reasoning_sentinel,
      tool_calls=[tool_call],
    ))]),
    SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
      content=final_answer_sentinel,
      reasoning_content=reasoning_sentinel,
      tool_calls=None,
    ))]),
  ])
  fake_client = SimpleNamespace(
    chat=SimpleNamespace(
      completions=SimpleNamespace(create=lambda **kwargs: next(responses))
    )
  )

  run_one_turn(
    messages=[{"role": "user", "content": "handle sensitive data"}],
    client=fake_client,
    tool_definitions=[],
    tool_functions={"sensitive_tool": lambda: tool_result_sentinel},
  )

  stdout = capsys.readouterr().out
  assert tool_result_sentinel not in stdout
  assert final_answer_sentinel not in stdout
  assert reasoning_sentinel not in stdout


def test_run_one_turn_stops_after_max_tool_rounds():
  call_count = 0

  def create(**kwargs):
    nonlocal call_count
    call_count += 1
    tool_call = SimpleNamespace(
      id=f"call-{call_count}",
      type="function",
      function=SimpleNamespace(name="get_food", arguments="{}"),
    )
    message = SimpleNamespace(
      content="",
      reasoning_content="continue calling tools",
      tool_calls=[tool_call],
    )
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])

  fake_client = SimpleNamespace(
    chat=SimpleNamespace(
      completions=SimpleNamespace(create=create)
    )
  )

  with pytest.raises(
    AgentToolRoundLimitError,
    match="maximum tool rounds exceeded",
  ):
    run_one_turn(
      messages=[{"role": "user", "content": "loop"}],
      client=fake_client,
      tool_definitions=TOOL_DEFINITIONS,
      tool_functions=TOOL_FUNCTIONS,
      max_tool_rounds=2,
    )

  assert call_count == 3


def test_tool_round_limit_returns_existing_pending_approval():
  # 故障窗口：达到工具轮次上限时，如果本轮已经创建提案，必须返回
  # 结构化待审批摘要和降级答复，不能抛异常丢失已提交的业务事实。
  call_count = 0

  def create(**kwargs):
    nonlocal call_count
    call_count += 1
    tool_call = SimpleNamespace(
      id=f"call-{call_count}",
      type="function",
      function=SimpleNamespace(name="propose_refund", arguments="{}"),
    )
    message = SimpleNamespace(
      content="",
      reasoning_content="继续调用提案工具",
      tool_calls=[tool_call],
    )
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])

  fake_client = SimpleNamespace(
    chat=SimpleNamespace(
      completions=SimpleNamespace(create=create)
    )
  )
  proposal_result = {
    "ok": True,
    "data": {
      "run_id": "run-1",
      "proposal_id": "proposal-1",
      "approval_id": "approval-1",
      "action_type": "refund",
      "status": "awaiting_approval",
      "amount_cents": 1000,
      "currency": "CNY",
      "resume_required": False,
      "error_code": None,
    },
  }
  messages = [{"role": "user", "content": "申请退款"}]

  result = run_one_turn(
    messages=messages,
    client=fake_client,
    tool_definitions=[],
    tool_functions={"propose_refund": lambda: proposal_result},
    max_tool_rounds=1,
  )

  assert call_count == 2
  assert result.answer_incomplete is True
  assert result.pending_approvals[0].run_id == "run-1"
  assert messages[-1]["role"] == "assistant"
  assert "提案已创建" in messages[-1]["content"]


@pytest.mark.parametrize("max_tool_rounds", [0, -1])
def test_run_one_turn_requires_positive_tool_round_limit(max_tool_rounds):
  with pytest.raises(ValueError, match="max_tool_rounds must be positive"):
    run_one_turn(
      messages=[],
      client=SimpleNamespace(),
      tool_definitions=[],
      tool_functions={},
      max_tool_rounds=max_tool_rounds,
    )


def _knowledge_payload(*citation_ids, sufficient=True, with_offsets=True):
  citations = []
  for citation_id in citation_ids:
    citation = {
      "citation_id": citation_id,
      "document_id": "doc-1",
      "version_id": "version-1",
      "chunk_id": f"chunk-{citation_id}",
      "title": "Returns",
      "heading_path": "/window",
      "content": "trusted policy",
    }
    if with_offsets:
      # 与真实服务端一致：引用携带块在版本正文中的字符区间。
      citation["start_offset"] = 120
      citation["end_offset"] = 134
    citations.append(citation)
  return {
    "ok": sufficient,
    "data": {
      "result_code": "KNOWLEDGE_FOUND" if sufficient else None,
      "strategy": "multi",
      "evidence_status": "sufficient" if sufficient else "insufficient",
      "citations": citations if sufficient else [],
      "retrieval_summary": {
        "strategy": "multi",
        "round_count": 1,
        "evidence_status": "sufficient" if sufficient else "insufficient",
        "latency_ms": 3,
      },
    },
  }


def test_validate_final_citations_maps_known_ids_in_answer_order():
  validation = validate_final_citations(
    "Window [C2], packaging [C1], repeated [C2].",
    _knowledge_payload("C1", "C2"),
  )
  assert [item.citation_id for item in validation.citations] == ["C2", "C1"]
  assert validation.answer_incomplete is False


def test_validate_final_citations_rejects_unknown_and_missing_required():
  unknown = validate_final_citations(
    "Policy [C1] [C9]", _knowledge_payload("C1")
  )
  assert [item.citation_id for item in unknown.citations] == ["C1"]
  assert unknown.unknown_citation_ids == ("C9",)
  assert unknown.answer_incomplete is True

  missing = validate_final_citations(
    "The return window is seven days.", _knowledge_payload("C1")
  )
  assert missing.missing_required_citation is True
  assert missing.answer_incomplete is True


def test_validate_final_citations_preserves_chunk_offsets():
  # 保护行为：引用对象是从工具体返回的 dict 重建的（Citation(**dict)），
  # 因此新增的 start_offset / end_offset 必须能穿过这条链路保留下来——
  # 否则前端拿不到定位依据，也无法察觉（重建成功但不带偏移）。
  validation = validate_final_citations(
    "Window [C1].", _knowledge_payload("C1")
  )
  assert len(validation.citations) == 1
  citation = validation.citations[0]
  assert citation.start_offset == 120
  assert citation.end_offset == 134
  # HTTP 序列化必须一并输出这两个字段（前端只读 public_dict 的形状）。
  assert citation.public_dict()["start_offset"] == 120
  assert citation.public_dict()["end_offset"] == 134


def test_validate_final_citations_tolerates_legacy_payload_without_offsets():
  # 边界情况：不含偏移的历史/降级载荷不得让重建失败，偏移应回落为 None，
  # 由前端按「无法精确定位」处理。
  validation = validate_final_citations(
    "Window [C1].", _knowledge_payload("C1", with_offsets=False)
  )
  assert len(validation.citations) == 1
  assert validation.citations[0].start_offset is None
  assert validation.citations[0].end_offset is None


def test_runner_emits_content_free_citation_invalid_event():
  tool_call = SimpleNamespace(
    id="knowledge-call",
    function=SimpleNamespace(
      name="search_knowledge", arguments='{"question":"returns"}'
    ),
  )
  responses = iter([
    SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
      content="", reasoning_content=None, tool_calls=[tool_call]
    ))]),
    SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
      content="Policy [C1] [C9]", reasoning_content=None, tool_calls=None
    ))]),
  ])
  client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
    create=lambda **kwargs: next(responses)
  )))
  result = run_one_turn(
    messages=[{"role": "user", "content": "returns"}],
    client=client,
    tool_definitions=[],
    tool_functions={"search_knowledge": lambda **kwargs: _knowledge_payload("C1")},
  )
  assert [item.citation_id for item in result.citations] == ["C1"]
  assert result.answer_incomplete is True
  invalid = [event for event in result.events if event.type == "citation.invalid"]
  assert invalid[0].tool_call_id == "knowledge-call"
  assert invalid[0].result == {
    "unknown_citation_ids": ["C9"],
    "missing_required_citation": False,
  }
  assert "Policy" not in str(invalid[0].result)


def test_runner_flags_citation_without_knowledge_tool_with_fixed_identity():
  message = SimpleNamespace(
    content="Unsupported [C7]", reasoning_content=None, tool_calls=None
  )
  client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
    create=lambda **kwargs: SimpleNamespace(
      choices=[SimpleNamespace(message=message)]
    )
  )))
  result = run_one_turn(
    messages=[{"role": "user", "content": "guess"}],
    client=client,
    tool_definitions=[],
    tool_functions={},
  )
  invalid = [event for event in result.events if event.type == "citation.invalid"]
  assert invalid[0].tool_call_id == "final-answer"
  assert invalid[0].tool_call_name == "citation_validation"


def test_second_budget_denial_does_not_overwrite_first_knowledge_payload():
  # 保护行为（按新语义改写，意图不变）：第二次检索失败不得覆盖或污染
  # 第一次检索的引用——回答里的 [C1] 仍解析到第一次检索的证据，
  # 引用校验仍为完整（answer_incomplete=False）。
  tool_calls = [
    SimpleNamespace(id="call-1", function=SimpleNamespace(
      name="search_knowledge", arguments='{"question":"returns"}'
    )),
    SimpleNamespace(id="call-2", function=SimpleNamespace(
      name="search_knowledge", arguments='{"question":"again"}'
    )),
  ]
  responses = iter([
    SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
      content="", reasoning_content=None, tool_calls=[tool_calls[0]]
    ))]),
    SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
      content="", reasoning_content=None, tool_calls=[tool_calls[1]]
    ))]),
    SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
      content="Policy [C1]", reasoning_content=None, tool_calls=None
    ))]),
  ])
  service_results = iter([
    _knowledge_payload("C1"),
    {"ok": False, "error": {"code": "SEARCH_BUDGET_EXCEEDED", "message": "budget"}},
  ])
  client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
    create=lambda **kwargs: next(responses)
  )))
  result = run_one_turn(
    messages=[{"role": "user", "content": "returns"}],
    client=client,
    tool_definitions=[],
    tool_functions={"search_knowledge": lambda **kwargs: next(service_results)},
  )
  assert [item.citation_id for item in result.citations] == ["C1"]
  assert result.citations[0].chunk_id == "chunk-C1"
  assert result.retrieval_summary.evidence_status == "sufficient"
  assert result.answer_incomplete is False


# —— 一轮多次检索：编号续编 / 证据归属 / 合并摘要（设计 4.5） ——

def _second_knowledge_payload(
  citation_id="C1",
  *,
  document_id="doc-2",
  evidence_status="sufficient",
  latency_ms=7,
):
  """构造第二次检索的工具结果：文档与偏移都与第一次检索明显不同。"""
  return {
    "ok": True,
    "data": {
      "result_code": "KNOWLEDGE_FOUND",
      "strategy": "multi",
      "evidence_status": evidence_status,
      "citations": (
        [{
          "citation_id": citation_id,
          "document_id": document_id,
          "version_id": "version-2",
          "chunk_id": "chunk-2",
          "title": "退款到账",
          "heading_path": "/refund",
          "content": "退款 3 个工作日到账",
          "start_offset": 200,
          "end_offset": 212,
        }]
        if evidence_status != "failed"
        else []
      ),
      "retrieval_summary": {
        "strategy": "multi",
        "round_count": 1,
        "evidence_status": evidence_status,
        "latency_ms": latency_ms,
      },
    },
  }


def _run_two_knowledge_searches(
  final_answer,
  second_payload,
  *,
  first_sufficient=True,
):
  """跑一轮「两次 search_knowledge 都成功」的对话，返回结果与写回模型的消息。"""
  tool_calls = [
    SimpleNamespace(id="call-1", function=SimpleNamespace(
      name="search_knowledge", arguments='{"question":"returns"}'
    )),
    SimpleNamespace(id="call-2", function=SimpleNamespace(
      name="search_knowledge", arguments='{"question":"refund"}'
    )),
  ]
  responses = iter([
    SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
      content="", reasoning_content=None, tool_calls=[tool_calls[0]]
    ))]),
    SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
      content="", reasoning_content=None, tool_calls=[tool_calls[1]]
    ))]),
    SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
      content=final_answer, reasoning_content=None, tool_calls=None
    ))]),
  ])
  client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
    create=lambda **kwargs: next(responses)
  )))
  service_results = iter([
    _knowledge_payload("C1", "C2", sufficient=first_sufficient),
    second_payload,
  ])
  messages = [{"role": "user", "content": "退货与退款"}]
  result = run_one_turn(
    messages=messages,
    client=client,
    tool_definitions=[],
    tool_functions={"search_knowledge": lambda **kwargs: next(service_results)},
  )
  return result, messages


def test_two_knowledge_searches_continue_citation_numbering():
  # 保护行为：一轮内两次检索都成功时，第二次检索的编号必须续编（C3 而不是又一组 C1），
  # 且**事件与写回模型的 tool 消息**都必须是续编后的编号——
  # 否则模型看到的编号与最终响应不一致，引用会指向错误的文档。
  result, messages = _run_two_knowledge_searches(
    "退货看 [C1]，退款看 [C3]。", _second_knowledge_payload("C1")
  )

  completed = [
    event for event in result.events if event.type == "tool_call.completed"
  ]
  assert [
    item["citation_id"] for item in completed[1].result["data"]["citations"]
  ] == ["C3"]

  tool_messages = [m for m in messages if m["role"] == "tool"]
  assert len(tool_messages) == 2
  assert '"citation_id": "C1"' in tool_messages[0]["content"]
  assert '"citation_id": "C3"' in tool_messages[1]["content"]
  # 第二次检索的原始编号 C1 不得再出现（它已被续编为 C3）
  assert '"citation_id": "C1"' not in tool_messages[1]["content"]


def test_two_knowledge_searches_attribute_each_citation_to_its_own_evidence():
  # 保护行为：回答引用第二次检索的编号时，必须取到第二次那条证据
  # （document_id / version_id / chunk_id / 偏移都对），而不是第一次检索的同名编号。
  result, _ = _run_two_knowledge_searches(
    "退款到账时间见 [C3]。", _second_knowledge_payload("C1")
  )

  assert [item.citation_id for item in result.citations] == ["C3"]
  citation = result.citations[0]
  assert citation.document_id == "doc-2"
  assert citation.version_id == "version-2"
  assert citation.chunk_id == "chunk-2"
  assert citation.start_offset == 200
  assert citation.end_offset == 212
  assert result.answer_incomplete is False


def test_two_knowledge_searches_keep_answer_order_across_rounds():
  # 保护行为：引用两次检索的编号时两条都在，顺序与回答正文里首次出现的顺序一致
  # （回答先写 [C3] 再写 [C1]，结果顺序也必须是 C3、C1）。
  result, _ = _run_two_knowledge_searches(
    "先看退款 [C3]，再看退货 [C1]。", _second_knowledge_payload("C1")
  )

  assert [item.citation_id for item in result.citations] == ["C3", "C1"]
  assert result.citations[0].document_id == "doc-2"
  assert result.citations[1].document_id == "doc-1"
  assert result.answer_incomplete is False


def test_same_chunk_hit_twice_gets_two_distinct_citation_ids():
  # 边界情况：同一条证据被两次检索命中时得到两个编号（C1 与 C3），不做去重——
  # 去重会强迫模型改编号，且与主设计稿「块重叠不去重」的口径冲突。
  result, _ = _run_two_knowledge_searches(
    "两次都引到同一条 [C1] [C3]。", _second_knowledge_payload("C1")
  )

  assert [item.citation_id for item in result.citations] == ["C1", "C3"]
  assert {item.chunk_id for item in result.citations} == {"chunk-C1", "chunk-2"}


def test_two_knowledge_searches_merge_retrieval_summary():
  # 保护行为：摘要必须合并成一条——round_count 是本轮实际检索次数、
  # latency_ms 是各轮之和、evidence_status 取「最有证据」的那一轮。
  result, _ = _run_two_knowledge_searches(
    "退货与退款政策见 [C1]。", _second_knowledge_payload("C1", latency_ms=11)
  )

  assert result.retrieval_summary.round_count == 2
  assert result.retrieval_summary.latency_ms == 3 + 11
  assert result.retrieval_summary.evidence_status == "sufficient"
  assert result.retrieval_summary.strategy == "multi"


def test_merged_summary_reports_missing_citation_when_later_round_found_evidence():
  # 保护行为（安全性）：第一轮证据不足、第二轮拿到证据，回答却一个引用都没带时，
  # 必须判为 answer_incomplete=True——只看第一轮会让这条漏引不告警。
  result, _ = _run_two_knowledge_searches(
    "退货与退款政策请咨询客服。",
    _second_knowledge_payload("C1", evidence_status="sufficient"),
    first_sufficient=False,
  )

  assert list(result.citations) == []
  assert result.retrieval_summary.evidence_status == "sufficient"
  assert result.retrieval_summary.round_count == 2
  assert result.answer_incomplete is True


def test_merged_summary_stays_insufficient_when_no_round_has_evidence():
  # 边界情况：两轮都没有证据时合并结果是 insufficient（而不是 failed），
  # 且不会因为「没有可用引用」就误判为漏引。
  result, _ = _run_two_knowledge_searches(
    "没有查到相关政策。",
    _second_knowledge_payload("C1", evidence_status="insufficient"),
    first_sufficient=False,
  )

  assert result.retrieval_summary.evidence_status == "insufficient"
  assert result.retrieval_summary.round_count == 2
  assert result.answer_incomplete is False


def test_second_failed_search_does_not_hide_first_round_summary():
  # 边界情况：第二次检索整体失败（没有 retrieval_summary）时，
  # 摘要仍由第一轮决定，且编号续编不会影响第一轮的引用归属。
  result, _ = _run_two_knowledge_searches(
    "退货政策见 [C1]。",
    {"ok": False, "error": {"code": "SEARCH_INTERNAL_ERROR", "message": "boom"}},
  )

  assert [item.citation_id for item in result.citations] == ["C1"]
  assert result.retrieval_summary.round_count == 1
  assert result.retrieval_summary.evidence_status == "sufficient"
  assert result.answer_incomplete is False


#测试工具------------------
def get_food()->Any:
  return "其实啥也没有-v-"

def send_popup_to_user(content:str):
  return f"成功向用户发一个弹窗，弹窗的内容是{content}"

def dict_result_test()->dict:
  return {
    "name":"小明",
    "content":"Hello"
  }

def raise_error_func():
  raise ValueError("执行时异常")


tool_functions = {
  "get_food":get_food,
  "send_popup_to_user":send_popup_to_user,
  "dict_result_test":dict_result_test,
  "raise_error_func":raise_error_func,
}
tool_definitions = [
  {
    "type":"function",
    "function":{
      "name":"get_food",
      "description":"饿了就可以调用喵",
      "parameters":{
        "type":"object",
        "properties":{}
      }
    },
  },
  {
    "type":"function",
    "function":{
      "name":"send_popup_to_user",
      "description":"当你想提醒用户某件事情的时候就可以调用，直接向用户发出弹窗提醒用户",
      "parameters":{
        "type":"object",
        "properties":{
          "content":{
            "type":"string",
            "description":"弹窗中的消息内容，.e.g 你在干什么呀为什么不理我"
          }
        },
        "required":["content"],
      }
    }
  },
  {
    "type":"function",
    "function":{
      "name":"dict_result_test",
      "description":"这是一个测试工具，用来测试返回字典的工具结果能否被合法的转成字符串",
      "parameters":{
        "type":"object",
        "properties":{}
      }
    }
  },
  {
    "type":"function",
    "function":{
      "name":"raise_error_func",
      "description":"这是一个测试工具，用来测试工具执行时发生异常能否正常处理",
      "parameters":{
        "type":"object",
        "properties":{}
      }
    }
  }
]

TOOL_DEFINITIONS = tool_definitions
TOOL_FUNCTIONS = tool_functions

#模型直接回答
def test_1(monkeypatch):
  # 1. 构造假的模型消息
  fake_message = SimpleNamespace(
    content="你好，我是假模型的回答",
    reasoning_content="这是假的推理内容",
    tool_calls=None,
  )

  # 2. 构造假的模型响应
  fake_response = SimpleNamespace(
    choices=[
        SimpleNamespace(message=fake_message)
    ]
  )

  # 3. 构造假的客户端
  fake_client = SimpleNamespace(
    chat=SimpleNamespace(
      completions=SimpleNamespace(
          create=lambda **kwargs: fake_response
      )
    )
  )

  messages = [
    {
      "role": "user",
      "content": "Hello",
    }
  ]

  # 这里执行的是真实 run_one_turn，但调用的是假客户端
  result = run_one_turn(
    messages=messages,
    client=fake_client,
    tool_definitions=TOOL_DEFINITIONS,
    tool_functions=TOOL_FUNCTIONS,
    on_event=None,
  )

  # 5. 验证 Agent 的真实行为
  assert result.llm_answer == "你好，我是假模型的回答"
  assert result.llm_reasoning_content == "这是假的推理内容"
  assert result.events == []

  # run_one_turn 应当把最终回答加入消息历史
  assert messages == [
    {
      "role": "user",
      "content": "Hello",
    },
    {
      "role": "assistant",
      "content": "你好，我是假模型的回答",
      "reasoning_content": "这是假的推理内容",
    },
  ]



#调用单次工具
def test_2(monkeypatch):
  #构造假的客户端
  # 1. 构造假的模型消息
  fake_tool_calls = SimpleNamespace(
    id = "tool_call_id_1",
    type="function",
    function=SimpleNamespace(
      name="get_food",
      arguments="{}"
    ),
  )
  fake_tool_message = SimpleNamespace(
    content="",
    reasoning_content="我需要调用get_food工具",
    tool_calls=[fake_tool_calls]
  )
  fake_final_message = SimpleNamespace(
    content="调用工具完成",
    reasoning_content="调用了get_food",
    tool_calls=None,
  )

  
  # 2. 构造假的模型响应
  fake_tool_response = SimpleNamespace(
    choices=[
      SimpleNamespace(message=fake_tool_message)
    ]
  )
  fake_final_response = SimpleNamespace(
    choices=[
      SimpleNamespace(message=fake_final_message)
    ]
  )

  fake_response=iter([fake_tool_response,fake_final_response])
  def creat_next_fake_response(**kwagrs):
    return next(fake_response)

  # 3. 构造假的客户端 response = client.chat.complietions.creat(),response.choices[0].messages.content/.reasoning_content
  fake_client = SimpleNamespace(
    chat=SimpleNamespace(
      completions=SimpleNamespace(
          create=creat_next_fake_response
      )
    )
  )

  # 4. 临时替换 main.py 中的真实客户端
  #monkeypatch.setattr(main, "client", fake_client)

  messages = [
    {
      "role":"user",
      "content":"请你调用get_food工具"
    },
  ]

  #执行run_one_turn
  result = run_one_turn(
    messages=messages,
    client=fake_client,
    tool_definitions=TOOL_DEFINITIONS,
    tool_functions=TOOL_FUNCTIONS,
    on_event=None,
  )

  #测试AgentEvent是否正确
  events = result.events
  assert len(events)==3
  assert events[0].type=="tool_call.requested" and events[1].type=="tool_call.started" and events[2].type=="tool_call.completed"
  assert events[0].tool_call_id=="tool_call_id_1" and events[1].tool_call_id=="tool_call_id_1" and events[2].tool_call_id=="tool_call_id_1"
  assert events[0].tool_call_name=="get_food" and events[1].tool_call_name=="get_food" and events[2].tool_call_name=="get_food"

  assert messages==[
    {
      "role":"user",
      "content":"请你调用get_food工具"
    },
    {
      "role":"assistant",
      "content":"",
      "reasoning_content":"我需要调用get_food工具",
      "tool_calls":[
        {
          "id":"tool_call_id_1",
          "type":"function",
          "function":{
            "name":"get_food",
            "arguments":"{}"
          },
        }
      ]
    },
    {
      "role":"tool",
      "tool_call_id":"tool_call_id_1",
      "content":"其实啥也没有-v-"
    },
    {
      "role":"assistant",
      "content":"调用工具完成",
      "reasoning_content":"调用了get_food",
    }
  ]
  


def test_3(monkeypatch):
  # 1. 构造假的模型消息
  fake_tool_calls = SimpleNamespace(
    id = "tool_call_id_1",
    type="function",
    function=SimpleNamespace(
      name="dict_result_test",
      arguments="{}"
    ),
  )
  fake_tool_message = SimpleNamespace(
    content="",
    reasoning_content="我需要调用dict_result_test工具",
    tool_calls=[fake_tool_calls]
  )
  fake_final_message = SimpleNamespace(
    content="调用工具完成",
    reasoning_content="调用了dict_result_test",
    tool_calls=None,
  )

  
  # 2. 构造假的模型响应
  fake_tool_response = SimpleNamespace(
    choices=[
      SimpleNamespace(message=fake_tool_message)
    ]
  )
  fake_final_response = SimpleNamespace(
    choices=[
      SimpleNamespace(message=fake_final_message)
    ]
  )

  fake_response=iter([fake_tool_response,fake_final_response])
  def creat_next_fake_response(**kwagrs):
    return next(fake_response)

  # 3. 构造假的客户端 response = client.chat.complietions.creat(),response.choices[0].messages.content/.reasoning_content
  fake_client = SimpleNamespace(
    chat=SimpleNamespace(
      completions=SimpleNamespace(
          create=creat_next_fake_response
      )
    )
  )

  # 4. 临时替换 main.py 中的真实客户端
  #monkeypatch.setattr(main, "client", fake_client)

  messages = [
    {
      "role":"user",
      "content":"请你调用dict_result_test工具"
    },
  ]

  #执行run_one_turn
  result = run_one_turn(
    messages=messages,
    client=fake_client,
    tool_definitions=TOOL_DEFINITIONS,
    tool_functions=TOOL_FUNCTIONS,
    on_event=None,
  )

  assert messages==[
    {
      "role":"user",
      "content":"请你调用dict_result_test工具"
    },
    {
      "role":"assistant",
      "content":"",
      "reasoning_content":"我需要调用dict_result_test工具",
      "tool_calls":[
        {
          "id":"tool_call_id_1",
          "type":"function",
          "function":{
            "name":"dict_result_test",
            "arguments":"{}"
          }
        }
      ]
    },
    {
      "role":"tool",
      "tool_call_id":"tool_call_id_1",
      "content":'{"name": "小明", "content": "Hello"}'
    },
    {
      "role":"assistant",
      "content":"调用工具完成",
      "reasoning_content":"调用了dict_result_test"
    }
  ]


#参数 JSON 损坏，只产生 failed，不产生 completed。
def test_4(monkeypatch):
  #构造假的客户端
  # 1. 构造假的模型消息
  fake_tool_calls = SimpleNamespace(
    id = "tool_call_id_1",
    type="function",
    function=SimpleNamespace(
      name="get_food",
      arguments="{"
    ),
  )
  fake_tool_calls_turn_1 = SimpleNamespace(
    id="tool_call_id_1",
    type="function",
    function=SimpleNamespace(
      name="get_food",
      arguments="{}"
    )
  )
  fake_tool_message = SimpleNamespace(
    content="",
    reasoning_content="我需要调用get_food工具",
    tool_calls=[fake_tool_calls]
  )
  fake_tool_message_turn_1 = SimpleNamespace(
    content="",
    reasoning_content="工具参数错误，让我传入正确的工具参数",
    tool_calls=[fake_tool_calls_turn_1]
  )
  fake_final_message = SimpleNamespace(
    content="调用工具完成",
    reasoning_content="调用了get_food",
    tool_calls=None,
  )

  
  # 2. 构造假的模型响应
  fake_tool_response = SimpleNamespace(
    choices=[
      SimpleNamespace(message=fake_tool_message)
    ]
  )
  fake_tool_message_turn_1_response = SimpleNamespace(
    choices=[
      SimpleNamespace(message=fake_tool_message_turn_1)
    ]
  )
  fake_final_response = SimpleNamespace(
    choices=[
      SimpleNamespace(message=fake_final_message)
    ]
  )

  fake_response=iter([fake_tool_response,fake_tool_message_turn_1_response,fake_final_response])
  def creat_next_fake_response(**kwagrs):
    return next(fake_response)

  # 3. 构造假的客户端 response = client.chat.complietions.creat(),response.choices[0].messages.content/.reasoning_content
  fake_client = SimpleNamespace(
    chat=SimpleNamespace(
      completions=SimpleNamespace(
          create=creat_next_fake_response
      )
    )
  )

  # 4. 临时替换 main.py 中的真实客户端
  #monkeypatch.setattr(main, "client", fake_client)

  messages = [
    {
      "role":"user",
      "content":"请你调用get_food工具"
    },
  ]

  #执行run_one_turn
  result = run_one_turn(
    messages=messages,
    client=fake_client,
    tool_definitions=TOOL_DEFINITIONS,
    tool_functions=TOOL_FUNCTIONS,
    on_event=None,
  )

  #测试AgentEvent是否正确,尝试调用(错误参数)->收到错误消息->再次调用(正确参数)->进行回答
  #faild request start complete
  events = result.events
  assert len(events)==4
  assert events[0].type=="tool_call.failed" and events[1].type=="tool_call.requested" and events[2].type=="tool_call.started" and events[3].type=="tool_call.completed"
  
  print(messages)

  assert messages==[
    {
      "role":"user",
      "content":"请你调用get_food工具"
    },
    {
      "role":"assistant",
      "content":"",
      "reasoning_content":"我需要调用get_food工具",
      "tool_calls":[
        {
          "id":"tool_call_id_1",
          "type":"function",
          "function":{
            "name":"get_food",
            "arguments":"{"
          },
        }
      ]
    },
    {
      "role":"tool",
      "tool_call_id":"tool_call_id_1",
      "content":'{"OK": false, "error": "工具参数解析失败"}'
    },
    {
      "role":"assistant",
      "content":"",
      "reasoning_content":"工具参数错误，让我传入正确的工具参数",
      "tool_calls":[
        {
          "id":"tool_call_id_1",
          "type":"function",
          "function":{
            "name":"get_food",
            "arguments":"{}"
          }
        }
      ]
    },
    {
      "role":"tool",
      "tool_call_id":"tool_call_id_1",
      "content":"其实啥也没有-v-"
    },
    {
      "role":"assistant",
      "content":"调用工具完成",
      "reasoning_content":"调用了get_food",
    }
  ]

#5.调用了未注册工具。
def test_5(monkeypatch):
  #构造假的客户端
  # 1. 构造假的模型消息
  fake_tool_calls = SimpleNamespace(
    id = "tool_call_id_1",
    type="function",
    function=SimpleNamespace(
      name="unknow_tool",
      arguments="{}"
    ),
  )
  fake_tool_message = SimpleNamespace(
    content="",
    reasoning_content="我需要调用unknow_tool工具",
    tool_calls=[fake_tool_calls]
  )
  fake_final_message = SimpleNamespace(
    content="unknow_tool这个工具不存在",
    reasoning_content="没有unknow_tool这个工具",
    tool_calls=None,
  )

  
  # 2. 构造假的模型响应
  fake_tool_response = SimpleNamespace(
    choices=[
      SimpleNamespace(message=fake_tool_message)
    ]
  )
  fake_final_response = SimpleNamespace(
    choices=[
      SimpleNamespace(message=fake_final_message)
    ]
  )

  fake_response=iter([fake_tool_response,fake_final_response])
  def creat_next_fake_response(**kwagrs):
    return next(fake_response)

  # 3. 构造假的客户端 response = client.chat.complietions.creat(),response.choices[0].messages.content/.reasoning_content
  fake_client = SimpleNamespace(
    chat=SimpleNamespace(
      completions=SimpleNamespace(
          create=creat_next_fake_response
      )
    )
  )

  # 4. 临时替换 main.py 中的真实客户端
  #monkeypatch.setattr(main, "client", fake_client)

  messages = [
    {
      "role":"user",
      "content":"请你调用unknow_tool工具"
    },
  ]

  #执行run_one_turn
  result = run_one_turn(
    messages=messages,
    client=fake_client,
    tool_definitions=TOOL_DEFINITIONS,
    tool_functions=TOOL_FUNCTIONS,
    on_event=None,
  )

  events = result.events
  assert events[0].type=="tool_call.requested" and events[1].type=="tool_call.failed"
  assert len(events)==2

  print(messages)

  assert messages==[
    {
      "role":"user",
      "content":"请你调用unknow_tool工具"
    },
    {
      "role":"assistant",
      "content":"",
      "reasoning_content":"我需要调用unknow_tool工具",
      "tool_calls":[
        {
          "id":"tool_call_id_1",
          "type":"function",
          "function":{
            "name":"unknow_tool",
            "arguments":"{}"
          }
        }
      ]
    },
    {
      "role":"tool",
      "tool_call_id":"tool_call_id_1",
      "content":'{"OK": false, "error": "未注册的工具"}'
    },
    {
      "role":"assistant",
      "content":"unknow_tool这个工具不存在",
      "reasoning_content":"没有unknow_tool这个工具"
    }
  ]

#6.工具函数抛异常，只产生 failed。
def test_6(monkeypatch):
  #构造假的客户端
  # 1. 构造假的模型消息
  fake_tool_calls = SimpleNamespace(
    id = "tool_call_id_1",
    type="function",
    function=SimpleNamespace(
      name="raise_error_func",
      arguments="{}"
    ),
  )
  fake_tool_message = SimpleNamespace(
    content="",
    reasoning_content="我需要调用raise_error_func工具",
    tool_calls=[fake_tool_calls]
  )
  fake_final_message = SimpleNamespace(
    content="raise_error_func这个工具抛出了异常",
    reasoning_content="让我告诉用户这个工具抛出了异常",
    tool_calls=None,
  )

  
  # 2. 构造假的模型响应
  fake_tool_response = SimpleNamespace(
    choices=[
      SimpleNamespace(message=fake_tool_message)
    ]
  )
  fake_final_response = SimpleNamespace(
    choices=[
      SimpleNamespace(message=fake_final_message)
    ]
  )

  fake_response=iter([fake_tool_response,fake_final_response])
  def creat_next_fake_response(**kwagrs):
    return next(fake_response)

  # 3. 构造假的客户端 response = client.chat.complietions.creat(),response.choices[0].messages.content/.reasoning_content
  fake_client = SimpleNamespace(
    chat=SimpleNamespace(
      completions=SimpleNamespace(
          create=creat_next_fake_response
      )
    )
  )

  # 4. 临时替换 main.py 中的真实客户端
  #monkeypatch.setattr(main, "client", fake_client)

  messages = [
    {
      "role":"user",
      "content":"请你调用raise_error_func工具"
    },
  ]

  #执行run_one_turn
  result = run_one_turn(
    messages=messages,
    client=fake_client,
    tool_definitions=TOOL_DEFINITIONS,
    tool_functions=TOOL_FUNCTIONS,
    on_event=None,
  )

  events = result.events
  # 保护行为：事件顺序必须是请求、开始、失败。
  assert events[0].type=="tool_call.requested" and events[1].type=="tool_call.started" and events[2].type=="tool_call.failed"
  assert messages==[
    {
      "role":"user",
      "content":"请你调用raise_error_func工具"
    },
    {
      "role":"assistant",
      "content":"",
      "reasoning_content":"我需要调用raise_error_func工具",
      "tool_calls":[
        {
          "id":"tool_call_id_1",
          "type":"function",
          "function":{
            "name":"raise_error_func",
            "arguments":"{}"
          }
        }
      ]
    },
    {
      "role":"tool",
      "tool_call_id":"tool_call_id_1",
      "content":'{"OK": false, "error": "TOOL_EXECUTION_FAILED"}'
    },
    {
      "role":"assistant",
      "content":"raise_error_func这个工具抛出了异常",
      "reasoning_content":"让我告诉用户这个工具抛出了异常"
    }
  ]

#7.一次调用多个工具，其中一个成功、一个失败。
def test_7(monkeypatch):
  #构造假的客户端
  # 1. 构造假的模型消息
  fake_tool_calls = SimpleNamespace(
    id = "tool_call_id_1",
    type="function",
    function=SimpleNamespace(
      name="raise_error_func",
      arguments="{}"
    ),
  )
  fake_tool_calls_1 = SimpleNamespace(
    id = "tool_call_id_2",
    type="function",
    function=SimpleNamespace(
      name="get_food",
      arguments="{}"
    )
  )
  fake_tool_message = SimpleNamespace(
    content="同时调用两个工具",
    reasoning_content="我需要调用raise_error_func和get_food工具",
    tool_calls=[fake_tool_calls,fake_tool_calls_1]
  )
  fake_final_message = SimpleNamespace(
    content="raise_error_func这个工具抛出了异常,get_food工具执行成功",
    reasoning_content="我得到了两个工具的结果",
    tool_calls=None,
  )

  
  # 2. 构造假的模型响应
  fake_tool_response = SimpleNamespace(
    choices=[
      SimpleNamespace(message=fake_tool_message)
    ]
  )
  fake_final_response = SimpleNamespace(
    choices=[
      SimpleNamespace(message=fake_final_message)
    ]
  )

  fake_response=iter([fake_tool_response,fake_final_response])
  def creat_next_fake_response(**kwagrs):
    return next(fake_response)

  # 3. 构造假的客户端 response = client.chat.complietions.creat(),response.choices[0].messages.content/.reasoning_content
  fake_client = SimpleNamespace(
    chat=SimpleNamespace(
      completions=SimpleNamespace(
          create=creat_next_fake_response
      )
    )
  )

  # 4. 临时替换 main.py 中的真实客户端
  #monkeypatch.setattr(main, "client", fake_client)

  messages = [
    {
      "role":"user",
      "content":"请你同时调用raise_error_func和get_food工具"
    },
  ]

  #执行run_one_turn
  result = run_one_turn(
    messages=messages,
    client=fake_client,
    tool_definitions=TOOL_DEFINITIONS,
    tool_functions=TOOL_FUNCTIONS,
    on_event=None,
  )

  events = result.events
  for event in events:
    print(f"tool_call_id:{event.tool_call_id} type:{event.type}")
  assert events[0].tool_call_id=="tool_call_id_1" and events[0].type=="tool_call.requested"
  assert events[1].tool_call_id=="tool_call_id_1" and events[1].type=="tool_call.started"
  assert events[2].tool_call_id=="tool_call_id_1" and events[2].type=="tool_call.failed"
  assert events[3].tool_call_id=="tool_call_id_2" and events[3].type=="tool_call.requested"
  assert events[4].tool_call_id=="tool_call_id_2" and events[4].type=="tool_call.started"
  assert events[5].tool_call_id=="tool_call_id_2" and events[5].type=="tool_call.completed"
  assert messages==[
    {
      "role":"user",
      "content":"请你同时调用raise_error_func和get_food工具"
    },
    {
      "role":"assistant",
      "content":"同时调用两个工具",
      "reasoning_content":"我需要调用raise_error_func和get_food工具",
      "tool_calls":[
        {
          "id":"tool_call_id_1",
          "type":"function",
          "function":{
            "name":"raise_error_func",
            "arguments":"{}"
          }
        },
        {
          "id":"tool_call_id_2",
          "type":"function",
          "function":{
            "name":"get_food",
            "arguments":"{}"
          }
        }
      ]
    },
    {
      "role":"tool",
      "tool_call_id":"tool_call_id_1",
      "content":'{"OK": false, "error": "TOOL_EXECUTION_FAILED"}'
    },
    {
      "role":"tool",
      "tool_call_id":"tool_call_id_2",
      "content":"其实啥也没有-v-"
    },
    {
      "role":"assistant",
      "content":"raise_error_func这个工具抛出了异常,get_food工具执行成功",
      "reasoning_content":"我得到了两个工具的结果"
    }
  ]


#8.工具执行后，模型继续调用第二轮工具。
def test_8(monkeypatch):
  #构造假的客户端
  # 1. 构造假的模型消息
  fake_tool_calls = SimpleNamespace(
    id = "tool_call_id_1",
    type="function",
    function=SimpleNamespace(
      name="get_food",
      arguments="{}"
    ),
  )
  fake_tool_calls_1 = SimpleNamespace(
    id = "tool_call_id_2",
    type="function",
    function=SimpleNamespace(
      name="send_popup_to_user",
      arguments='{"content":"Hello,hi"}'
    )
  )
  fake_tool_message = SimpleNamespace(
    content="让我先调用get_food，得到结果后继续调用send_popup_to_user",
    reasoning_content="我需要先调用get_food工具",
    tool_calls=[fake_tool_calls]
  )
  fake_tool_message_1 = SimpleNamespace(
    content="我已经获取到了get_food的结果，现在让我调用send_popup_to_user",
    reasoning_content="假的思考过程",
    tool_calls=[fake_tool_calls_1]
  )
  fake_final_message = SimpleNamespace(
    content="先调用了get_food，然后调用了send_popup_to_user",
    reasoning_content="调用结束",
    tool_calls=None,
  )

  
  # 2. 构造假的模型响应
  fake_tool_response = SimpleNamespace(
    choices=[
      SimpleNamespace(message=fake_tool_message)
    ]
  )
  fake_tool_response_1 = SimpleNamespace(
    choices=[
      SimpleNamespace(message=fake_tool_message_1)
    ]
  )
  fake_final_response = SimpleNamespace(
    choices=[
      SimpleNamespace(message=fake_final_message)
    ]
  )

  fake_response=iter([fake_tool_response,fake_tool_response_1,fake_final_response])
  def creat_next_fake_response(**kwagrs):
    return next(fake_response)

  # 3. 构造假的客户端 response = client.chat.complietions.creat(),response.choices[0].messages.content/.reasoning_content
  fake_client = SimpleNamespace(
    chat=SimpleNamespace(
      completions=SimpleNamespace(
          create=creat_next_fake_response
      )
    )
  )

  # 4. 临时替换 main.py 中的真实客户端
  #monkeypatch.setattr(main, "client", fake_client)

  messages = [
    {
      "role":"user",
      "content":"请你连续调用工具"
    },
  ]

  #执行run_one_turn
  result = run_one_turn(
    messages=messages,
    client=fake_client,
    tool_definitions=TOOL_DEFINITIONS,
    tool_functions=TOOL_FUNCTIONS,
    on_event=None,
  )

  events = result.events
  for eve in events:
    print(eve.type)
  #tool_call_id_1:request->start->complete
  #tool_call_id_2:request->start->complete
  assert (
    events[0].type=="tool_call.requested" and 
    events[1].type=="tool_call.started" and 
    events[2].type=="tool_call.completed" and
    events[0].tool_call_id=="tool_call_id_1" and
    events[1].tool_call_id=="tool_call_id_1" and
    events[2].tool_call_id=="tool_call_id_1" 
  )
  assert (
    events[3].type=="tool_call.requested" and 
    events[4].type=="tool_call.started" and 
    events[5].type=="tool_call.completed" and
    events[3].tool_call_id=="tool_call_id_2" and
    events[4].tool_call_id=="tool_call_id_2" and
    events[5].tool_call_id=="tool_call_id_2" 
  )
  assert messages==[
    {
      "role":"user",
      "content":"请你连续调用工具"
    },
    {
      "role":"assistant",
      "content":"让我先调用get_food，得到结果后继续调用send_popup_to_user",
      "reasoning_content":"我需要先调用get_food工具",
      "tool_calls":[
        {
          "id":"tool_call_id_1",
          "type":"function",
          "function":{
            "name":"get_food",
            "arguments":"{}"
          }
        }
      ]
    },
    {
      "role":"tool",
      "tool_call_id":"tool_call_id_1",
      "content":"其实啥也没有-v-"
    },
    {
      "role":"assistant",
      "content":"我已经获取到了get_food的结果，现在让我调用send_popup_to_user",
      "reasoning_content":"假的思考过程",
      "tool_calls":[
        {
          "id":"tool_call_id_2",
          "type":"function",
          "function":{
            "name":"send_popup_to_user",
            "arguments":'{"content":"Hello,hi"}'
          }
        }
      ]
    },
    {
      "role":"tool",
      "tool_call_id":"tool_call_id_2",
      "content":"成功向用户发一个弹窗，弹窗的内容是Hello,hi"
    },
    {
      "role":"assistant",
      "content":"先调用了get_food，然后调用了send_popup_to_user",
      "reasoning_content":"调用结束"
    }
  ]
