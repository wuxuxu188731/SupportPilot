# 同步→异步迁移 GPT 提示词

> 复制下面全部内容，粘贴到 GPT 对话框即可。

---

你是一名资深 Python 异步编程专家。我现在有一个 FastAPI Agent 项目，
全部是同步代码（def、threading.Lock、sqlite3），我想把它迁移成异步版本
（async def、asyncio.Lock、aiosqlite），但我是初学者，不知道从哪里下手。

请你完成以下工作：

## 一、项目背景

我的项目是一个多用户对话 Agent，架构如下：

main.py → 依赖装配
  ├── router.py → HTTP 层（FastAPI）
  ├── chat_service.py → 业务层（加锁、拼装消息、调用 Agent、持久化）
  ├── conversation_locks.py → 按 conversation_id 分段的线程锁
  ├── sqlite_store.py → SQLite 持久化（会话 + 消息的 CRUD）
  ├── runner.py → Agent 编排循环（调用 LLM + 工具调用）
  ├── base.py → SessionStore 接口（Protocol）
  └── schemas/chat.py → Pydantic 请求/响应模型

## 二、当前源代码

### 1. main.py（装配入口）
```python
from functools import partial
from fastapi import FastAPI
from app.agent.runner import run_one_turn
from app.api.router import creat_router
from app.application.chat_service import ChatService
from app.concurrency.conversation_locks import ConversationLockRegistry
from app.core.config import create_llm_client, get_chat_db_path
from app.sessions.sqlite_store import SQLiteSessionStore
from app.tools.registry import TOOL_FUNCTIONS, TOOL_DEFINITIONS

app = FastAPI()
client = create_llm_client()

session_store = SQLiteSessionStore(database_path=get_chat_db_path())
run_agent = partial(
    run_one_turn,
    client=client,
    tool_definitions=TOOL_DEFINITIONS,
    tool_functions=TOOL_FUNCTIONS,
    on_event=None
)

chat_service = ChatService(
    store=session_store,
    run_agent=run_agent,
    locks=ConversationLockRegistry()
)

app.include_router(creat_router(chat_service=chat_service))
```

### 2. router.py（HTTP 层）
```python
from fastapi import APIRouter, HTTPException
from app.schemas.chat import (
    ChatRequest, ConversationCreated, CreateConversationRequest,
    LLMResponse, SystemPromptUpdated, UpdateSystemPromptRequest,
)
from app.sessions.base import ConversationNotFoundError
from app.application.chat_service import ChatService

def creat_router(*, chat_service: ChatService) -> APIRouter:
    router = APIRouter()

    @router.post("/conversations/", response_model=ConversationCreated, status_code=201)
    def create_conversation(request: CreateConversationRequest) -> ConversationCreated:
        user_id = request.user_id.strip()
        if not user_id:
            raise HTTPException(status_code=422, detail="user_id must not be blank")
        system_prompt = request.system_prompt.strip() if request.system_prompt else None
        conversation = chat_service.create_conversation(user_id=user_id, system_prompt=system_prompt)
        return ConversationCreated(conversation_id=conversation.conversation_id)

    @router.post("/conversations/{conversation_id}/chat/", response_model=LLMResponse)
    def chat(conversation_id: str, request: ChatRequest) -> LLMResponse:
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
            return chat_service.chat(user_id=user_id, conversation_id=conversation_id, question=question)
        except ConversationNotFoundError as exc:
            raise HTTPException(status_code=404, detail="conversation not found") from exc

    @router.put("/conversations/{conversation_id}/system-prompt/")
    def update_system_prompt(conversation_id: str, request: UpdateSystemPromptRequest) -> SystemPromptUpdated:
        user_id = request.user_id.strip()
        conversation_id = conversation_id.strip()
        prompt = request.system_prompt.strip()
        if not user_id:
            raise HTTPException(status_code=422, detail="user_id must not be blank")
        if not conversation_id:
            raise HTTPException(status_code=422, detail="conversation_id must not be blank")
        if not prompt:
            raise HTTPException(status_code=422, detail="prompt must not be blank")
        try:
            chat_service.update_system_prompt(user_id=user_id, conversation_id=conversation_id, system_prompt=prompt)
        except ConversationNotFoundError as exc:
            raise HTTPException(status_code=404, detail="conversation not found") from exc
        return SystemPromptUpdated(updated=True)

    return router
```

