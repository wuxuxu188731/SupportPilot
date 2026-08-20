# Customer Support Agent Golden Path Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将受控客服 Tool Gateway 按每次聊天请求的可信 `TenantContext` 绑定给 Agent，使模型能够完成“查询订单 → 查询物流 → 创建工单 → 根据工具事实生成回复”的第一条端到端黄金路径。

**Architecture:** 保留现有 `run_one_turn()` 作为通用工具循环，在其外新增 `CustomerSupportAgentRunner` 适配器；适配器每次运行时调用 `gateway.bind(context)`，把本次请求专属的工具函数和固定工具定义交给通用 runner。`ChatService` 负责组装不可缺失的客服基础提示词、会话自定义提示词和历史消息，并把当前 `TenantContext` 传给适配器；`main.py` 只负责装配真实客户端、Gateway、Agent runner 和 ChatService。所有黄金路径使用假模型响应和真实 SQLite 数据测试，不访问外部模型 API。

**Tech Stack:** Python 3.10+、现有 OpenAI-compatible client、FastAPI、Pydantic、SQLite、pytest、标准库 `typing` / `dataclasses` / `json` / `types`。

## Global Constraints

- 当前 Alembic head 为 `0008_ticket_comments`，计划编写前全量测试为 `194 passed`。
- 第四步已经交付 `CustomerSupportToolGateway.bind(context)`、四个工具函数、四个工具定义和 Gateway 工厂。
- 本计划不修改数据库表，不新增 migration。
- 本计划不实现 RAG、退款、补偿、审批、LangGraph、Agent 长期记忆、流式输出、人工接管或真实第三方系统。
- 所有测试必须使用假模型客户端；测试命令不得访问 DeepSeek、OpenAI 或其他外部 API。
- `TenantContext` 必须来自现有认证和 membership 校验流程，并由 `ChatService.chat()` 逐请求传递。
- 禁止在应用启动时调用 `gateway.bind()` 生成全局客服工具函数；绑定必须发生在每次 Agent 运行中。
- 同一个 `CustomerSupportAgentRunner` 可以被不同企业复用，但每次运行必须重新绑定对应上下文。
- 模型永远不能提交可信 `organization_id`、`user_id`、`role` 或 `context`；继续由 Tool Gateway 执行这一安全边界。
- `run_one_turn()` 继续保持与具体客服领域无关，不能导入 `CustomerSupportToolGateway` 或业务 Store。
- Agent 工具循环必须设置最大工具轮数，防止模型无限调用工具。
- 客服基础提示词必须始终存在，不能因为会话没有自定义 `system_prompt` 而消失。
- 会话自定义提示词只能作为附加指令，不能替代服务器提供的客服基础提示词。
- 客服回答中的订单状态、物流状态、工单号必须来自成功工具结果，不能由提示词或测试代码直接注入业务事实。
- 只有用户明确要求创建工单，或者当前黄金路径测试明确表达“请创建工单”时，提示词才允许模型调用 `create_ticket`。
- 工具返回 `ok: false` 时，模型不得声称业务操作成功；可以修正参数重试或解释失败。
- Gateway 返回的业务失败信封属于一次成功完成的函数调用，因此 runner 记录 `tool_call.completed`；业务是否成功由结果中的 `ok` 判断。
- 现有会话隔离和锁语义必须保持：同一会话串行，不同会话可并行，失败的 Agent 回合不持久化部分消息。
- 第五步完成后，`main.py` 不再把 `get_food` 和 `send_popup_to_user` 作为生产聊天工具；演示文件可以保留供旧单元测试使用。
- 每个 Task 采用 TDD：失败测试 → 确认失败 → 最小实现 → 局部测试 → 全量回归 → 独立提交。

---

## 1. 第五步完成标准

HTTP 请求经过现有认证和租户依赖后，调用链应为：

```text
POST /conversations/{id}/chat
        ↓
get_current_tenant
        ↓
TenantContext(user_id, organization_id, role)
        ↓
ChatService.chat(context, conversation_id, question)
        ↓
CustomerSupportAgentRunner(messages, context)
        ↓
gateway.bind(context)              每次请求重新绑定
        ↓
run_one_turn(definitions, bound_functions)
        ↓
模型选择 get_order
        ↓
模型选择 get_logistics
        ↓
用户明确要求时选择 create_ticket
        ↓
模型根据工具结果生成最终回复
```

黄金路径用户问题固定为：

```text
订单 ORD-GOLDEN-001 已超过承诺发货时间，请查询订单和物流；
如果确实还没有发货，请创建一个高优先级物流工单并告诉我工单号。
```

预期工具顺序：

```text
get_order
→ get_logistics
→ create_ticket
→ final answer
```

## 2. 最终文件结构

```text
app/agent/
  prompts.py
    不可缺失的客服基础提示词

  support_runner.py
    每次请求绑定 Gateway 的 Agent 适配器

  runner.py
    通用工具循环；增加模型参数和最大工具轮数

app/application/
  chat_service.py
    组装基础提示词并把 TenantContext 传给 Agent

main.py
  装配 Gateway、CustomerSupportAgentRunner 和 ChatService

tests/agent/
  test_runner.py
  test_support_runner.py
  test_customer_support_golden_path.py

tests/application/
  test_chat_service.py

tests/
  test_main.py

docs/
  customer-support-agent-golden-path.md

README.md
```

---

### Task 1: 给通用 Agent 工具循环增加模型配置和轮次上限

**独立验收产物：** `run_one_turn()` 使用调用方提供的模型名；模型连续调用工具超过上限时抛出稳定异常，不会无限循环；现有八类 runner 行为保持通过。

