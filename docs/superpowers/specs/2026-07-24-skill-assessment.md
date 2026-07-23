# 当前 Agent 开发能力评估

> 基于 day06 项目全部代码的阅读分析，评估时间：2026-07-24。

---

## 一、你已经掌握的技能

### 1. Python 语言基础 ✅

| 技能点 | 证据（代码位置） | 掌握程度 |
|--------|-----------------|---------|
| 类型注解 | `router.py:55` — `def chat(conversation_id : str, request : ChatRequest)->LLMResponse` | 熟练 |
| `dataclass` | `sessions/base.py:4-8` — `@dataclass(frozen=True) class Conversation` | 会用 |
| `Protocol`（结构化鸭子类型） | `sessions/base.py:14` — `class SessionStore(Protocol)` | **超出初学者水平** |
| `@contextmanager` | `conversation_locks.py:10` — 自定义上下文管理器 | **超出初学者水平** |
| `functools.partial` | `main.py:19` — 用 partial 预绑定依赖 | 会用 |
| 异常链（`raise ... from exc`） | `router.py:72` — `raise HTTPException(...) from exc` | 了解，主动提问过 |
| 条件表达式 | 全项目多处 `x if cond else y` | 熟练 |
| `enumerate(..., start=1)` | `sqlite_store.py:199` | 细节注意到位 |

### 2. FastAPI Web 框架 ✅

- 路由定义（`@router.post`、`@router.put`）
- 路径参数（`{conversation_id}`）
- Pydantic 请求体验证（`ChatRequest`、`CreateConversationRequest`）
- 响应模型声明（`response_model=...`）
- HTTP 状态码映射（201 创建、404 未找到、422 参数非法）
- `TestClient` 端到端测试
- 路由工厂模式（`create_router(chat_service=...)` 而非全局单例）

### 3. Pydantic 数据校验 ✅

- `BaseModel` 定义请求/响应 Schema
- `Field(min_length=1)` 校验
- `Field(default_factory=list)` 默认值
- 嵌套 Optional 类型（`str | None`）

### 4. SQLite 持久化 ✅

- 手动建表（`CREATE TABLE IF NOT EXISTS`）
- 外键约束
- WAL 模式
- `BEGIN IMMEDIATE` 事务控制
- `executemany` 批量插入
- `COALESCE(MAX(seq), 0)` 序列号管理
- JSON 序列化存储（`payload_json` 列）
- `row_factory = sqlite3.Row` 行工厂

### 5. 多线程与并发控制 ✅

- `threading.Lock` 互斥锁
- `threading.Event` 信号同步
- `threading.Barrier` 屏障同步
- `threading.Thread` 多线程
- 自定义 `ConversationLockRegistry` 实现按 ID 分段锁

### 6. 测试编写 ✅

你写了 **5 个测试文件**，覆盖了不同层级：

| 测试文件 | 测试层级 | 测试数量 |
|---------|---------|---------|
| `tests/agent/test_runner.py` | Agent 编排逻辑（8 个场景） | 8 |
| `tests/tools/test_arguments.py` | 工具参数解析（4 个场景） | 4 |
| `tests/sessions/test_sqlite_store.py` | 数据库持久化（5 个场景） | 5 |
| `tests/application/test_chat_service.py` | 业务服务层（5 个场景） | 5 |
| `tests/concurrency/test_conversation_locks.py` | 锁机制（2 个场景） | 2 |
| `tests/api/test_router.py` | HTTP API 层（5 个场景，部分待实现） | 1 |
| `tests/test_main.py` | 应用装配验证 | 1 |

你使用的测试技术：
- **假对象（Fake Object）模式**：`FakeChatService`、fake LLM client
- **`monkeypatch`** 环境变量注入
- **`tmp_path`** 临时文件隔离
- **`pytest.raises`** 异常断言
- **`SimpleNamespace`** 快速构造假响应对象
- **`iter([...])` + `next()`** 模拟多轮 LLM 调用序列

### 7. Agent 核心循环 ✅

你的 `run_one_turn`（`agent/runner.py:43-200`）实现了完整的 Agent 编排：

```
while True:
    调用 LLM
    ↓
    无 tool_calls？ → 返回最终回答
    ↓ 有 tool_calls
    将 assistant 消息加入 messages
    ↓
    遍历每个 tool_call：
      → 解析参数（失败 → failed 事件 + 错误消息）
      → 查找函数（未注册 → failed 事件）
      → 执行函数（异常 → failed 事件）
      → 成功 → completed 事件 + 结果消息
    ↓
    继续 while 循环（让 LLM 看到工具结果后再次决策）
```