### 3. chat_service.py（业务层）
```python
from typing import Callable
from app.concurrency.conversation_locks import ConversationLockRegistry
from app.schemas.chat import LLMResponse
from app.sessions.base import Conversation, SessionStore

class ChatService:
    def __init__(self, *, store: SessionStore, run_agent: Callable[..., LLMResponse], locks: ConversationLockRegistry):
        self._store = store
        self._run_agent = run_agent
        self._locks = locks

    def create_conversation(self, *, user_id: str, system_prompt: str | None = None) -> Conversation:
        return self._store.create_conversation(user_id=user_id, system_prompt=system_prompt)

    def update_system_prompt(self, *, user_id: str, conversation_id: str, system_prompt: str) -> None:
        with self._locks.acquire(conversation_id=conversation_id):
            self._store.update_system_prompt(user_id=user_id, conversation_id=conversation_id, system_prompt=system_prompt)

    def chat(self, *, user_id: str, conversation_id: str, question: str) -> LLMResponse:
        with self._locks.acquire(conversation_id=conversation_id):
            conversation = self._store.get_conversation(user_id=user_id, conversation_id=conversation_id)
            history = self._store.load_messages(conversation_id=conversation.conversation_id, user_id=user_id)
            messages: list[dict] = []
            if conversation.system_prompt:
                messages.append({"role": "system", "content": conversation.system_prompt})
            messages.extend(history)
            new_messages_start = len(messages)
            if question.strip():
                messages.append({"role": "user", "content": question})
            response = self._run_agent(messages=messages)
            self._store.append_messages(
                conversation_id=conversation_id, user_id=user_id,
                messages=messages[new_messages_start:]
            )
            return response
```

### 4. conversation_locks.py（锁机制）
```python
from contextlib import contextmanager
from threading import Lock
from typing import Iterator

class ConversationLockRegistry:
    def __init__(self):
        self._registry_guard = Lock()
        self._locks: dict[str, Lock] = {}

    @contextmanager
    def acquire(self, conversation_id: str) -> Iterator[None]:
        with self._registry_guard:
            conversation_lock = self._locks.setdefault(conversation_id, Lock())
        with conversation_lock:
            yield
```

### 5. sqlite_store.py（持久化层——核心部分）
```python
import sqlite3, json
from pathlib import Path
from uuid import uuid4
from contextlib import contextmanager
from typing import Iterator
from app.sessions.base import Conversation, ConversationNotFoundError

class SQLiteSessionStore:
    def __init__(self, database_path: str | Path):
        self._database_path = database_path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(database=self._database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def create_conversation(self, *, user_id: str, system_prompt: str | None = None) -> Conversation:
        conversation = Conversation(conversation_id=str(uuid4()), user_id=user_id, system_prompt=system_prompt)
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO conversations (id, user_id, system_prompt) VALUES (?, ?, ?)",
                (conversation.conversation_id, conversation.user_id, conversation.system_prompt)
            )
        return conversation

    def get_conversation(self, *, user_id: str, conversation_id: str) -> Conversation:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT id, user_id, system_prompt FROM conversations WHERE id=? AND user_id=?",
                (conversation_id, user_id)
            ).fetchone()
            if row is None:
                raise ConversationNotFoundError("conversation not found")
        return Conversation(conversation_id=row["id"], user_id=row["user_id"], system_prompt=row["system_prompt"])

    def load_messages(self, *, user_id: str, conversation_id: str) -> list[dict]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT id FROM conversations WHERE id=? AND user_id=?", (conversation_id, user_id)
            ).fetchone()
            if row is None:
                raise ConversationNotFoundError("conversation not found")
            rows = connection.execute(
                "SELECT payload_json FROM messages WHERE conversation_id = ? ORDER BY seq ASC", (conversation_id,)
            ).fetchall()
        return [json.loads(r["payload_json"]) for r in rows]

    def append_messages(self, *, user_id: str, conversation_id: str, messages: list[dict]) -> None:
        if not messages:
            return
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT id FROM conversations WHERE id=? AND user_id=?", (conversation_id, user_id)
            ).fetchone()
            if row is None:
                raise ConversationNotFoundError("conversation not found")
            last_seq = connection.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM messages WHERE conversation_id = ?", (conversation_id,)
            ).fetchone()[0]
            rows = [
                (conversation_id, last_seq + i, str(m["role"]), json.dumps(m, ensure_ascii=False))
                for i, m in enumerate(messages, start=1)
            ]
            connection.executemany(
                "INSERT INTO messages(conversation_id, seq, role, payload_json) VALUES (?, ?, ?, ?)", rows
            )
```

