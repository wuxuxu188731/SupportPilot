# Web Agent Phase 1 Module Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改变现有 HTTP 和 Agent 行为的前提下，将 `main.py` 拆成职责清晰、可独立测试的模块。

**Architecture:** 保持当前 `/chat`、`/setSys` 和全局历史行为，先机械移动代码并显式传递依赖。每个任务结束时全部测试必须通过；多会话存储留到阶段二。

**Tech Stack:** Python 3.10、FastAPI、Pydantic、OpenAI Python SDK、pytest。

## Global Constraints

- 不访问真实 DeepSeek API；测试必须使用假客户端。
- 不在本阶段修改 HTTP 路径、响应字段或 Agent 事件语义。
- 不在本阶段实现数据库、Redis、鉴权或多 worker。
- 每次只移动一个职责，测试通过后再继续。
- 不把测试专用工具长期保留在生产工具注册表中。

---

## 文件结构

阶段一结束时应具有：

```text
app/
├── __init__.py
├── agent/
│   ├── __init__.py
│   ├── events.py
│   └── runner.py
├── api/
│   ├── __init__.py
│   └── router.py
├── core/
│   ├── __init__.py
│   └── config.py
├── schemas/
│   ├── __init__.py
│   └── chat.py
├── sessions/
│   ├── __init__.py
│   └── legacy_file.py
└── tools/
    ├── __init__.py
    ├── builtin.py
    └── registry.py
main.py
tests/
├── agent/test_runner.py
└── tools/test_arguments.py
```

### Task 1: 固定测试基线并整理测试辅助函数

**Files:**
- Modify: `tests/test_agent_runner.py`
- Modify: `tests/test_tool_execution.py`

**Interfaces:**
- Consumes: 当前 `main.run_one_turn`。
- Produces: 12 个真正的行为测试；辅助函数不再被 pytest 当成测试。

- [ ] **Step 1: 运行当前基线**

Run:

```powershell
$env:DEEPSEEK_API_KEY='test-only'
python -B -m pytest -p no:cacheprovider -q
```

Expected: `13 passed`。其中一个是被误收集的 `test_init_client`。

- [ ] **Step 2: 把 `test_init_client` 改为普通辅助函数**

将：

```python
def test_init_client(monkeypatch):
```

改成：

```python
def install_direct_answer_client(monkeypatch):
```

并把调用处：

```python
test_init_client(monkeypatch=monkeypatch)
```

改成：

```python
install_direct_answer_client(monkeypatch=monkeypatch)
```

- [ ] **Step 3: 修正 API Key 的导入顺序**

测试文件顶部必须先设置测试 Key，再导入项目模块：

```python
import os
from types import SimpleNamespace

os.environ["DEEPSEEK_API_KEY"] = "test-only-key"

from .. import main
```

删除设置环境变量之前的 `from ..main import run_one_turn, LLMResponse`。

- [ ] **Step 4: 验证测试整理没有改变行为**

Run:

```powershell
$env:DEEPSEEK_API_KEY='test-only'
python -B -m pytest -p no:cacheprovider -q
```

Expected: `12 passed`。

- [ ] **Step 5: 提交（由用户自行决定是否执行）**

```powershell
git add tests/test_agent_runner.py tests/test_tool_execution.py
git commit -m "test: clean up agent runner fixtures"
```

### Task 2: 提取 AgentEvent 与 HTTP Schema

**Files:**
- Create: `app/__init__.py`
- Create: `app/agent/__init__.py`
- Create: `app/agent/events.py`
- Create: `app/schemas/__init__.py`
- Create: `app/schemas/chat.py`
- Modify: `main.py`

**Interfaces:**
- Produces: `AgentEvent`、`LLMResponse`、`SystemPrompt`、`UserQuestion`，字段与当前版本完全一致。

- [ ] **Step 1: 创建包目录和空的 `__init__.py`**