**Files:**
- Modify: `app/agent/runner.py`
- Modify: `tests/agent/test_runner.py`

**Interfaces:**
- Produces: `AgentToolRoundLimitError`
- Produces: `DEFAULT_MAX_TOOL_ROUNDS = 8`
- Extends: `run_one_turn(model_name, max_tool_rounds)`

- [ ] **Step 1: 写模型名可配置失败测试**

在 `tests/agent/test_runner.py` 追加：

```python
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
```

- [ ] **Step 2: 写无限工具调用保护失败测试**

把 `AgentToolRoundLimitError` 加入测试 imports，并追加：

```python
def test_run_one_turn_stops_after_max_tool_rounds():
    call_count = 0

    def create(**kwargs):
        nonlocal call_count
        call_count += 1
        tool_call = SimpleNamespace(
            id=f"call-{call_count}",
            type="function",
            function=SimpleNamespace(
                name="get_food",
                arguments="{}",
            ),
        )
        message = SimpleNamespace(
            content="",
            reasoning_content="继续调用工具",
            tool_calls=[tool_call],
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message)]
        )

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
```

这里允许模型产生两轮工具调用；第三次模型仍返回工具调用时立即抛错，第三轮工具函数不得执行。

- [ ] **Step 3: 写非法轮次配置测试**

追加：

```python
@pytest.mark.parametrize("max_tool_rounds", [0, -1])
def test_run_one_turn_requires_positive_tool_round_limit(
    max_tool_rounds,
):
    with pytest.raises(
        ValueError,
        match="max_tool_rounds must be positive",
    ):
        run_one_turn(
            messages=[],
            client=SimpleNamespace(),
            tool_definitions=[],
            tool_functions={},
            max_tool_rounds=max_tool_rounds,
        )
```

- [ ] **Step 4: 运行新测试并确认失败**

Run:

```powershell
python -m pytest tests/agent/test_runner.py -q
```

Expected: FAIL，因为新参数和异常尚未实现。

- [ ] **Step 5: 增加配置和异常**

在 `app/agent/runner.py` imports 中加入：

```python
from app.core.config import MODEL_NAME
from typing import Protocol
```

在模块顶部增加：

```python
DEFAULT_MAX_TOOL_ROUNDS = 20


class AgentToolRoundLimitError(RuntimeError):
    pass


class AgentToolFunction(Protocol):
    def __call__(self, **arguments: Any) -> Any:
        raise NotImplementedError
```

- [ ] **Step 6: 扩展 `run_one_turn()` 签名**

把函数签名整理为：

```python
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
```

- [ ] **Step 7: 实现参数校验和轮次计数**

在函数开头增加：

```python
    if max_tool_rounds <= 0:
        raise ValueError(
            "max_tool_rounds must be positive"
        )
    tool_rounds = 0
```

把模型调用中的硬编码模型名：

```python
model="deepseek-v4-flash"
```

替换为：

```python
model=model_name
```

在确认 `message.tool_calls` 非空之后、把 assistant 工具调用加入 `messages` 之前增加：

```python
    if tool_rounds >= max_tool_rounds:
        raise AgentToolRoundLimitError(
            "maximum tool rounds exceeded"
        )
    tool_rounds += 1
```

- [ ] **Step 8: 运行 runner 专项和全量回归**

Run:

```powershell
python -m pytest tests/agent/test_runner.py -q
python -m pytest -q
```

Expected: 两条命令全部 PASS。

- [ ] **Step 9: 提交 Task 1**

```powershell
git add app/agent/runner.py tests/agent/test_runner.py
git commit -m "feat: bound agent tool rounds"
```

---

### Task 2: 新增按请求绑定 Tool Gateway 的客服 Agent 适配器

**独立验收产物：** 同一个 Agent 适配器每次调用都使用本次传入的 `TenantContext` 重新绑定工具；通用 runner 收到 Gateway 的四个定义和本次请求专属函数。

**Files:**
- Create: `app/agent/support_runner.py`
- Create: `tests/agent/test_support_runner.py`

**Interfaces:**
- Consumes: `CustomerSupportToolGateway`
- Consumes: `run_one_turn`
- Produces: `CustomerSupportAgentRunner`
- Produces: `CustomerSupportAgentRunner.__call__(messages, context)`

- [ ] **Step 1: 写 Fake Gateway 和 Fake 通用 runner**

创建 `tests/agent/test_support_runner.py`：

```python
from dataclasses import dataclass, field

from app.agent.support_runner import (
    CustomerSupportAgentRunner,
)
from app.application.organization_service import TenantContext
from app.organizations.base import MembershipRole
from app.schemas.chat import LLMResponse


ORG_A = TenantContext(
    user_id="user-a",
    organization_id="org-a",
    role=MembershipRole.AGENT,
)
ORG_B = TenantContext(
    user_id="user-b",
    organization_id="org-b",
    role=MembershipRole.ADMIN,
)


@dataclass
class FakeGateway:
    bound_contexts: list[TenantContext] = field(
        default_factory=list
    )

    @property
    def definitions(self):
        return [
            {
                "type": "function",
                "function": {"name": "get_order"},
            }
        ]

    def bind(self, *, context):
        self.bound_contexts.append(context)

        def get_order(**arguments):
            return {
                "ok": True,
                "data": {
                    "organization": context.organization_id,
                    "arguments": arguments,
                },
            }

        return {"get_order": get_order}


@dataclass
class RecordingRunTurn:
    calls: list[dict] = field(default_factory=list)

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return LLMResponse(llm_answer="answer")
```