### 6. runner.py（Agent 编排——核心部分）
```python
import json, time
from typing import Any, Callable
from app.agent.events import AgentEvent
from app.schemas.chat import LLMResponse

def run_one_turn(
    *, messages: list[dict], client: Any, tool_definitions: list[dict],
    tool_functions: dict[str, Callable[..., Any]],
    on_event: Callable[[AgentEvent], None] | None = None
) -> LLMResponse:
    events: list[AgentEvent] = []

    def emit(event: AgentEvent):
        events.append(event)
        if on_event is not None:
            on_event(event)

    while True:
        response = client.chat.completions.create(
            model="deepseek-v4-flash", messages=messages, tools=tool_definitions,
            extra_body={"thinking": {"type": "enabled"}}
        )
        llm_res = response.choices[0].message.content
        reason_content = response.choices[0].message.reasoning_content

        if not response.choices[0].message.tool_calls:
            messages.append({"role": "assistant", "content": llm_res, "reasoning_content": reason_content})
            return LLMResponse(llm_answer=llm_res, llm_reasoning_content=reason_content, events=events)

        messages.append({
            "role": "assistant", "content": llm_res, "reasoning_content": reason_content,
            "tool_calls": [{
                "id": tc.id, "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments}
            } for tc in response.choices[0].message.tool_calls]
        })

        for tool_call in response.choices[0].message.tool_calls:
            func_name = tool_call.function.name
            try:
                func_arguments = json.loads(tool_call.function.arguments or "{}")
                if not isinstance(func_arguments, dict):
                    raise ValueError("arguments must be a JSON object")
            except (json.JSONDecodeError, TypeError, ValueError):
                emit(AgentEvent(type="tool_call.failed", tool_call_id=tool_call.id, tool_call_name=func_name, error="参数解析失败"))
                messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": json.dumps({"OK": False, "error": "参数解析失败"})})
                continue

            emit(AgentEvent(type="tool_call.requested", tool_call_id=tool_call.id, tool_call_name=func_name, tool_call_arguments=func_arguments))
            func = tool_functions.get(func_name)
            if func is None:
                emit(AgentEvent(type="tool_call.failed", tool_call_id=tool_call.id, tool_call_name=func_name, error="未注册的工具"))
                messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": json.dumps({"OK": False, "error": "未注册的工具"})})
                continue

            emit(AgentEvent(type="tool_call.started", tool_call_id=tool_call.id, tool_call_name=func_name, tool_call_arguments=func_arguments))
            start_at = time.perf_counter()
            try:
                func_result = func(**func_arguments)
                duration_ms = (time.perf_counter() - start_at) * 1000
            except Exception as exc:
                duration_ms = (time.perf_counter() - start_at) * 1000
                emit(AgentEvent(type="tool_call.failed", tool_call_id=tool_call.id, tool_call_name=func_name, error=f"{type(exc).__name__}:{exc}", duration_ms=duration_ms))
                messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": json.dumps({"OK": False, "error": f"{type(exc).__name__}:{exc}"})})
                continue

            emit(AgentEvent(type="tool_call.completed", tool_call_id=tool_call.id, tool_call_name=func_name, tool_call_arguments=func_arguments, result=func_result, duration_ms=duration_ms))
            messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": func_result if isinstance(func_result, str) else json.dumps(func_result, ensure_ascii=False, default=str)})
```

### 7. base.py（SessionStore 接口）
```python
from dataclasses import dataclass
from typing import Protocol

@dataclass(frozen=True)
class Conversation:
    conversation_id: str
    user_id: str
    system_prompt: str | None = None

class ConversationNotFoundError(LookupError):
    pass

class SessionStore(Protocol):
    def create_conversation(self, *, user_id: str, system_prompt: str | None = None) -> Conversation: ...
    def get_conversation(self, *, user_id: str, conversation_id: str) -> Conversation: ...
    def update_system_prompt(self, *, user_id: str, conversation_id: str, system_prompt: str) -> None: ...
    def load_messages(self, *, user_id: str, conversation_id: str) -> list[dict]: ...
    def append_messages(self, *, user_id: str, conversation_id: str, messages: list[dict]) -> None: ...
```

## 三、请你输出以下内容

### A. 逐文件改造对照表
用表格列出每个文件需要改什么，格式：
| 文件 | 当前（同步） | 改为（异步） | 风险等级 | 注意事项 |

### B. 需要替换的第三方库
列出同步库 → 异步库的对应关系，以及 pip install 命令。
例如：sqlite3 → aiosqlite，threading.Lock → asyncio.Lock（标准库，无需安装），openai 同步客户端 → 是否需要换成异步客户端。

### C. 最容易踩的 5 个坑
用具体代码示例说明（从我的项目里取代码片段），标注"错误写法 vs 正确写法"。

### D. 改造顺序建议
哪些文件应该先改，哪些后改，给出顺序和理由。

### E. 改造后的 router.py 和 chat_service.py 完整代码
给出这两个核心文件改造后的完整异步版本，让我看到最终成品应该长什么样。

要求：
- 所有输出用中文
- 代码注释用中文
- 不要用抽象的理论，全部针对我上面贴的具体代码来分析
- 假设我是初学者，解释每个改动"为什么"