只创建上述目录与 `__init__.py`，不要移动其他代码。

- [ ] **Step 2: 将 AgentEvent 移到 `app/agent/events.py`**

```python
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class AgentEvent(BaseModel):
    type: Literal[
        "tool_call.requested",
        "tool_call.started",
        "tool_call.completed",
        "tool_call.failed",
    ]
    timestamp: datetime = Field(default_factory=datetime.now)
    tool_call_id: str
    tool_call_name: str
    tool_call_arguments: dict[str, Any] = Field(default_factory=dict)
    result: Any | None = None
    error: str | None = None
    duration_ms: float | None = None
```

- [ ] **Step 3: 将 HTTP Schema 移到 `app/schemas/chat.py`**

```python
from pydantic import BaseModel, Field

from app.agent.events import AgentEvent


class LLMResponse(BaseModel):
    llm_answer: str | None = None
    llm_reasoning_content: str | None = None
    events: list[AgentEvent] = Field(default_factory=list)


class SystemPrompt(BaseModel):
    prompt: str


class UserQuestion(BaseModel):
    question: str
```

- [ ] **Step 4: `main.py` 改为导入这些类**

```python
from app.agent.events import AgentEvent
from app.schemas.chat import LLMResponse, SystemPrompt, UserQuestion
```

删除 `main.py` 中四个重复类定义，其他代码不动。

- [ ] **Step 5: 运行全部测试**

Expected: `12 passed`。

### Task 3: 提取具体工具与工具注册表

**Files:**
- Create: `app/tools/__init__.py`
- Create: `app/tools/builtin.py`
- Create: `app/tools/registry.py`
- Modify: `main.py`

**Interfaces:**
- Produces: `TOOL_FUNCTIONS: dict[str, Callable]`、`TOOL_DEFINITIONS: list[dict]`。

- [ ] **Step 1: 将四个当前工具机械移动到 `app/tools/builtin.py`**

第一小步先保持行为，包含当前测试工具：

```python
from typing import Any


def get_food() -> Any:
    return "其实啥也没有-v-"


def send_popup_to_user(content: str):
    return f"成功向用户发一个弹窗，弹窗的内容是{content}"


def dict_result_test() -> dict:
    return {"name": "小明", "content": "Hello"}


def raise_error_func():
    raise ValueError("执行时异常")
```

- [ ] **Step 2: 将 `function_map` 和 `tools` 移到 `app/tools/registry.py`**

使用大写常量名：

```python
from app.tools.builtin import (
    dict_result_test,
    get_food,
    raise_error_func,
    send_popup_to_user,
)

TOOL_FUNCTIONS = {
    "get_food": get_food,
    "send_popup_to_user": send_popup_to_user,
    "dict_result_test": dict_result_test,
    "raise_error_func": raise_error_func,
}

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "get_food",
            "description": "饿了就可以调用喵",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_popup_to_user",
            "description": "当你想提醒用户某件事情的时候就可以调用，直接向用户发出弹窗提醒用户",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "弹窗中的消息内容",
                    }
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "dict_result_test",
            "description": "测试返回字典的工具结果能否转换成字符串",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "raise_error_func",
            "description": "测试工具执行异常能否被正常处理",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]
```

此步骤只允许机械移动，不能顺便重写 Schema。

- [ ] **Step 3: `main.py` 使用兼容别名**

```python
from app.tools.registry import TOOL_DEFINITIONS, TOOL_FUNCTIONS

tools = TOOL_DEFINITIONS
function_map = TOOL_FUNCTIONS
```

删除原工具函数、`function_map` 和 `tools` 定义。

- [ ] **Step 4: 运行全部测试**

Expected: `12 passed`。

### Task 4: 提取 AgentRunner 并显式注入依赖