- [ ] **Step 2: 写逐请求重新绑定上下文的失败测试**

追加：

```python
def test_support_runner_binds_gateway_for_each_context():
    gateway = FakeGateway()
    run_turn = RecordingRunTurn()
    runner = CustomerSupportAgentRunner(
        client=object(),
        gateway=gateway,
        model_name="test-model",
        max_tool_rounds=5,
        run_turn=run_turn,
    )

    runner(
        messages=[{"role": "user", "content": "A"}],
        context=ORG_A,
    )
    runner(
        messages=[{"role": "user", "content": "B"}],
        context=ORG_B,
    )

    assert gateway.bound_contexts == [ORG_A, ORG_B]
    first_function = run_turn.calls[0][
        "tool_functions"
    ]["get_order"]
    second_function = run_turn.calls[1][
        "tool_functions"
    ]["get_order"]
    assert first_function(order_no="ORD-001")["data"][
        "organization"
    ] == "org-a"
    assert second_function(order_no="ORD-001")["data"][
        "organization"
    ] == "org-b"
```

- [ ] **Step 3: 写通用 runner 参数转发测试**

追加：

```python
def test_support_runner_forwards_gateway_and_limits():
    gateway = FakeGateway()
    run_turn = RecordingRunTurn()
    client = object()
    runner = CustomerSupportAgentRunner(
        client=client,
        gateway=gateway,
        model_name="support-model",
        max_tool_rounds=6,
        run_turn=run_turn,
    )
    messages = [{"role": "user", "content": "hello"}]

    result = runner(messages=messages, context=ORG_A)

    call = run_turn.calls[0]
    assert result.llm_answer == "answer"
    assert call["messages"] is messages
    assert call["client"] is client
    assert call["tool_definitions"] == gateway.definitions
    assert set(call["tool_functions"]) == {"get_order"}
    assert call["model_name"] == "support-model"
    assert call["max_tool_rounds"] == 6
    assert call["on_event"] is None
```

- [ ] **Step 4: 运行测试并确认失败**

Run:

```powershell
python -m pytest tests/agent/test_support_runner.py -q
```

Expected: collection FAIL，因为 `support_runner.py` 尚不存在。

- [ ] **Step 5: 定义通用 runner 协议和适配器**

创建 `app/agent/support_runner.py`：

```python
from typing import Any, Protocol

from app.agent.runner import (
    AgentToolFunction,
    DEFAULT_MAX_TOOL_ROUNDS,
    run_one_turn,
)
from app.application.organization_service import TenantContext
from app.core.config import MODEL_NAME
from app.schemas.chat import LLMResponse
from app.tools.support_gateway import (
    CustomerSupportToolGateway,
)


class RunTurn(Protocol):
    def __call__(
        self,
        *,
        messages: list[dict],
        client: Any,
        tool_definitions: list[dict],
        tool_functions: dict[str, AgentToolFunction],
        on_event: Any,
        model_name: str,
        max_tool_rounds: int,
    ) -> LLMResponse:
        raise NotImplementedError


class CustomerSupportAgentRunner:
    def __init__(
        self,
        *,
        client: Any,
        gateway: CustomerSupportToolGateway,
        model_name: str = MODEL_NAME,
        max_tool_rounds: int = DEFAULT_MAX_TOOL_ROUNDS,
        on_event: Any = None,
        run_turn: RunTurn = run_one_turn,
    ):
        self._client = client
        self._gateway = gateway
        self._model_name = model_name
        self._max_tool_rounds = max_tool_rounds
        self._on_event = on_event
        self._run_turn = run_turn

    def __call__(
        self,
        *,
        messages: list[dict],
        context: TenantContext,
    ) -> LLMResponse:
        return self._run_turn(
            messages=messages,
            client=self._client,
            tool_definitions=self._gateway.definitions,
            tool_functions=self._gateway.bind(
                context=context
            ),
            on_event=self._on_event,
            model_name=self._model_name,
            max_tool_rounds=self._max_tool_rounds,
        )
```

`on_event` 在本阶段保持 `Any`，因为现有通用 runner 已负责其运行时调用约定。

- [ ] **Step 6: 运行适配器测试和全量回归**

Run:

```powershell
python -m pytest tests/agent/test_support_runner.py -q
python -m pytest -q
```

Expected: 两条命令全部 PASS。

- [ ] **Step 7: 提交 Task 2**

```powershell
git add app/agent/support_runner.py tests/agent/test_support_runner.py
git commit -m "feat: bind support tools per agent request"
```

---

### Task 3: 加入客服基础提示词并让 ChatService 传递 TenantContext

**独立验收产物：** 每次聊天的第一条消息都是服务器客服基础提示词；可选会话提示词只能追加；Agent runner 收到当前请求的原始 `TenantContext`。

**Files:**
- Create: `app/agent/prompts.py`
- Modify: `app/application/chat_service.py`
- Modify: `tests/application/test_chat_service.py`

**Interfaces:**
- Produces: `SUPPORT_SYSTEM_PROMPT`
- Extends: `ChatService.__init__(base_system_prompt)`
- Changes: agent call to `run_agent(messages=messages, context=context)`

- [ ] **Step 1: 创建客服基础提示词内容测试**

在 `tests/application/test_chat_service.py` imports 中加入：

```python
from app.agent.prompts import SUPPORT_SYSTEM_PROMPT
```

追加：

```python
def test_support_prompt_contains_fact_and_write_rules():
    assert "工具结果" in SUPPORT_SYSTEM_PROMPT
    assert "不得编造" in SUPPORT_SYSTEM_PROMPT
    assert "明确要求创建工单" in SUPPORT_SYSTEM_PROMPT
    assert "ok" in SUPPORT_SYSTEM_PROMPT
```

