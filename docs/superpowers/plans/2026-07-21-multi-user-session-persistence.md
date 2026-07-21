# Multi-User Session Persistence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 删除全局 `messages`，使用 SQLite 持久化按 `user_id + conversation_id` 隔离的会话，并保证同一会话的并发请求不会互相覆盖。

**Architecture:** Router 只处理 HTTP 输入输出；`ChatService` 负责所有权校验、会话级加锁、组装局部消息副本、调用 Agent 和成功后的原子提交；`SQLiteSessionStore` 负责会话与消息持久化。`run_one_turn()` 保持现有接口和行为，只允许它修改一次请求内的局部 `messages`。

**Tech Stack:** Python 3.10.10、FastAPI 0.115.0、Pydantic 2.11.5、标准库 `sqlite3`、pytest、SQLite WAL。

## Global Constraints

- 不新增 ORM、Redis、PostgreSQL 或认证依赖；本阶段只使用标准库 `sqlite3`。
- 测试不得访问真实 DeepSeek API；统一使用 fake runner/client。
- HTTP 请求中的 `user_id` 仅用于本阶段演示；生产环境必须改为从登录凭证中取得。
- `conversation_id` 必须由服务端使用 UUID4 生成，客户端不能指定。
- 每次读取、写入或修改会话都必须同时校验 `user_id` 和 `conversation_id`；错误用户与不存在会话统一表现为 404。
- 同一会话请求串行执行，不同会话可并行；该锁只保证单进程安全，因此本阶段只运行一个 Uvicorn worker。
- 不在等待 LLM 返回期间持有 SQLite 事务；数据库事务只包围短暂的读取或写入。
- 一轮 Agent 运行成功后才保存本轮 `user/assistant/tool` 消息；模型调用抛异常时，本轮局部消息全部丢弃。
- 上述回滚只覆盖聊天记录，不可能撤销已经产生外部副作用的工具调用。
- 完整保存 Agent 消息字典，确保 `tool_calls`、`tool_call_id`、`reasoning_content` 等字段能够无损恢复。
- 不删除现有 `chat_history.json`；切换后停止读取它，保留给用户手工核对或迁移。
- 每个 Task 完成后只提交该 Task 涉及的文件；开始下一个 Task 前必须保持全部测试通过。

---

## 最终文件结构

```text
app/
├── application/
│   ├── __init__.py
│   └── chat_service.py          # 一轮聊天用例和持久化边界
├── concurrency/
│   ├── __init__.py
│   └── conversation_locks.py    # 单进程、按 conversation_id 加锁
├── sessions/
│   ├── __init__.py
│   ├── base.py                  # Conversation、异常和 SessionStore 接口
│   ├── legacy_file.py           # 暂时保留但不再被 main.py 使用
│   └── sqlite_store.py          # SQLite 持久化实现
├── api/router.py
├── schemas/chat.py
└── ...
tests/
├── application/test_chat_service.py
├── concurrency/test_conversation_locks.py
├── sessions/test_sqlite_store.py
├── api/test_router.py
└── test_main.py
main.py
```

## Task 1: 建立 SQLite 会话模型和所有权边界

**Files:**
- Create: `app/sessions/base.py`
- Create: `app/sessions/sqlite_store.py`
- Create: `tests/sessions/__init__.py`
- Create: `tests/sessions/test_sqlite_store.py`

**Interfaces:**
- Produces: `Conversation`、`ConversationNotFoundError`、`SessionStore`、`SQLiteSessionStore.create_conversation()`、`get_conversation()`、`update_system_prompt()`。
- Ownership rule: 查询条件必须同时包含 `conversation_id` 和 `user_id`。

- [ ] **Step 1: 写会话持久化和所有权测试**

创建 `tests/sessions/test_sqlite_store.py`：

```python
import pytest

from app.sessions.base import ConversationNotFoundError
from app.sessions.sqlite_store import SQLiteSessionStore


def test_conversation_survives_store_recreation(tmp_path):
    database_path = tmp_path / "chat.db"
    first_store = SQLiteSessionStore(database_path)

    created = first_store.create_conversation(
        user_id="user-a",
        system_prompt="你是助手 A",
    )

    second_store = SQLiteSessionStore(database_path)
    loaded = second_store.get_conversation(
        user_id="user-a",
        conversation_id=created.conversation_id,
    )

    assert loaded == created


def test_get_conversation_hides_wrong_owner(tmp_path):
    store = SQLiteSessionStore(tmp_path / "chat.db")
    created = store.create_conversation(user_id="user-a")

    with pytest.raises(ConversationNotFoundError):
        store.get_conversation(
            user_id="user-b",
            conversation_id=created.conversation_id,
        )


def test_update_system_prompt_is_persistent(tmp_path):
    database_path = tmp_path / "chat.db"
    store = SQLiteSessionStore(database_path)
    created = store.create_conversation(user_id="user-a")

    store.update_system_prompt(
        user_id="user-a",
        conversation_id=created.conversation_id,
        system_prompt="新的系统提示词",
    )

    reopened = SQLiteSessionStore(database_path)
    loaded = reopened.get_conversation(
        user_id="user-a",
        conversation_id=created.conversation_id,
    )
    assert loaded.system_prompt == "新的系统提示词"
```