**Files:**
- Create: `app/agent/runner.py`
- Modify: `main.py`
- Modify: `tests/test_agent_runner.py`
- Move: `tests/test_tool_execution.py` → `tests/tools/test_arguments.py`
- Move: `tests/test_agent_runner.py` → `tests/agent/test_runner.py`

**Interfaces:**
- Produces: `run_one_turn(*, messages: list[dict], client: Any, tool_definitions: list[dict], tool_functions: dict[str, Callable[..., Any]], on_event: Callable[[AgentEvent], None] | None = None) -> LLMResponse`。

- [ ] **Step 1: 先更新一个编排测试，直接调用新接口并观察失败**

把测试中的调用目标改为 `app.agent.runner.run_one_turn`，显式传入 fake client、工具定义和工具函数。此时模块尚不存在。

Run: 该单个测试。

Expected: FAIL，原因是 `app.agent.runner` 不存在。

- [ ] **Step 2: 创建 `app/agent/runner.py`**

机械移动以下函数：

- `stringify_tool_result`
- `parse_tool_arguments`
- `build_error_result`
- `run_one_turn`

然后只做三项依赖替换：

```python
# 原 client 改为参数 client；请求参数保持完整
response = client.chat.completions.create(
    model="deepseek-v4-flash",
    messages=messages,
    tools=tool_definitions,
    extra_body={"thinking": {"type": "enabled"}},
)

# 原 tools
tools=tool_definitions

# 原 function_map
func = tool_functions.get(func_name)
```

不要在移动过程中改变事件顺序或异常语义。

- [ ] **Step 3: 逐个迁移剩余 Agent 编排测试**

每个测试自行构造：

```python
tool_functions = {
    "get_food": lambda: "其实啥也没有-v-",
}
```

返回字典和抛异常的测试函数定义在测试文件内，不再依赖生产注册表里的测试工具。

- [ ] **Step 4: 从生产工具注册表删除测试专用工具**

从 `app/tools/builtin.py` 和 `TOOL_FUNCTIONS`、`TOOL_DEFINITIONS` 删除：

- `dict_result_test`
- `raise_error_func`

真实工具只保留 `get_food` 和 `send_popup_to_user`。

- [ ] **Step 5: `main.py` 调用显式依赖接口**

```python
resp = run_one_turn(
    messages=messages,
    client=client,
    tool_definitions=TOOL_DEFINITIONS,
    tool_functions=TOOL_FUNCTIONS,
    on_event=None,
)
```

- [ ] **Step 6: 运行 Agent 测试与全部测试**

Expected: Agent 测试全部通过，随后全部 `12 passed`。

### Task 5: 提取配置和旧文件历史存储

**Files:**
- Create: `app/core/__init__.py`
- Create: `app/core/config.py`
- Create: `app/sessions/__init__.py`
- Create: `app/sessions/legacy_file.py`
- Modify: `main.py`

**Interfaces:**
- Produces: `create_llm_client()`、`read_history_chat()`、`save_history_chat()`。

- [ ] **Step 1: 创建 `app/core/config.py`**

```python
import os

from openai import OpenAI

MODEL_NAME = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")


def create_llm_client() -> OpenAI:
    return OpenAI(
        api_key=os.environ["DEEPSEEK_API_KEY"],
        base_url="https://api.deepseek.com",
    )
```

- [ ] **Step 2: 移动旧文件历史函数**

`app/sessions/legacy_file.py` 保持当前 JSON 行为：

```python
import json


def read_history_chat(path: str = "chat_history.json") -> list[dict]:
    try:
        with open(path, mode="r", encoding="utf-8") as file:
            return json.load(file)
    except FileNotFoundError:
        return []


def save_history_chat(messages: list[dict], path: str = "chat_history.json") -> None:
    with open(path, mode="w", encoding="utf-8") as file:
        json.dump(messages, file, indent=2, ensure_ascii=False)
```

- [ ] **Step 3: `main.py` 改为创建和导入依赖**