- [ ] **Step 2: 修改测试装配和 runner 签名**

把 `build_service()` 中的构造改为：

```python
  service = ChatService(
    store=store,
    run_agent=runner,
    locks=ConversationLockRegistry(),
    base_system_prompt=SUPPORT_SYSTEM_PROMPT,
  )
```

把测试文件中的 runner 定义分别改为：

```python
def direct_answer_runner(*, messages, context):
```

```python
def recording_runner(*, messages, context):
```

```python
def fail_runner(*, messages, context):
```

```python
def controlled_runner(*, messages, context):
```

```python
def synchronized_runner(*, messages, context):
```

runner 函数体保持原有行为。

- [ ] **Step 3: 写基础提示词和上下文传递失败测试**

追加：

```python
def test_chat_prepends_base_prompt_and_passes_context(tmp_path):
    received = []

    def runner(*, messages, context):
        received.append((list(messages), context))
        messages.append(
            {"role": "assistant", "content": "answer"}
        )
        return LLMResponse(llm_answer="answer")

    service, _ = build_service(tmp_path, runner)
    conversation = service.create_conversation(
        context=CONTEXT,
        system_prompt="回复使用简体中文",
    )

    service.chat(
        context=CONTEXT,
        conversation_id=conversation.conversation_id,
        question="查询订单",
    )

    messages, received_context = received[0]
    assert received_context is CONTEXT
    assert messages[:3] == [
        {
            "role": "system",
            "content": SUPPORT_SYSTEM_PROMPT,
        },
        {
            "role": "system",
            "content": "回复使用简体中文",
        },
        {"role": "user", "content": "查询订单"},
    ]
```

- [ ] **Step 4: 更新已有历史消息期望**

在 `test_second_turn_receives_previous_history_and_system_prompt` 的第二轮期望最前面加入：

```python
{
    "role": "system",
    "content": SUPPORT_SYSTEM_PROMPT,
},
```

原有会话提示词、第一轮用户消息、第一轮回答和第二轮用户消息顺序保持不变。

- [ ] **Step 5: 运行测试并确认失败**

Run:

```powershell
python -m pytest tests/application/test_chat_service.py -q
```

Expected: FAIL，因为基础提示词和 context 转发尚未实现。

- [ ] **Step 6: 创建客服基础提示词**

创建 `app/agent/prompts.py`：

```python
SUPPORT_SYSTEM_PROMPT = """你是 SupportPilot，面向电商售后客服团队的工单处理助手。

工作规则：
1. 订单、客户、物流和工单事实必须来自工具结果，不得编造。
2. 用户提供订单号并要求查询时，先调用 get_order，再根据问题调用 get_logistics。
3. 只有用户明确要求创建工单时，才调用 create_ticket。
4. 添加内部备注必须使用 add_ticket_note，并且只在用户明确要求时执行。
5. 工具结果的 ok 为 true 才能声称查询或操作成功。
6. 工具结果的 ok 为 false 时，根据 error.code 修正参数重试，或明确说明失败。
7. 回复应简洁说明已经核实的事实；创建工单成功时必须引用工具返回的 ticket_no。
8. 不得要求或猜测 organization_id、user_id、role 或 context。
"""
```

- [ ] **Step 7: 修改 ChatService 构造和消息组装**

在 `ChatService.__init__()` 增加：

```python
base_system_prompt: str
```

并保存：

```python
self._base_system_prompt = base_system_prompt.strip()
if not self._base_system_prompt:
    raise ValueError(
        "base_system_prompt must not be blank"
    )
```

把 `chat()` 中：

```python
messages: list[dict] = []
```

替换为：

```python
messages: list[dict] = [
    {
        "role": "system",
        "content": self._base_system_prompt,
    }
]
```

保留后续会话提示词和历史消息追加逻辑。

- [ ] **Step 8: 把 TenantContext 传给 Agent**

把：

```python
response = self._run_agent(messages=messages)
```

修改为：

```python
response = self._run_agent(
    messages=messages,
    context=context,
)
```

- [ ] **Step 9: 运行 ChatService 专项和全量回归**

Run:

```powershell
python -m pytest tests/application/test_chat_service.py -q
python -m pytest -q
```

Expected: 两条命令全部 PASS。

- [ ] **Step 10: 提交 Task 3**

```powershell
git add app/agent/prompts.py app/application/chat_service.py tests/application/test_chat_service.py
git commit -m "feat: pass tenant context into support agent"
```

---

### Task 4: 在 main.py 装配真实客服 Gateway 和请求级 Agent runner

**独立验收产物：** 生产聊天装配不再使用 `get_food` 和弹窗演示工具；`main.py` 使用同一个数据库创建 Tool Gateway，并将 `CustomerSupportAgentRunner` 注入 ChatService。

**Files:**
- Modify: `main.py`
- Modify: `tests/test_main.py`

**Interfaces:**
- Consumes: `create_customer_support_tool_gateway`
- Consumes: `CustomerSupportAgentRunner`
- Consumes: `SUPPORT_SYSTEM_PROMPT`
- Produces: production `support_tool_gateway`
- Produces: production `support_agent_runner`

- [ ] **Step 1: 写生产装配失败测试**

在 `tests/test_main.py` 的第一个测试中增加：