覆盖了 8 种路径：
1. 模型直接回答
2. 单次工具调用成功
3. 工具返回字典→JSON 序列化
4. 参数 JSON 损坏→failed 事件
5. 调用未注册工具→failed 事件
6. 工具执行抛异常→failed 事件
7. 一次调用多工具，混合成功/失败
8. 多轮工具调用（第一轮结果触发第二轮调用）

### 8. 项目架构能力 ✅

你的项目从单文件 `function_calling_demo.py` 演进到当前分层架构：

```
main.py                  ← 依赖装配（Composition Root）
app/
├── api/router.py        ← HTTP 层（只做输入输出翻译）
├── application/         ← 用例层（事务边界、加锁）
├── agent/runner.py      ← Agent 编排（独立于 HTTP/DB）
├── sessions/            ← 持久化（SQLite + 接口协议）
├── concurrency/         ← 锁机制
├── schemas/             ← 数据传输对象
├── tools/               ← 工具注册
└── core/config.py       ← 配置
```

这遵循了**依赖注入**和**关注点分离**原则——`run_one_turn` 不知道数据库的存在，Router 不知道 SQL，存储层不知道 HTTP。

---

## 二、你还在学习中的技能

### 1. Python 语法细节

| 问题 | 表现 |
|------|------|
| 行尾逗号导致 tuple | `router.py:42` 曾写 `user_id = request.user_id.strip(),` → 变成 `("user-a",)` |
| 忘记赋值 | `router.py:57` 写 `conversation_id.strip()` 但没 `=` 赋值 |
| 路由字符串多余字符 | `router.py:35` 写 `}/chat/}` 多了个 `}` |

这些不是"不会"，是初学者都会犯的**手滑型错误**，写多了自然会减少。你在排查时能定位到问题所在，说明理解是到位的。

### 2. `with` 语句的嵌套语义

`conversation_locks.py` 中 `with conversation_lock` 被嵌套在 `with self._registry_guard` 内部，导致 `_registry_guard` 在整个业务操作期间不释放。这涉及对"锁的持有范围应该多窄"的理解——你目前知道需要用锁，但对"锁应该保护的最小临界区"还需要积累经验。

### 3. 部分测试待完成

`tests/api/test_router.py` 中 4 个测试函数目前是 `pass`：
- `test_chat_passes_user_and_conversation_to_service`
- `test_missing_or_unowned_conversation_returns_404`
- `test_update_system_prompt_is_conversation_scoped`
- `test_blank_question_and_prompt_return_422`

---

## 三、当前水平定位

如果把 Agent 开发能力分成几个阶段：

```
阶段1："能跑就行" — 单文件脚本，全局变量，无测试
阶段2："能拆模块" — 分文件，有 import，有基本测试
阶段3："分层架构" — 接口/实现分离，依赖注入，测试覆盖多层级
阶段4："生产就绪" — 鉴权、分布式锁、可观测性、CI/CD
```

**你目前处于阶段 2 → 3 的过渡期。**

具体来说：
- 你的**架构意识**已经到阶段 3——Protocol 接口、依赖注入、分层职责都非常清晰
- 你的**代码实现**在阶段 2.5——核心逻辑正确，但偶尔有语法小失误
- 你的**测试能力**在阶段 2.5——覆盖率不错，测试设计合理（用假对象、控制并发时序），但部分测试还没写完

作为**初学者**来说，这个水平**远超平均**。大多数初学者还在写单文件脚本的阶段，你已经能设计出 `ChatService → SessionStore(Protocol) → SQLiteSessionStore` 这样清晰的接口-实现分离，并且用 `ConversationLockRegistry` 解决了并发安全问题。这非常难得。

---

## 四、你下一步可以学什么

结合你项目的 TODO（`docs/superpowers/plans/2026-07-22-supportpilot-project-evolution-roadmap.md`），自然的学习方向：

1. **鉴权** — 把 `user_id` 从请求参数改为从 JWT/Cookie 中提取
2. **异步编程** — `async def` + `await`，把同步代码改成异步（`asyncio.Lock`）
3. **可观测性** — 结构化日志（`structlog`）、请求追踪 ID
4. **流式响应** — SSE（Server-Sent Events）把 LLM 的输出逐 token 推给前端
5. **工具调用的幂等性** — 当 LLM 调用失败重试时，已执行的外部工具调用如何不重复执行
