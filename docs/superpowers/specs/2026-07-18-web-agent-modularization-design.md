# Web Agent 模块化与多会话设计

## 目标

把当前单文件 FastAPI Agent Demo 逐步演进为模块化单体，并为多用户同时访问建立清晰边界。第一阶段只拆职责并保持行为不变；第二阶段取消全局 `messages`，加入按 `conversation_id` 隔离的内存会话存储。

## 架构边界

- `main.py`：创建 FastAPI、创建依赖、挂载 Router，不包含 Agent 循环、具体工具或历史存储实现。
- `schemas/chat.py`：HTTP 请求与响应的 Pydantic Schema。
- `api/router.py`：接收 HTTP 参数、调用应用层、把应用异常翻译成 HTTP 状态码。
- `application/chat_service.py`：一次聊天用例，负责加锁、读取历史副本、调用 Agent、成功后保存。
- `agent/runner.py`：模型请求、工具调用、工具结果回传和事件流。
- `agent/events.py`：AgentEvent。
- `tools/builtin.py`：具体工具函数。
- `tools/registry.py`：工具函数注册表和发给模型的 JSON Schema。
- `sessions/base.py`：SessionStore 接口。
- `sessions/memory.py`：按 conversation_id 隔离的单进程内存实现。
- `core/config.py`：环境变量、模型名和 DeepSeek 客户端创建。

## 多会话数据流

1. 服务端创建不可预测的 `conversation_id`。
2. ChatService 获取该 conversation_id 的独立锁。
3. SessionStore 返回 messages 副本，不暴露共享列表。
4. ChatService 把副本交给 AgentRunner。
5. Agent 成功后整体保存副本；异常时丢弃副本，原历史不变。
6. 同一会话请求串行执行，不同会话可以并行。

## 错误边界

- 工具参数损坏、工具未注册和工具运行异常：转换为 failed 事件及 tool 错误消息，交给模型继续处理。
- 模型 API 异常：AgentRunner 向上抛出，ChatService 不提交局部历史。
- 会话不存在：应用层抛出明确异常，Router 转换为 404。
- HTTP 参数错误：由 FastAPI/Pydantic 或 Router 处理。
- 锁必须通过上下文管理器释放。

## 第一阶段限制

- 内存历史在进程重启后丢失。
- 只运行一个 Uvicorn worker。
- conversation_id 只提供会话隔离，不代表用户身份认证。
- 登录鉴权、Redis/数据库、跨进程锁、工具幂等、RAG 和 MCP 均不在第一轮实现范围。

## 测试策略

- AgentRunner 测试继续使用假 LLM 客户端，不访问 DeepSeek。
- 工具参数解析测试跟随 AgentRunner 或工具执行模块迁移。
- SessionStore 测试覆盖会话隔离、返回副本、保存替换和锁。
- ChatService 测试使用假 Runner + 真 InMemorySessionStore，覆盖成功提交和失败回滚。
- Router 测试只验证 HTTP 输入输出和状态码。

## 实施分期

### 阶段一：行为不变的模块拆分

移动 Schema、事件、工具、AgentRunner、历史读写和 Router；每一步只修 import 与依赖传递，现有测试始终为绿。

### 阶段二：多会话应用层

新增 SessionStore、InMemorySessionStore、ChatService 和 conversation API，删除全局 messages 与单一 chat_history.json 行为。

