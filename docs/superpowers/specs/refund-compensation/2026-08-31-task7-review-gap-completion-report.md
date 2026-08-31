# Task 7 代码审查遗漏补齐报告

## 1. 报告信息

- 日期：2026-08-31
- 对应设计：`2026-08-28-refund-compensation-approval-durable-execution-design.md`
- 对应任务：Task 7——Agent 提案工具集成
- Task 7 基线提交：`3966b4b 实现Agent提案工具集成与待审批响应（Task 7）`
- 本次范围：补齐 Task 7 审查发现的可靠响应、状态解释、错误脱敏和仓库规范遗漏

## 2. 审查发现的问题

### 2.1 高：工作流首次启动失败被包装成普通工具成功

`ActionWorkflowService.create_proposal()` 已经通过 `start_ok` 和
`start_error_code` 表达“提案事实已提交，但工作流首次启动失败”。原
`ActionToolGateway` 只返回 Run 状态和提案标识，丢弃了这两个字段。

当运行器或 checkpoint 不可用时，实际结果是 Run 保持 `queued`，但工具仍返回
`ok=true`。Agent Runtime 又会把该结果加入 `pending_approvals`，模型容易按照提示词
把它错误描述为“已经正常进入等待审批”，调用方也不知道需要管理员显式恢复。

### 2.2 高：提案提交后模型答复失败会形成调用方不可见的待审批事实

提案工具调用完成时，Run、Proposal、Version 和 Approval 已经持久化。原
`run_one_turn()` 如果在下一次模型调用生成最终答复时失败，会直接向上抛异常；
`ChatService` 因拿不到 `LLMResponse`，既不会返回 `pending_approvals`，也不会持久化
本轮消息。

故障复现中数据库已经存在一条 `awaiting_approval` 提案，但聊天请求失败且会话消息
数为零。调用方无法取得 Run ID 和 Approval ID，重试还会生成新的 `turn_id`，并可能
只看到 `ACTION_ACTIVE_PROPOSAL_EXISTS`。

工具轮次达到上限也存在同类窗口：提案已创建，但原实现直接抛出
`AgentToolRoundLimitError`。

### 2.3 中：只读状态工具不足以解释关键业务终态

原 `get_action_status` 结果没有返回以下已经存在于业务模型中的事实：

- 当前提案版本的金额、币种、原因和类型专属参数；
- 审批决定备注；
- Run 级稳定错误码和可重试标记；
- Execution 级可重试标记。

因此，后续聊天无法准确解释修改后批准的最终金额、拒绝原因，以及发生在执行记录
创建前的工作流失败。

### 2.4 中：未预期工具异常可能泄漏底层信息

原 `ActionToolGateway.execute()` 只捕获领域异常和订单不存在异常。SQLite、文件、
序列化或其他基础设施异常会进入 Agent Runtime 的通用异常分支。通用分支把异常类型
和原始消息写入工具消息及返回事件，可能暴露数据库路径、SQL 或提供方原始响应，
不符合设计中的稳定错误码和脱敏要求。

### 2.5 低：改动位置未完全满足中文注释规范

Task 7 修改的主应用装配测试缺少中文行为注释；`run_one_turn()` 被修改的代码区域仍
保留英文历史注释。该问题不影响运行结果，但不符合仓库 `AGENTS.md` 的测试方法与
代码注释规范。

## 3. 已完成的修复

### 3.1 保留提案部分成功并显式返回恢复信息

提案工具结果和聊天响应中的 `PendingApproval` 新增：

```text
resume_required   工作流是否需要管理员显式恢复
error_code        首次启动失败的稳定错误码
```

正常启动返回 `resume_required=false`、`error_code=null`。运行器不可用时保留已经创建
的 Run、Proposal 和 Approval 标识，同时返回：

```text
status=queued
resume_required=true
error_code=CHECKPOINT_UNAVAILABLE
```

系统提示词同步增加约束：该分支必须说明提案已经保存但工作流尚未正常启动，禁止表述
为已经正常进入审批流程。

### 3.2 提案创建后的确定性降级响应