```python
  from app.agent.support_runner import (
      CustomerSupportAgentRunner,
  )
  from app.tools.support_gateway import (
      CustomerSupportToolGateway,
  )

  assert isinstance(
      main.support_tool_gateway,
      CustomerSupportToolGateway,
  )
  assert isinstance(
      main.support_agent_runner,
      CustomerSupportAgentRunner,
  )
  assert set(
      item["function"]["name"]
      for item in main.support_tool_gateway.definitions
  ) == {
      "get_order",
      "get_logistics",
      "create_ticket",
      "add_ticket_note",
  }
  assert not hasattr(main, "TOOL_FUNCTIONS")
  assert not hasattr(main, "TOOL_DEFINITIONS")
```

- [ ] **Step 2: 运行测试并确认失败**

Run:

```powershell
python -m pytest tests/test_main.py::test_main_wires_sqlite_service_without_global_messages -q
```

Expected: FAIL，因为 main 仍装配旧演示工具。

- [ ] **Step 3: 替换 imports**

从 `main.py` 删除：

```python
from functools import partial
from app.tools.registry import TOOL_FUNCTIONS, TOOL_DEFINITIONS
```

加入：

```python
from app.agent.prompts import SUPPORT_SYSTEM_PROMPT
from app.agent.support_runner import CustomerSupportAgentRunner
from app.core.config import MODEL_NAME
from app.tools.support_factory import (
    create_customer_support_tool_gateway,
)
```

- [ ] **Step 4: 替换全局演示工具装配**

删除原有：

```python
run_agent = partial(
    run_one_turn,
    client=client,
    tool_definitions=TOOL_DEFINITIONS,
    tool_functions=TOOL_FUNCTIONS,
    on_event=None,
)
```

改为：

```python
support_tool_gateway = (
    create_customer_support_tool_gateway(database_path)
)
support_agent_runner = CustomerSupportAgentRunner(
    client=client,
    gateway=support_tool_gateway,
    model_name=MODEL_NAME,
)
```

删除已经不再使用的 `run_one_turn` import。

- [ ] **Step 5: 更新 ChatService 装配**

把 ChatService 构造改为：

```python
chat_service = ChatService(
    store=session_store,
    run_agent=support_agent_runner,
    locks=ConversationLockRegistry(),
    base_system_prompt=SUPPORT_SYSTEM_PROMPT,
)
```

- [ ] **Step 6: 运行 main 和 API 回归**

Run:

```powershell
python -m pytest tests/test_main.py tests/api -q
python -m pytest -q
```

Expected: 两条命令全部 PASS；测试只构造客户端，不发起真实模型请求。

- [ ] **Step 7: 提交 Task 4**

```powershell
git add main.py tests/test_main.py
git commit -m "feat: wire controlled support tools into chat"
```

---

### Task 5: 用假模型和真实 SQLite 跑通客服黄金路径

**独立验收产物：** 一个真实 `ChatService` 回合依次执行订单查询、物流查询、工单创建，并将工具消息和最终回复保存进当前企业当前用户的会话历史。

**Files:**
- Create: `tests/agent/test_customer_support_golden_path.py`

**Interfaces:**
- Consumes: real SQLite stores
- Consumes: real `CustomerSupportToolGateway`
- Consumes: real `CustomerSupportAgentRunner`
- Uses: fake OpenAI-compatible client
- Verifies: `get_order → get_logistics → create_ticket → final answer`

- [ ] **Step 1: 创建假模型响应构造器**

创建 `tests/agent/test_customer_support_golden_path.py`：

```python
import json
from types import SimpleNamespace

from app.agent.prompts import SUPPORT_SYSTEM_PROMPT
from app.agent.support_runner import CustomerSupportAgentRunner
from app.application.chat_service import ChatService
from app.application.organization_service import TenantContext
from app.concurrency.conversation_locks import (
    ConversationLockRegistry,
)
from app.customers.sqlite_store import SQLiteCustomerStore
from app.orders.base import OrderStatus
from app.orders.sqlite_store import SQLiteOrderStore
from app.organizations.base import MembershipRole
from app.organizations.sqlite_store import SQLiteOrganizationStore
from app.sessions.sqlite_store import SQLiteSessionStore
from app.tickets.sqlite_store import SQLiteTicketStore
from app.tools.support_factory import (
    create_customer_support_tool_gateway,
)
from app.users.sqlite_store import SQLiteUserStore


def tool_call_response(call_id, name, arguments):
    tool_call = SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(
            name=name,
            arguments=json.dumps(
                arguments,
                ensure_ascii=False,
            ),
        ),
    )
    message = SimpleNamespace(
        content="",
        reasoning_content=f"调用 {name}",
        tool_calls=[tool_call],
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message)]
    )


def final_response(content):
    message = SimpleNamespace(
        content=content,
        reasoning_content="根据工具结果生成回复",
        tool_calls=None,
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message)]
    )


class FakeCompletionClient:
    def __init__(self, responses):
        self._responses = iter(responses)
        self.calls = []
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(
                create=self.create
            )
        )

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return next(self._responses)
```

- [ ] **Step 2: 创建真实业务数据和聊天装配 helper**

追加：