```python
from app.core.config import create_llm_client
from app.sessions.legacy_file import read_history_chat, save_history_chat

client = create_llm_client()
messages = read_history_chat()
```

- [ ] **Step 4: 运行全部测试**

Expected: `12 passed`。

### Task 6: 提取 Router，让 main.py 只负责组装

**Files:**
- Create: `app/api/__init__.py`
- Create: `app/api/router.py`
- Modify: `main.py`
- Test: `tests/api/test_router.py`

**Interfaces:**
- Produces: `create_router(*, messages, run_agent, save_history) -> APIRouter`。

- [ ] **Step 1: 写一个 Router 冒烟测试并观察失败**

测试创建 FastAPI，挂载待创建 Router，并验证现有路由 `/chat` 和 `/setSys` 存在。Expected: FAIL，因为 `create_router` 尚不存在。

- [ ] **Step 2: 创建依赖明确的 Router 工厂**

```python
from typing import Any, Callable

from fastapi import APIRouter, HTTPException

from app.schemas.chat import LLMResponse, SystemPrompt, UserQuestion


def create_router(
    *,
    messages: list[dict],
    run_agent: Callable[..., LLMResponse],
    save_history: Callable[[list[dict]], None],
) -> APIRouter:
    router = APIRouter()

    @router.post("/setSys", tags=["设置系统提示词"])
    async def set_system_prompt(prompt: SystemPrompt) -> bool:
        system_prompt = prompt.prompt.strip()
        if not system_prompt:
            raise HTTPException(status_code=401, detail="系统提示词为空")
        messages.append({"role": "system", "content": system_prompt})
        return True

    @router.post("/chat", response_model=LLMResponse, tags=["向模型聊天"])
    def chat(question: UserQuestion) -> LLMResponse:
        messages.append({"role": "user", "content": question.question})
        response = run_agent(messages=messages)
        save_history(messages)
        return response

    return router
```

`run_agent` 由 `main.py` 使用 `functools.partial` 或一个小包装函数绑定 client、tool_definitions 和 tool_functions。

- [ ] **Step 3: 将 main.py 缩减为组装代码**

```python
from functools import partial

from fastapi import FastAPI

from app.agent.runner import run_one_turn
from app.api.router import create_router
from app.core.config import create_llm_client
from app.sessions.legacy_file import read_history_chat, save_history_chat
from app.tools.registry import TOOL_DEFINITIONS, TOOL_FUNCTIONS

app = FastAPI()
client = create_llm_client()
messages = read_history_chat()

run_agent = partial(
    run_one_turn,
    client=client,
    tool_definitions=TOOL_DEFINITIONS,
    tool_functions=TOOL_FUNCTIONS,
    on_event=None,
)

app.include_router(
    create_router(
        messages=messages,
        run_agent=run_agent,
        save_history=save_history_chat,
    )
)
```

- [ ] **Step 4: 运行 Router 测试和全部测试**

Expected: Router 测试通过；全部测试通过。

### Task 7: 阶段一验收

**Files:**
- Review: `main.py`
- Review: `app/**`
- Review: `tests/**`

- [ ] **Step 1: 运行全部测试**

```powershell
$env:DEEPSEEK_API_KEY='test-only'
python -B -m pytest -p no:cacheprovider -q
```

Expected: 全部 PASS，且不访问真实 DeepSeek。

- [ ] **Step 2: 检查职责边界**

验收条件：

- `main.py` 只包含依赖创建和 FastAPI 组装。
- Agent 测试直接测试 `app.agent.runner`。
- 测试专用工具不在生产注册表。
- Router 不包含工具执行循环。
- AgentRunner 不导入 FastAPI，也不读写历史文件。
- 阶段一仍只有一个全局 messages；它将在阶段二被删除。

- [ ] **Step 3: 提交（由用户自行决定是否执行）**

```powershell
git add app main.py tests
git commit -m "refactor: split web agent responsibilities"
```