`run_one_turn()` 新增只在 `pending_approvals` 非空时启用的降级路径：

- 下一次模型调用失败时不再丢弃已提交提案；
- 工具轮次达到上限时不再抛出导致提案标识丢失的异常；
- 返回固定、脱敏的中文答复；
- 返回完整 `pending_approvals`；
- 设置 `answer_incomplete=true`；
- 把降级后的 assistant 消息追加到本轮消息，使 `ChatService` 可以正常持久化。

如果模型在任何提案创建前失败，仍保持原有异常传播语义，不把普通聊天故障伪装成
提案部分成功。

### 3.3 完善状态工具业务摘要

`get_action_status` 现在额外返回：

```text
run_error_code
run_error_retryable
decision_comment
current_version.version_no
current_version.amount_cents
current_version.currency
current_version.reason_code
current_version.reason_text
current_version.parameters
execution_error_retryable
```

`parameters_json` 在工具边界解析并校验为 JSON 对象；损坏数据映射为稳定的
`EXECUTION_DATA_INTEGRITY_ERROR`，不会把原始解析异常返回给模型。

### 3.4 统一异常脱敏

`ActionToolGateway` 对未预期异常统一返回：

```text
ACTION_TOOL_UNAVAILABLE
```

响应只包含固定中文提示，不包含异常类型、数据库路径或底层消息。Agent Runtime 的
通用工具异常分支也改为只返回稳定错误码 `TOOL_EXECUTION_FAILED`，避免其他工具的
未捕获异常继续泄漏内部细节。

### 3.5 补齐中文注释

- 为主应用装配测试补充中文行为说明；
- 把本次涉及的 `run_one_turn()` 英文历史注释翻译为中文；
- 新增测试方法全部带有中文行为或故障窗口说明；
- 新增响应模型属性全部带有中文属性注释。

## 4. 新增和强化的测试

本次新增或强化以下场景：

- 正常提案明确返回 `resume_required=false`；
- 运行器不可用时返回 `queued`、`resume_required=true` 和
  `CHECKPOINT_UNAVAILABLE`，同时保留 Run/Approval ID；
- 提案创建后最终模型答复失败时返回降级响应并持久化消息；
- 提案创建后达到工具轮次上限时返回既有待审批摘要；
- 状态工具返回当前版本金额、原因、参数和审批备注；
- 没有 Execution 记录的 Run 失败仍返回 Run 级错误码；
- 包含敏感文件路径的未预期异常被映射为 `ACTION_TOOL_UNAVAILABLE`，响应中不出现
  路径或异常类型；
- Agent Runtime 的通用工具异常使用 `TOOL_EXECUTION_FAILED`。

## 5. 验证结果

Task 7 与 Agent Runtime 专项回归：

```text
python -m pytest -q \
  tests/tools/test_action_arguments.py \
  tests/tools/test_action_gateway.py \
  tests/agent/test_action_agent_integration.py \
  tests/agent/test_runner.py \
  tests/agent/test_support_runner.py \
  tests/application/test_chat_service.py \
  tests/test_main.py
```

结果：`66 passed`。

全量回归：

```text
python -m pytest -q
```

结果：`914 passed, 10 skipped`。跳过项和警告均为仓库既有测试环境行为，本次没有
新增失败。

## 6. 与前序审查报告的边界

本报告只记录 Task 7 新发现并已修复的问题。Task 4 报告中保留的执行器授权链和成功
结果严格解析问题，以及 Task 6 报告中保留的 HTTP 校验错误格式、响应白名单和订单号
展示问题，仍属于各自任务的历史中低等级待办，不在本次修改范围内。

## 7. 最终结论

本次补齐后，Task 7 不再把工作流启动失败伪装成正常等待审批；提案提交后的模型故障
和工具轮次上限不会让持久化提案对调用方不可见；状态工具可以解释当前批准版本、审批
备注及 Run/Execution 失败；未预期异常也不会泄漏底层细节。Task 7 的 Agent 提案工具
集成、可靠响应和只读状态解释现在满足进入 Task 8 端到端验收的前置条件。