```python
def build_golden_path(tmp_path):
    database_path = tmp_path / "golden.db"
    users = SQLiteUserStore(database_path)
    organizations = SQLiteOrganizationStore(database_path)
    customers = SQLiteCustomerStore(database_path)
    orders = SQLiteOrderStore(database_path)
    sessions = SQLiteSessionStore(database_path)

    user = users.create_user(
        username="alice",
        password_hash="hash",
    )
    organization = organizations.create_with_admin(
        name="Company A",
        admin_user_id=user.user_id,
    )
    customer = customers.create_customer(
        organization_id=organization.organization_id,
        customer_no="CUST-GOLDEN-001",
        name="林晓",
    )
    order = orders.create_order(
        organization_id=organization.organization_id,
        order_no="ORD-GOLDEN-001",
        customer_id=customer.customer_id,
        status=OrderStatus.PROCESSING,
        item_summary="无线耳机 x1",
        total_amount_cents=39900,
        currency="CNY",
        placed_at="2026-07-25T08:00:00+00:00",
        promised_ship_at="2026-07-28T08:00:00+00:00",
    )
    context = TenantContext(
        user_id=user.user_id,
        organization_id=organization.organization_id,
        role=MembershipRole.ADMIN,
    )
    responses = [
        tool_call_response(
            "call-order",
            "get_order",
            {"order_no": order.order_no},
        ),
        tool_call_response(
            "call-logistics",
            "get_logistics",
            {"order_no": order.order_no},
        ),
        tool_call_response(
            "call-ticket",
            "create_ticket",
            {
                "order_no": order.order_no,
                "summary": "订单超过承诺时间仍未发货",
                "category": "logistics",
                "priority": "high",
            },
        ),
        final_response(
            "已核实订单尚未产生物流记录，并已创建工单 "
            "TKT-GOLDEN-001。"
        ),
    ]
    client = FakeCompletionClient(responses)
    gateway = create_customer_support_tool_gateway(
        database_path,
        ticket_no_factory=lambda: "TKT-GOLDEN-001",
    )
    agent = CustomerSupportAgentRunner(
        client=client,
        gateway=gateway,
        model_name="fake-support-model",
    )
    chat = ChatService(
        store=sessions,
        run_agent=agent,
        locks=ConversationLockRegistry(),
        base_system_prompt=SUPPORT_SYSTEM_PROMPT,
    )
    conversation = chat.create_conversation(
        context=context
    )
    return {
        "database_path": database_path,
        "client": client,
        "chat": chat,
        "context": context,
        "conversation": conversation,
        "sessions": sessions,
        "tickets": SQLiteTicketStore(database_path),
    }
```

- [ ] **Step 3: 写完整黄金路径测试**

追加：

```python
def test_delayed_order_creates_ticket_and_grounded_reply(
    tmp_path,
):
    scope = build_golden_path(tmp_path)

    result = scope["chat"].chat(
        context=scope["context"],
        conversation_id=scope["conversation"].conversation_id,
        question=(
            "订单 ORD-GOLDEN-001 已超过承诺发货时间，"
            "请查询订单和物流；如果确实还没有发货，"
            "请创建一个高优先级物流工单并告诉我工单号。"
        ),
    )

    assert result.llm_answer == (
        "已核实订单尚未产生物流记录，并已创建工单 "
        "TKT-GOLDEN-001。"
    )
    assert [
        event.tool_call_name
        for event in result.events
        if event.type == "tool_call.completed"
    ] == [
        "get_order",
        "get_logistics",
        "create_ticket",
    ]
    ticket = scope["tickets"].get_by_no(
        organization_id=scope["context"].organization_id,
        ticket_no="TKT-GOLDEN-001",
    )
    assert ticket.summary == "订单超过承诺时间仍未发货"
```

- [ ] **Step 4: 验证模型调用参数和消息持久化**

在同一测试末尾增加：

```python
    assert len(scope["client"].calls) == 4
    for call in scope["client"].calls:
        assert call["model"] == "fake-support-model"
        assert {
            item["function"]["name"]
            for item in call["tools"]
        } == {
            "get_order",
            "get_logistics",
            "create_ticket",
            "add_ticket_note",
        }

    history = scope["sessions"].load_messages(
        organization_id=scope["context"].organization_id,
        user_id=scope["context"].user_id,
        conversation_id=(
            scope["conversation"].conversation_id
        ),
    )
    tool_messages = [
        message
        for message in history
        if message["role"] == "tool"
    ]
    assert len(tool_messages) == 3
    assert '"availability": "not_created"' in (
        tool_messages[1]["content"]
    )
    assert '"ticket_no": "TKT-GOLDEN-001"' in (
        tool_messages[2]["content"]
    )
    assert history[-1]["content"] == result.llm_answer
```

- [ ] **Step 5: 运行黄金路径测试**

Run:

```powershell
python -m pytest tests/agent/test_customer_support_golden_path.py -q
```

Expected: `1 passed`，且没有任何网络请求。

- [ ] **Step 6: 运行 Agent、Gateway 和全量回归**

Run:

```powershell
python -m pytest tests/agent tests/tools -q
python -m pytest -q
```

Expected: 两条命令全部 PASS。

- [ ] **Step 7: 提交 Task 5**

```powershell
git add tests/agent/test_customer_support_golden_path.py
git commit -m "test: cover support agent golden path"
```

---

### Task 6: 验证同一 Agent 的跨租户重新绑定和工具失败恢复

**独立验收产物：** 同一个 Agent runner 连续服务两个企业时返回各自订单数据；Gateway 返回参数错误后，模型可以修正参数再次调用，且最终回复不能把失败调用说成成功。

**Files:**
- Modify: `tests/agent/test_customer_support_golden_path.py`

**Interfaces:**
- Verifies: request-scoped tenant rebinding
- Verifies: `ok: false` tool result recovery
- Verifies: no cross-tenant fact leakage

- [ ] **Step 1: 增加单工具查询假响应 helper**

在黄金路径测试文件追加：

```python
def order_query_responses(order_no, final_text):
    return [
        tool_call_response(
            "call-order",
            "get_order",
            {"order_no": order_no},
        ),
        final_response(final_text),
    ]
```