- [ ] **Step 2: 运行测试并确认按预期失败**

Run:

```powershell
$env:DEEPSEEK_API_KEY='test-only-key'
python -B -m pytest -p no:cacheprovider tests/sessions/test_sqlite_store.py -q
```

Expected: collection 阶段 FAIL，提示 `app.sessions.base` 或 `app.sessions.sqlite_store` 不存在。

- [ ] **Step 3: 定义会话对象、异常和存储接口**

创建 `app/sessions/base.py`：

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
    def create_conversation(
        self,
        *,
        user_id: str,
        system_prompt: str | None = None,
    ) -> Conversation:
        raise NotImplementedError

    def get_conversation(
        self,
        *,
        user_id: str,
        conversation_id: str,
    ) -> Conversation:
        raise NotImplementedError

    def update_system_prompt(
        self,
        *,
        user_id: str,
        conversation_id: str,
        system_prompt: str,
    ) -> None:
        raise NotImplementedError

    def load_messages(
        self,
        *,
        user_id: str,
        conversation_id: str,
    ) -> list[dict]:
        raise NotImplementedError

    def append_messages(
        self,
        *,
        user_id: str,
        conversation_id: str,
        messages: list[dict],
    ) -> None:
        raise NotImplementedError
```

- [ ] **Step 4: 创建 SQLite schema 和会话方法**

创建 `app/sessions/sqlite_store.py`，包括以下实现：

```python
import sqlite3
from pathlib import Path
from uuid import uuid4

from app.sessions.base import Conversation, ConversationNotFoundError