- [ ] **Step 2: 写两个企业同订单号隔离测试**

追加：

```python
def test_same_agent_runner_rebinds_between_tenants(tmp_path):
    database_path = tmp_path / "tenants.db"
    users = SQLiteUserStore(database_path)
    organizations = SQLiteOrganizationStore(database_path)
    customers = SQLiteCustomerStore(database_path)
    orders = SQLiteOrderStore(database_path)

    contexts = []
    for username, company, item_summary in (
        ("alice", "Company A", "企业A耳机 x1"),
        ("bob", "Company B", "企业B键盘 x1"),
    ):
        user = users.create_user(
            username=username,
            password_hash="hash",
        )
        organization = organizations.create_with_admin(
            name=company,
            admin_user_id=user.user_id,
        )
        customer = customers.create_customer(
            organization_id=organization.organization_id,
            customer_no="CUST-001",
            name=f"{company} Customer",
        )
        orders.create_order(
            organization_id=organization.organization_id,
            order_no="ORD-SHARED-001",
            customer_id=customer.customer_id,
            status=OrderStatus.PROCESSING,
            item_summary=item_summary,
            total_amount_cents=10000,
            currency="CNY",
            placed_at="2026-07-25T08:00:00+00:00",
        )
        contexts.append(
            TenantContext(
                user_id=user.user_id,
                organization_id=organization.organization_id,
                role=MembershipRole.ADMIN,
            )
        )

    client = FakeCompletionClient(
        order_query_responses(
            "ORD-SHARED-001",
            "企业A查询完成",
        )
        + order_query_responses(
            "ORD-SHARED-001",
            "企业B查询完成",
        )
    )
    gateway = create_customer_support_tool_gateway(
        database_path
    )
    agent = CustomerSupportAgentRunner(
        client=client,
        gateway=gateway,
        model_name="fake-model",
    )

    first_messages = [
        {"role": "user", "content": "查询订单"}
    ]
    second_messages = [
        {"role": "user", "content": "查询订单"}
    ]
    agent(messages=first_messages, context=contexts[0])
    agent(messages=second_messages, context=contexts[1])

    assert "企业A耳机 x1" in first_messages[2]["content"]
    assert "企业B键盘 x1" in second_messages[2]["content"]
    assert "企业B键盘 x1" not in first_messages[2]["content"]
    assert "企业A耳机 x1" not in second_messages[2]["content"]
```

这里每组消息的索引 `2` 是工具消息：索引 `0` 为用户消息，索引 `1` 为 assistant 工具调用。

- [ ] **Step 3: 写工具参数失败后修正重试测试**

追加：

```python
def test_agent_can_retry_after_gateway_validation_failure(
    tmp_path,
):
    database_path = tmp_path / "retry.db"
    users = SQLiteUserStore(database_path)
    organizations = SQLiteOrganizationStore(database_path)
    user = users.create_user(
        username="alice",
        password_hash="hash",
    )
    organization = organizations.create_with_admin(
        name="Company A",
        admin_user_id=user.user_id,
    )
    context = TenantContext(
        user_id=user.user_id,
        organization_id=organization.organization_id,
        role=MembershipRole.ADMIN,
    )
    client = FakeCompletionClient(
        [
            tool_call_response(
                "bad-ticket",
                "create_ticket",
                {
                    "summary": "客户要求创建工单",
                    "category": "other",
                    "priority": "medium",
                },
            ),
            tool_call_response(
                "good-ticket",
                "create_ticket",
                {
                    "customer_no": "CUST-MISSING",
                    "summary": "客户要求创建工单",
                    "category": "other",
                    "priority": "medium",
                },
            ),
            final_response(
                "无法创建工单：没有找到对应客户。"
            ),
        ]
    )
    gateway = create_customer_support_tool_gateway(
        database_path
    )
    agent = CustomerSupportAgentRunner(
        client=client,
        gateway=gateway,
        model_name="fake-model",
    )
    messages = [
        {"role": "user", "content": "请创建工单"}
    ]

    result = agent(messages=messages, context=context)

    first_tool_result = json.loads(messages[2]["content"])
    second_tool_result = json.loads(messages[4]["content"])
    assert first_tool_result["ok"] is False
    assert first_tool_result["error"]["code"] == (
        "INVALID_ARGUMENTS"
    )
    assert second_tool_result["ok"] is False
    assert second_tool_result["error"]["code"] == (
        "CUSTOMER_NOT_FOUND"
    )
    assert "无法创建工单" in result.llm_answer
    assert "创建成功" not in result.llm_answer
```

- [ ] **Step 4: 运行隔离、失败恢复和全量回归**

Run:

```powershell
python -m pytest tests/agent/test_customer_support_golden_path.py -q
python -m pytest -q
```

Expected: 两条命令全部 PASS。

- [ ] **Step 5: 提交 Task 6**

```powershell
git add tests/agent/test_customer_support_golden_path.py
git commit -m "test: verify tenant-bound agent tools"
```

---

### Task 7: 更新产品文档并执行第五步最终验收

**独立验收产物：** README 可以准确描述当前产品能力和边界；开发文档说明请求级工具绑定和黄金路径；所有专项与全量测试通过。

**Files:**
- Create: `README.md`
- Create: `docs/customer-support-agent-golden-path.md`

**Interfaces:**
- Documents: current product definition
- Documents: request-scoped tool binding
- Documents: next-stage boundary

- [ ] **Step 1: 编写根 README 产品定义**

创建根目录 `README.md`，写入以下内容：

```markdown
# SupportPilot

SupportPilot 是一个面向电商售后客服团队的多租户工单处理 Agent。它能够理解
客服请求，在当前企业范围内查询客户、订单和物流信息，辅助创建工单和添加内部
备注，并根据真实工具结果生成回复。不同企业的成员、会话、业务数据和未来知识库
互相隔离。

当前已完成：

- 用户认证、多租户和 `admin` / `agent` 最小 RBAC。
- 企业范围的客户、订单、物流、工单和工单备注。
- 不依赖 LLM 的确定性客服业务服务。
- 服务器上下文绑定的受控 Tool Gateway。
- “查询订单 → 查询物流 → 创建工单 → 生成回复”Agent 黄金路径。

当前不包含：

- 企业知识库 RAG。
- 退款、补偿和审批。
- LangGraph 工作流。
- 真实电商、物流和 CRM 集成。
```

- [ ] **Step 2: 编写黄金路径架构文档**

创建 `docs/customer-support-agent-golden-path.md`，至少包含以下完整内容：

```markdown
# SupportPilot 客服 Agent 黄金路径

## 请求级工具绑定

每次聊天请求先通过认证和 membership 校验得到 `TenantContext`。ChatService 将
该上下文传给 `CustomerSupportAgentRunner`，runner 再调用
`gateway.bind(context)` 创建本次请求专属工具函数。

工具函数不能在应用启动时全局绑定，因为同一个进程会同时服务多个企业。

## 黄金路径

```text
用户报告订单延迟并明确要求创建工单
→ get_order
→ get_logistics
→ create_ticket
→ 模型引用工具返回的 ticket_no 生成回复
```

`availability=not_created` 表示订单存在但尚未产生物流记录。只有
`create_ticket` 返回 `ok=true` 时，回复才能声称工单创建成功。

## 可靠性

- Agent 工具轮数有固定上限。
- 工具参数由 Pydantic 校验。
- Tool Gateway 注入可信租户和操作人。
- 意外工具异常不会向模型暴露内部错误。
- Agent 回合失败时不保存部分会话消息。

## 测试

黄金路径测试使用假模型响应和真实 SQLite Store，不访问外部模型 API。测试验证
工具顺序、数据库写入、消息持久化、跨租户重新绑定和失败结果恢复。

## 下一阶段

下一阶段再增加企业范围 RAG、退款审批和更复杂工作流。RAG 检索必须继续使用
`TenantContext.organization_id` 作为强制过滤条件。
```

- [ ] **Step 3: 验证生产代码不再装配演示工具**

Run:

```powershell
Select-String `
  -Path .\main.py `
  -Pattern "get_food|send_popup_to_user|TOOL_FUNCTIONS|TOOL_DEFINITIONS"
```

Expected: 无输出。

- [ ] **Step 4: 验证请求级绑定调用存在**

Run:

```powershell
Select-String `
  -Path .\app\agent\support_runner.py `
  -Pattern "gateway.bind"
```

Expected: 正好找到一处生产调用。

- [ ] **Step 5: 运行第五步专项验收**

Run:

```powershell
python -m pytest `
  tests/agent/test_runner.py `
  tests/agent/test_support_runner.py `
  tests/agent/test_customer_support_golden_path.py `
  tests/application/test_chat_service.py `
  tests/tools `
  tests/test_main.py `
  -q
```

Expected: 全部 PASS，不发生网络访问。

- [ ] **Step 6: 运行全量回归**

Run:

```powershell
python -m pytest -q
```

Expected: 全部 PASS。

- [ ] **Step 7: 提交 Task 7**

```powershell
git add README.md docs/customer-support-agent-golden-path.md
git commit -m "docs: describe support agent golden path"
```

---

## 3. 最终验收清单

- [ ] `python -m pytest -q` 全部通过。
- [ ] Alembic head 仍为 `0008_ticket_comments`。
- [ ] 所有 Agent 测试均使用假客户端，没有访问真实模型 API。
- [ ] `run_one_turn()` 有正数工具轮次上限。
- [ ] 模型名不再硬编码在工具循环内部。
- [ ] `CustomerSupportAgentRunner` 每次调用都执行 `gateway.bind(context)`。
- [ ] 两次不同企业调用不会复用第一次绑定的工具函数。
- [ ] `ChatService` 始终加入服务器客服基础提示词。
- [ ] `ChatService` 把收到的同一个 `TenantContext` 传给 Agent runner。
- [ ] `main.py` 不再生产装配 `get_food` 和 `send_popup_to_user`。
- [ ] 黄金路径工具顺序为 `get_order → get_logistics → create_ticket`。
- [ ] 工单真实写入当前企业数据库范围。
- [ ] 最终回复引用工具返回的真实 `ticket_no`。
- [ ] 工具消息和最终 assistant 消息保存到正确会话。
- [ ] 同订单号在不同企业返回各自业务数据。
- [ ] `ok=false` 时模型不会声称操作成功。
- [ ] Agent 失败回合仍不持久化部分消息。
- [ ] README 和黄金路径文档与当前代码一致。

## 4. 第五步完成后的边界

完成本计划后，开发顺序状态为：

```text
1. 最小多租户基础                   已完成
2. 模拟客服业务数据                 已完成
3. 不依赖 LLM 的确定性业务服务      已完成
4. 受控 Tool Gateway               已完成
5. Agent 选择工具并生成回复         本计划完成后即完成
6. RAG、退款审批和 LangGraph        后续
```

下一阶段不要同时实现三个大型能力。推荐继续拆成独立计划：

```text
1. tenant-scoped-rag
2. refund-approval-workflow
3. agent-observability-and-evals
4. langgraph-workflow-migration
```