class SQLiteSessionStore:
    def __init__(self, database_path: str | Path):
        self._database_path = str(database_path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    system_prompt TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_conversations_user
                ON conversations(user_id, updated_at);

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(conversation_id) REFERENCES conversations(id),
                    UNIQUE(conversation_id, seq)
                );

                CREATE INDEX IF NOT EXISTS idx_messages_conversation_seq
                ON messages(conversation_id, seq);
                """
            )

    @staticmethod
    def _to_conversation(row: sqlite3.Row) -> Conversation:
        return Conversation(
            conversation_id=row["id"],
            user_id=row["user_id"],
            system_prompt=row["system_prompt"],
        )

    @staticmethod
    def _get_owned_row(
        connection: sqlite3.Connection,
        *,
        user_id: str,
        conversation_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT id, user_id, system_prompt
            FROM conversations
            WHERE id = ? AND user_id = ?
            """,
            (conversation_id, user_id),
        ).fetchone()
        if row is None:
            raise ConversationNotFoundError("conversation not found")
        return row

    def create_conversation(
        self,
        *,
        user_id: str,
        system_prompt: str | None = None,
    ) -> Conversation:
        conversation = Conversation(
            conversation_id=str(uuid4()),
            user_id=user_id,
            system_prompt=system_prompt,
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO conversations(id, user_id, system_prompt)
                VALUES (?, ?, ?)
                """,
                (
                    conversation.conversation_id,
                    conversation.user_id,
                    conversation.system_prompt,
                ),
            )
        return conversation

    def get_conversation(
        self,
        *,
        user_id: str,
        conversation_id: str,
    ) -> Conversation:
        with self._connect() as connection:
            row = self._get_owned_row(
                connection,
                user_id=user_id,
                conversation_id=conversation_id,
            )
        return self._to_conversation(row)

    def update_system_prompt(
        self,
        *,
        user_id: str,
        conversation_id: str,
        system_prompt: str,
    ) -> None:
        with self._connect() as connection:
            self._get_owned_row(
                connection,
                user_id=user_id,
                conversation_id=conversation_id,
            )
            connection.execute(
                """
                UPDATE conversations
                SET system_prompt = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND user_id = ?
                """,
                (system_prompt, conversation_id, user_id),
            )
```

- [ ] **Step 5: 运行 Task 1 测试和全部测试**

Run:

```powershell
$env:DEEPSEEK_API_KEY='test-only-key'
python -B -m pytest -p no:cacheprovider tests/sessions/test_sqlite_store.py -q
python -B -m pytest -p no:cacheprovider -q
```

Expected: Task 1 为 `3 passed`；全量为 `16 passed`。

- [ ] **Step 6: 提交 Task 1**

```powershell
git add app/sessions/base.py app/sessions/sqlite_store.py tests/sessions
git commit -m "feat: add persistent conversation store"
```

## Task 2: 按会话追加并无损恢复消息

**Files:**
- Modify: `app/sessions/sqlite_store.py`
- Modify: `tests/sessions/test_sqlite_store.py`

**Interfaces:**
- Consumes: Task 1 的 `SQLiteSessionStore` 和所有权规则。
- Produces: `load_messages(...)->list[dict]`、`append_messages(...)->None`；消息顺序由 `seq` 保证。

- [ ] **Step 1: 添加消息顺序、隔离和所有权测试**

向 `tests/sessions/test_sqlite_store.py` 追加：

```python
def test_messages_round_trip_in_order(tmp_path):
    store = SQLiteSessionStore(tmp_path / "chat.db")
    conversation = store.create_conversation(user_id="user-a")
    messages = [
        {"role": "user", "content": "调用工具"},
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "需要调用工具",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "get_food", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "result"},
    ]

    store.append_messages(
        user_id="user-a",
        conversation_id=conversation.conversation_id,
        messages=messages,
    )

    assert store.load_messages(
        user_id="user-a",
        conversation_id=conversation.conversation_id,
    ) == messages


def test_messages_are_isolated_by_conversation(tmp_path):
    store = SQLiteSessionStore(tmp_path / "chat.db")
    first = store.create_conversation(user_id="user-a")
    second = store.create_conversation(user_id="user-a")

    store.append_messages(
        user_id="user-a",
        conversation_id=first.conversation_id,
        messages=[{"role": "user", "content": "only first"}],
    )

    assert store.load_messages(
        user_id="user-a",
        conversation_id=first.conversation_id,
    ) == [{"role": "user", "content": "only first"}]
    assert store.load_messages(
        user_id="user-a",
        conversation_id=second.conversation_id,
    ) == []


def test_message_ownership_is_enforced(tmp_path):
    store = SQLiteSessionStore(tmp_path / "chat.db")
    conversation = store.create_conversation(user_id="user-a")

    with pytest.raises(ConversationNotFoundError):
        store.append_messages(
            user_id="user-b",
            conversation_id=conversation.conversation_id,
            messages=[{"role": "user", "content": "not allowed"}],
        )

    with pytest.raises(ConversationNotFoundError):
        store.load_messages(
            user_id="user-b",
            conversation_id=conversation.conversation_id,
        )
```

- [ ] **Step 2: 运行新增测试并确认失败**

Run: `python -B -m pytest -p no:cacheprovider tests/sessions/test_sqlite_store.py -q`

Expected: 前 3 个测试通过，新增 3 个测试因缺少 `append_messages`/`load_messages` 而 FAIL。

- [ ] **Step 3: 实现原子追加和有序读取**

在 `app/sessions/sqlite_store.py` 顶部增加：

```python
import json
```

在 `SQLiteSessionStore` 中增加：

```python
    def load_messages(
        self,
        *,
        user_id: str,
        conversation_id: str,
    ) -> list[dict]:
        with self._connect() as connection:
            self._get_owned_row(
                connection,
                user_id=user_id,
                conversation_id=conversation_id,
            )
            rows = connection.execute(
                """
                SELECT payload_json
                FROM messages
                WHERE conversation_id = ?
                ORDER BY seq ASC
                """,
                (conversation_id,),
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def append_messages(
        self,
        *,
        user_id: str,
        conversation_id: str,
        messages: list[dict],
    ) -> None:
        if not messages:
            return

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._get_owned_row(
                connection,
                user_id=user_id,
                conversation_id=conversation_id,
            )
            last_seq = connection.execute(
                """
                SELECT COALESCE(MAX(seq), 0)
                FROM messages
                WHERE conversation_id = ?
                """,
                (conversation_id,),
            ).fetchone()[0]

            rows = []
            for offset, message in enumerate(messages, start=1):
                rows.append(
                    (
                        conversation_id,
                        last_seq + offset,
                        str(message["role"]),
                        json.dumps(message, ensure_ascii=False),
                    )
                )

            connection.executemany(
                """
                INSERT INTO messages(conversation_id, seq, role, payload_json)
                VALUES (?, ?, ?, ?)
                """,
                rows,
            )
            connection.execute(
                """
                UPDATE conversations
                SET updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND user_id = ?
                """,
                (conversation_id, user_id),
            )
```

- [ ] **Step 4: 验证 Task 2**

Run:

```powershell
$env:DEEPSEEK_API_KEY='test-only-key'
python -B -m pytest -p no:cacheprovider tests/sessions/test_sqlite_store.py -q
python -B -m pytest -p no:cacheprovider -q
```

Expected: 存储测试 `6 passed`；全量 `19 passed`。

- [ ] **Step 5: 提交 Task 2**

```powershell
git add app/sessions/sqlite_store.py tests/sessions/test_sqlite_store.py
git commit -m "feat: persist isolated conversation messages"
```

## Task 3: 增加单进程会话级锁

**Files:**
- Create: `app/concurrency/__init__.py`
- Create: `app/concurrency/conversation_locks.py`
- Create: `tests/concurrency/__init__.py`
- Create: `tests/concurrency/test_conversation_locks.py`

**Interfaces:**
- Produces: `ConversationLockRegistry.acquire(conversation_id)` 上下文管理器。
- Guarantee: 相同 ID 互斥；异常退出后锁仍被释放。

- [ ] **Step 1: 写锁行为测试**

创建 `tests/concurrency/test_conversation_locks.py`：

```python
from threading import Event, Thread

import pytest

from app.concurrency.conversation_locks import ConversationLockRegistry


def test_same_conversation_is_serialized():
    registry = ConversationLockRegistry()
    first_entered = Event()
    release_first = Event()
    second_entered = Event()

    def first_request():
        with registry.acquire("conversation-1"):
            first_entered.set()
            assert release_first.wait(timeout=2)

    def second_request():
        assert first_entered.wait(timeout=2)
        with registry.acquire("conversation-1"):
            second_entered.set()

    first = Thread(target=first_request)
    second = Thread(target=second_request)
    first.start()
    second.start()

    assert first_entered.wait(timeout=2)
    assert not second_entered.wait(timeout=0.1)
    release_first.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert second_entered.is_set()


def test_lock_is_released_when_request_raises():
    registry = ConversationLockRegistry()

    with pytest.raises(RuntimeError):
        with registry.acquire("conversation-1"):
            raise RuntimeError("model failed")

    with registry.acquire("conversation-1"):
        acquired_again = True

    assert acquired_again is True
```

- [ ] **Step 2: 运行测试并确认失败**

Expected: collection FAIL，提示 `app.concurrency.conversation_locks` 不存在。

- [ ] **Step 3: 实现锁注册表**

创建 `app/concurrency/conversation_locks.py`：

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
            conversation_lock = self._locks.setdefault(
                conversation_id,
                Lock(),
            )

        with conversation_lock:
            yield
```

锁对象暂不回收，以避免“删除旧锁后，同一会话出现两把锁”的竞态；这是当前单进程 Demo 可接受的内存换正确性策略。

- [ ] **Step 4: 验证并提交 Task 3**

Run: `python -B -m pytest -p no:cacheprovider -q`

Expected: `21 passed`。

```powershell
git add app/concurrency tests/concurrency
git commit -m "feat: serialize requests per conversation"
```

## Task 4: 用 ChatService 建立一次聊天的事务边界

**Files:**
- Create: `app/application/__init__.py`
- Create: `app/application/chat_service.py`
- Create: `tests/application/__init__.py`
- Create: `tests/application/test_chat_service.py`

**Interfaces:**
- Consumes: `SessionStore`、`ConversationLockRegistry`、现有 `run_one_turn(messages=...)` 兼容 callable。
- Produces: `ChatService.create_conversation()`、`chat()`、`update_system_prompt()`。
- Persistence rule: 只保存 `new_messages_start` 之后的消息，不把动态拼接的 system prompt 重复存入消息表。

- [ ] **Step 1: 写成功、连续对话和失败回滚测试**

创建 `tests/application/test_chat_service.py`：

```python
import pytest

from app.application.chat_service import ChatService
from app.concurrency.conversation_locks import ConversationLockRegistry
from app.schemas.chat import LLMResponse
from app.sessions.sqlite_store import SQLiteSessionStore


def build_service(tmp_path, runner):
    store = SQLiteSessionStore(tmp_path / "chat.db")
    service = ChatService(
        store=store,
        run_agent=runner,
        locks=ConversationLockRegistry(),
    )
    return service, store


def direct_answer_runner(*, messages):
    messages.append({"role": "assistant", "content": "answer"})
    return LLMResponse(llm_answer="answer")


def test_chat_persists_only_current_conversation(tmp_path):
    service, store = build_service(tmp_path, direct_answer_runner)
    first = service.create_conversation(user_id="user-a")
    second = service.create_conversation(user_id="user-a")

    service.chat(
        user_id="user-a",
        conversation_id=first.conversation_id,
        question="hello",
    )

    assert store.load_messages(
        user_id="user-a",
        conversation_id=first.conversation_id,
    ) == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "answer"},
    ]
    assert store.load_messages(
        user_id="user-a",
        conversation_id=second.conversation_id,
    ) == []


def test_second_turn_receives_previous_history_and_system_prompt(tmp_path):
    received_messages = []

    def recording_runner(*, messages):
        received_messages.append([dict(message) for message in messages])
        messages.append({"role": "assistant", "content": "answer"})
        return LLMResponse(llm_answer="answer")

    service, _ = build_service(tmp_path, recording_runner)
    conversation = service.create_conversation(
        user_id="user-a",
        system_prompt="你是测试助手",
    )

    service.chat(
        user_id="user-a",
        conversation_id=conversation.conversation_id,
        question="first",
    )
    service.chat(
        user_id="user-a",
        conversation_id=conversation.conversation_id,
        question="second",
    )

    assert received_messages[1] == [
        {"role": "system", "content": "你是测试助手"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "second"},
    ]


def test_failed_runner_does_not_persist_partial_turn(tmp_path):
    def failing_runner(*, messages):
        messages.append({"role": "assistant", "content": "partial"})
        raise RuntimeError("model unavailable")

    service, store = build_service(tmp_path, failing_runner)
    conversation = service.create_conversation(user_id="user-a")

    with pytest.raises(RuntimeError, match="model unavailable"):
        service.chat(
            user_id="user-a",
            conversation_id=conversation.conversation_id,
            question="will fail",
        )

    assert store.load_messages(
        user_id="user-a",
        conversation_id=conversation.conversation_id,
    ) == []
```

- [ ] **Step 2: 运行测试并确认失败**

Expected: collection FAIL，提示 `app.application.chat_service` 不存在。

- [ ] **Step 3: 实现 ChatService**

创建 `app/application/chat_service.py`：

```python
from typing import Callable

from app.concurrency.conversation_locks import ConversationLockRegistry
from app.schemas.chat import LLMResponse
from app.sessions.base import Conversation, SessionStore


class ChatService:
    def __init__(
        self,
        *,
        store: SessionStore,
        run_agent: Callable[..., LLMResponse],
        locks: ConversationLockRegistry,
    ):
        self._store = store
        self._run_agent = run_agent
        self._locks = locks

    def create_conversation(
        self,
        *,
        user_id: str,
        system_prompt: str | None = None,
    ) -> Conversation:
        return self._store.create_conversation(
            user_id=user_id,
            system_prompt=system_prompt,
        )

    def update_system_prompt(
        self,
        *,
        user_id: str,
        conversation_id: str,
        system_prompt: str,
    ) -> None:
        with self._locks.acquire(conversation_id):
            self._store.update_system_prompt(
                user_id=user_id,
                conversation_id=conversation_id,
                system_prompt=system_prompt,
            )

    def chat(
        self,
        *,
        user_id: str,
        conversation_id: str,
        question: str,
    ) -> LLMResponse:
        with self._locks.acquire(conversation_id):
            conversation = self._store.get_conversation(
                user_id=user_id,
                conversation_id=conversation_id,
            )
            history = self._store.load_messages(
                user_id=user_id,
                conversation_id=conversation_id,
            )

            messages: list[dict] = []
            if conversation.system_prompt:
                messages.append(
                    {"role": "system", "content": conversation.system_prompt}
                )
            messages.extend(history)
            new_messages_start = len(messages)
            messages.append({"role": "user", "content": question})

            response = self._run_agent(messages=messages)

            self._store.append_messages(
                user_id=user_id,
                conversation_id=conversation_id,
                messages=messages[new_messages_start:],
            )
            return response
```

- [ ] **Step 4: 验证并提交 Task 4**

Run: `python -B -m pytest -p no:cacheprovider -q`

Expected: `24 passed`。

```powershell
git add app/application tests/application
git commit -m "feat: add transactional chat service"
```

## Task 5: 将 HTTP API 改为显式的会话资源

**Files:**
- Modify: `app/schemas/chat.py`
- Rewrite: `app/api/router.py`
- Rewrite: `tests/api/test_router.py`

**Interfaces:**
- Produces: `POST /conversations/`、`POST /conversations/{conversation_id}/chat/`、`PUT /conversations/{conversation_id}/system-prompt/`。
- Removes: 旧的全局 `/chat/` 和 `/setSys/` 行为。
- Error mapping: `ConversationNotFoundError -> HTTP 404`；全空白输入 -> HTTP 422。

- [ ] **Step 1: 扩展 HTTP Schema**

保留 `LLMResponse`，用以下模型替换旧的 `SystemPrompt` 和 `UserQuestion`：

```python
class CreateConversationRequest(BaseModel):
    user_id: str = Field(min_length=1)
    system_prompt: str | None = None


class ConversationCreated(BaseModel):
    conversation_id: str


class ChatRequest(BaseModel):
    user_id: str = Field(min_length=1)
    question: str = Field(min_length=1)


class UpdateSystemPromptRequest(BaseModel):
    user_id: str = Field(min_length=1)
    prompt: str = Field(min_length=1)


class SystemPromptUpdated(BaseModel):
    updated: bool
```

- [ ] **Step 2: 用 5 个 API 测试替换旧 smoke test**

`tests/api/test_router.py` 必须覆盖以下精确行为：

```python
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.router import create_router
from app.schemas.chat import LLMResponse
from app.sessions.base import Conversation, ConversationNotFoundError


class FakeChatService:
    def __init__(self):
        self.calls = []

    def create_conversation(self, *, user_id, system_prompt=None):
        self.calls.append(("create", user_id, system_prompt))
        return Conversation("conversation-1", user_id, system_prompt)

    def chat(self, *, user_id, conversation_id, question):
        self.calls.append(("chat", user_id, conversation_id, question))
        if conversation_id == "missing":
            raise ConversationNotFoundError("conversation not found")
        return LLMResponse(llm_answer="answer")

    def update_system_prompt(
        self, *, user_id, conversation_id, system_prompt
    ):
        self.calls.append(
            ("prompt", user_id, conversation_id, system_prompt)
        )


def build_client():
    service = FakeChatService()
    app = FastAPI()
    app.include_router(create_router(chat_service=service))
    return TestClient(app), service


def test_create_conversation_returns_server_id():
    client, service = build_client()
    response = client.post(
        "/conversations/",
        json={"user_id": "user-a", "system_prompt": "be helpful"},
    )
    assert response.status_code == 201
    assert response.json() == {"conversation_id": "conversation-1"}
    assert service.calls == [("create", "user-a", "be helpful")]


def test_chat_passes_user_and_conversation_to_service():
    client, service = build_client()
    response = client.post(
        "/conversations/conversation-1/chat/",
        json={"user_id": "user-a", "question": "hello"},
    )
    assert response.status_code == 200
    assert response.json()["llm_answer"] == "answer"
    assert service.calls == [
        ("chat", "user-a", "conversation-1", "hello")
    ]


def test_missing_or_unowned_conversation_returns_404():
    client, _ = build_client()
    response = client.post(
        "/conversations/missing/chat/",
        json={"user_id": "user-b", "question": "hello"},
    )
    assert response.status_code == 404
    assert response.json() == {"detail": "conversation not found"}


def test_update_system_prompt_is_conversation_scoped():
    client, service = build_client()
    response = client.put(
        "/conversations/conversation-1/system-prompt/",
        json={"user_id": "user-a", "prompt": "new prompt"},
    )
    assert response.status_code == 200
    assert response.json() == {"updated": True}
    assert service.calls == [
        ("prompt", "user-a", "conversation-1", "new prompt")
    ]


def test_blank_question_and_prompt_return_422():
    client, _ = build_client()
    chat_response = client.post(
        "/conversations/conversation-1/chat/",
        json={"user_id": "user-a", "question": "   "},
    )
    prompt_response = client.put(
        "/conversations/conversation-1/system-prompt/",
        json={"user_id": "user-a", "prompt": "   "},
    )
    assert chat_response.status_code == 422
    assert prompt_response.status_code == 422
```

- [ ] **Step 3: 运行 Router 测试并确认旧 Router 不满足新接口**

Run: `python -B -m pytest -p no:cacheprovider tests/api/test_router.py -q`

Expected: FAIL，原因包括缺少 `create_router(chat_service=...)` 和新路由。

- [ ] **Step 4: 重写 Router 工厂**

将 `app/api/router.py` 改为：

```python
from fastapi import APIRouter, HTTPException

from app.application.chat_service import ChatService
from app.schemas.chat import (
    ChatRequest,
    ConversationCreated,
    CreateConversationRequest,
    LLMResponse,
    SystemPromptUpdated,
    UpdateSystemPromptRequest,
)
from app.sessions.base import ConversationNotFoundError


def create_router(*, chat_service: ChatService) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/conversations/",
        response_model=ConversationCreated,
        status_code=201,
    )
    def create_conversation(
        request: CreateConversationRequest,
    ) -> ConversationCreated:
        user_id = request.user_id.strip()
        if not user_id:
            raise HTTPException(status_code=422, detail="user_id must not be blank")
        system_prompt = (
            request.system_prompt.strip() if request.system_prompt else None
        )
        conversation = chat_service.create_conversation(
            user_id=user_id,
            system_prompt=system_prompt,
        )
        return ConversationCreated(
            conversation_id=conversation.conversation_id
        )

    @router.post(
        "/conversations/{conversation_id}/chat/",
        response_model=LLMResponse,
    )
    def chat(conversation_id: str, request: ChatRequest) -> LLMResponse:
        user_id = request.user_id.strip()
        question = request.question.strip()
        if not user_id or not question:
            raise HTTPException(status_code=422, detail="input must not be blank")
        try:
            return chat_service.chat(
                user_id=user_id,
                conversation_id=conversation_id,
                question=question,
            )
        except ConversationNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail="conversation not found",
            ) from exc

    @router.put(
        "/conversations/{conversation_id}/system-prompt/",
        response_model=SystemPromptUpdated,
    )
    def update_system_prompt(
        conversation_id: str,
        request: UpdateSystemPromptRequest,
    ) -> SystemPromptUpdated:
        user_id = request.user_id.strip()
        prompt = request.prompt.strip()
        if not user_id or not prompt:
            raise HTTPException(status_code=422, detail="input must not be blank")
        try:
            chat_service.update_system_prompt(
                user_id=user_id,
                conversation_id=conversation_id,
                system_prompt=prompt,
            )
        except ConversationNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail="conversation not found",
            ) from exc
        return SystemPromptUpdated(updated=True)

    return router
```

- [ ] **Step 5: 验证并提交 Task 5**

Run:

```powershell
$env:DEEPSEEK_API_KEY='test-only-key'
python -B -m pytest -p no:cacheprovider tests/api/test_router.py -q
python -B -m pytest -p no:cacheprovider -q
```

Expected: Router `5 passed`；全量 `28 passed`。

```powershell
git add app/schemas/chat.py app/api/router.py tests/api/test_router.py
git commit -m "feat: expose conversation-scoped chat API"
```

## Task 6: 在 main.py 装配持久化服务并删除全局 messages

**Files:**
- Modify: `app/core/config.py`
- Rewrite: `main.py`
- Modify: `.gitignore`
- Create: `tests/test_main.py`

**Interfaces:**
- Produces: `get_chat_db_path()`；生产应用默认使用工作目录下的 `chat_history.db`。
- Removes: `main.messages`、`read_history_chat()`/`save_history_chat()` 注入和旧 Router 装配。

- [ ] **Step 1: 写应用装配测试**

创建 `tests/test_main.py`：

```python
import importlib
import sys


def test_main_wires_sqlite_service_without_global_messages(
    monkeypatch,
    tmp_path,
):
    database_path = tmp_path / "main-chat.db"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-key")
    monkeypatch.setenv("CHAT_DB_PATH", str(database_path))

    sys.modules.pop("main", None)
    main = importlib.import_module("main")

    paths = {route.path for route in main.app.routes}
    assert not hasattr(main, "messages")
    assert database_path.exists()
    assert "/conversations/" in paths
    assert "/conversations/{conversation_id}/chat/" in paths
```

- [ ] **Step 2: 运行测试并确认失败**

Expected: FAIL，因为当前 `main.py` 仍暴露全局 `messages`，也没有新路由和 SQLite 文件。

- [ ] **Step 3: 增加数据库路径配置**

在 `app/core/config.py` 中增加：

```python
def get_chat_db_path() -> str:
    return os.getenv("CHAT_DB_PATH", "chat_history.db")
```

- [ ] **Step 4: 重写 main.py 的依赖装配**

将 `main.py` 改为：

```python
from functools import partial

from fastapi import FastAPI

from app.agent.runner import run_one_turn
from app.api.router import create_router
from app.application.chat_service import ChatService
from app.concurrency.conversation_locks import ConversationLockRegistry
from app.core.config import create_llm_client, get_chat_db_path
from app.sessions.sqlite_store import SQLiteSessionStore
from app.tools.registry import TOOL_DEFINITIONS, TOOL_FUNCTIONS


app = FastAPI()
client = create_llm_client()

run_agent = partial(
    run_one_turn,
    client=client,
    tool_definitions=TOOL_DEFINITIONS,
    tool_functions=TOOL_FUNCTIONS,
    on_event=None,
)

session_store = SQLiteSessionStore(get_chat_db_path())
chat_service = ChatService(
    store=session_store,
    run_agent=run_agent,
    locks=ConversationLockRegistry(),
)

app.include_router(create_router(chat_service=chat_service))
```

- [ ] **Step 5: 忽略 SQLite 运行文件**

向 `.gitignore` 增加：

```gitignore
chat_history.db
chat_history.db-shm
chat_history.db-wal
```

不要删除或修改现有 `chat_history.json`。

- [ ] **Step 6: 验证并提交 Task 6**

Run:

```powershell
$env:DEEPSEEK_API_KEY='test-only-key'
python -B -m pytest -p no:cacheprovider tests/test_main.py -q
python -B -m pytest -p no:cacheprovider -q
```

Expected: main 测试 `1 passed`；全量 `29 passed`。

```powershell
git add .gitignore app/core/config.py main.py tests/test_main.py
git commit -m "feat: wire persistent multi-user sessions"
```

## Task 7: 验证同会话串行、跨会话并行

**Files:**
- Modify: `tests/application/test_chat_service.py`

**Interfaces:**
- Verifies: Task 3 的锁确实被 Task 4 使用，而不只是单独存在。
- Same conversation: 第二个 runner 调用必须等待第一个完成。
- Different conversations: 两个 runner 调用必须能同时进入。

- [ ] **Step 1: 添加 ChatService 并发集成测试**

向 `tests/application/test_chat_service.py` 增加 import：

```python
from threading import Barrier, Event, Lock, Thread
```

再追加：

```python
def test_same_conversation_requests_are_serialized(tmp_path):
    first_entered = Event()
    release_first = Event()
    second_entered = Event()
    call_guard = Lock()
    call_count = 0

    def controlled_runner(*, messages):
        nonlocal call_count
        with call_guard:
            call_count += 1
            current_call = call_count
        if current_call == 1:
            first_entered.set()
            assert release_first.wait(timeout=2)
        else:
            second_entered.set()
        messages.append({"role": "assistant", "content": "answer"})
        return LLMResponse(llm_answer="answer")

    service, _ = build_service(tmp_path, controlled_runner)
    conversation = service.create_conversation(user_id="user-a")

    def ask(question):
        service.chat(
            user_id="user-a",
            conversation_id=conversation.conversation_id,
            question=question,
        )

    first = Thread(target=ask, args=("first",))
    second = Thread(target=ask, args=("second",))
    first.start()
    assert first_entered.wait(timeout=2)
    second.start()
    assert not second_entered.wait(timeout=0.1)
    release_first.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert second_entered.is_set()


def test_different_conversations_can_run_in_parallel(tmp_path):
    both_runners_entered = Barrier(2)
    errors = []

    def synchronized_runner(*, messages):
        both_runners_entered.wait(timeout=2)
        messages.append({"role": "assistant", "content": "answer"})
        return LLMResponse(llm_answer="answer")

    service, _ = build_service(tmp_path, synchronized_runner)
    first = service.create_conversation(user_id="user-a")
    second = service.create_conversation(user_id="user-a")

    def ask(conversation_id):
        try:
            service.chat(
                user_id="user-a",
                conversation_id=conversation_id,
                question="hello",
            )
        except Exception as exc:
            errors.append(exc)

    threads = [
        Thread(target=ask, args=(first.conversation_id,)),
        Thread(target=ask, args=(second.conversation_id,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert errors == []
    assert all(not thread.is_alive() for thread in threads)
```

- [ ] **Step 2: 运行并发测试 10 次，排除偶然通过**

Run:

```powershell
1..10 | ForEach-Object {
  python -B -m pytest -p no:cacheprovider tests/application/test_chat_service.py -q
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
```

Expected: 每次均显示 `5 passed`，无死锁和超时。

- [ ] **Step 3: 运行全量测试并提交 Task 7**

Run: `python -B -m pytest -p no:cacheprovider -q`

Expected: `31 passed`。

```powershell
git add tests/application/test_chat_service.py
git commit -m "test: verify conversation concurrency boundaries"
```

## Task 8: 最终验收和手工 API 核对

**Files:**
- Review: `main.py`
- Review: `app/application/chat_service.py`
- Review: `app/sessions/sqlite_store.py`
- Review: `app/api/router.py`
- Review: `tests/**`

**Interfaces:**
- Produces: 可启动、可重启恢复、用户与会话均隔离的单进程 FastAPI 应用。

- [ ] **Step 1: 运行最终自动化测试**

```powershell
$env:DEEPSEEK_API_KEY='test-only-key'
python -B -m pytest -p no:cacheprovider -q
```

Expected: `31 passed`，测试期间不访问真实 DeepSeek。

- [ ] **Step 2: 检查全局消息已彻底移除**

```powershell
Get-ChildItem -Recurse -File -Include *.py |
  Select-String -Pattern '^messages\s*=\s*read_history_chat|messages=messages|save_history_chat'
```

Expected: 无匹配；`app/sessions/legacy_file.py` 中函数定义本身可以保留，但 `main.py` 和 Router 不得导入或调用它。

- [ ] **Step 3: 启动单 worker 服务进行手工核对**

```powershell
$env:CHAT_DB_PATH='chat_history.db'
if (-not $env:DEEPSEEK_API_KEY) {
  throw '请先在当前 PowerShell 会话中设置真实的 DEEPSEEK_API_KEY'
}
python -m uvicorn main:app --workers 1 --reload
```

在 Swagger `http://127.0.0.1:8000/docs` 中依次执行：

1. 用户 A 创建会话 A1。
2. 用户 A 创建会话 A2。
3. 用户 B 创建会话 B1。
4. 分别向 A1、A2、B1 发送不同问题，确认上下文不串话。
5. 用用户 B 的 `user_id` 请求 A1，确认返回 404。
6. 停止服务并重新启动，再向 A1 追问，确认历史仍能恢复。
7. 修改 A1 的 system prompt，确认 A2 与 B1 不受影响。

- [ ] **Step 4: 检查 SQLite 数据分布**

服务停止后运行：

```powershell
python -c "import sqlite3; c=sqlite3.connect('chat_history.db'); print(c.execute('select user_id, count(*) from conversations group by user_id').fetchall()); print(c.execute('select conversation_id, count(*) from messages group by conversation_id').fetchall())"
```

Expected: `conversations` 按用户分组，`messages` 按三个不同的 `conversation_id` 分组；数量与手工请求一致。

- [ ] **Step 5: 最终边界检查**

验收条件：

- `main.py` 只负责依赖创建和 FastAPI 装配，不存在全局 `messages`。
- Router 不读取数据库、不拼装 Agent 历史、不持有锁。
- ChatService 不执行 SQL，只通过 `SessionStore` 接口访问历史。
- SQLite store 不导入 FastAPI 或 Agent runner。
- `run_one_turn()` 不知道 `user_id`、`conversation_id` 或数据库。
- 每个存储方法都执行用户所有权校验。
- system prompt 属于会话配置，不作为重复历史消息写入。
- tool call 消息经过数据库往返后与原字典完全相等。
- 同会话请求串行，跨会话请求并行。
- `chat_history.json` 仍保留且不再被生产入口读取。

- [ ] **Step 6: 提交验收中产生的必要修正**

只有手工验收确实产生代码或测试修正时才执行：

```powershell
git add app main.py tests .gitignore
git commit -m "fix: complete multi-user session acceptance"
```

若没有文件变化，不创建空提交。

## 本阶段明确不做

- 登录、JWT、Cookie Session 或 API Key 鉴权。
- 多 Uvicorn worker、多进程锁或跨机器分布式锁。
- Redis 缓存与 PostgreSQL。
- 历史消息分页、标题生成、删除会话、会话列表。
- token 计数、上下文窗口裁剪和历史摘要。
- `chat_history.json` 自动迁移；旧文件缺少可靠的用户和会话归属，自动猜测会造成错误归档。
- Agent 工具外部副作用的幂等与补偿事务。

这些能力应在本计划全部验收通过后，以独立计划继续实现。
