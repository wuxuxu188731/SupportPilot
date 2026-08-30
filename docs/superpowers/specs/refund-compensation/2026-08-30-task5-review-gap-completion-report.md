# Task 5 代码审查与可靠恢复遗漏补齐报告

## 1. 报告信息

- 日期：2026-08-30
- 对应设计：`2026-08-28-refund-compensation-approval-durable-execution-design.md`
- 对应任务：Task 5——LangGraph Runtime
- Task 5 基线提交：`1a42004 实现 LangGraph 确定性工作流运行器及测试（Task 5）`
- 本次范围：修复 Task 5 代码审查发现的三项可靠恢复与失败一致性遗漏

## 2. 审查结论回顾

Task 5 初始实现已经具备 SQLite checkpointer、StateGraph、审批中断、决定恢复、
重启恢复、成功结果重放和可重试失败显式恢复等主流程能力。代码审查与故障窗口
复现确认仍有以下三项遗漏。

### 2.1 高：已批准的 running Run 会被退回待审批

原图路由把所有 `running` 状态都视为首次启动在两次状态转换之间的中间态，
统一进入 `mark_awaiting_approval`。

如果进程在审批恢复已经把 Run 推进到 `running`、但执行节点尚未完成时中断，
下一次恢复会把 Run 错误退回 `awaiting_approval`。这会绕开 Task 4 已实现的
`claimed/running` 中断执行重新认领路径。

审查期间的复现结果为：

```text
before running
after awaiting_approval
```

### 2.2 高：checkpoint 丢失时恢复静默空转

原运行器只要读取到审批决定，就直接调用 `Command(resume=...)`，但没有确认
对应 `thread_id` 是否存在审批中断 checkpoint。

当 checkpoint 文件丢失或被替换为空数据库时，LangGraph 对不存在的 checkpoint
可能不抛异常而直接返回。应用服务因此把调用判断为恢复成功并返回
`resume_required=false`，但业务 Run 仍停留在 `awaiting_approval`。

审查期间的复现结果为：

```text
运行器未抛异常
run_status awaiting_approval
```

### 2.3 中：执行认领前异常没有形成业务失败事实

原执行节点捕获所有 `ActionError` 后，假定执行器已经更新执行记录和 Run，随后
直接让图进入失败终点。但批准链损坏等错误可能发生在 `tool_executions` 创建之前，
此时执行器没有可用于调用 `record_execution_failure()` 的 Execution ID。

结果是图状态已经 `failed` 并进入 END，而业务 Run 仍为 `running`，Proposal 仍为
`approved`，稳定错误码和失败审计也没有落库。

审查期间的复现结果为：

```text
graph_outcome failed
error_code EXECUTION_DATA_INTEGRITY_ERROR
run_status running
run_error None
```

## 3. 已完成的修复

### 3.1 按业务决定区分 running 所属阶段

`action_graph.py` 的起始路由现在同时读取 Run 与持久化 Approval Decision：

- `queued` 仍按首次启动路径进入等待审批；
- `running` 且没有决定时，视为首次启动在状态转换之间中断，补齐等待状态；
- `running` 且已有决定时，直接进入持久化决定节点和执行恢复；
- `awaiting_approval` 且已有决定、但 checkpoint 丢失时，同样直接进入决定节点；
- `awaiting_approval` 且没有决定时，继续保持审批中断。

因此，已批准 Run 不再发生 `running -> awaiting_approval` 的业务阶段回退。

### 3.2 根据真实 checkpoint 位置选择恢复方式

`LangGraphActionWorkflowRunner` 现在在恢复前调用图状态查询，并依据 checkpoint
中的待执行节点选择恢复方式：

1. checkpoint 正在等待审批且存在持久化决定：使用 `Command(resume=...)`；
2. checkpoint 存在其他未完成节点：使用 `invoke(None)` 从原节点继续；
3. checkpoint 不存在或图已经结束：使用稳定 Run ID 和企业 ID 从 START 重建；
4. 从 START 重建时，图按业务库中的 Run、Proposal、Approval 和 Decision 路由，
   不信任旧 checkpoint 或调用方提供的批准标记。

运行器读取决定时不再仅限 `awaiting_approval` 状态。即使进程已经把 Run 推进到
`running`，仍能携带真实决定恢复审批中断节点。

### 3.3 原子记录无 Execution 场景的工作流失败

`ActionStore` 新增 `record_workflow_failure()` 契约，SQLite 实现在同一个
`BEGIN IMMEDIATE` 事务中完成：

- 校验 Run 当前为 `running`；
- 校验 Run 与 Proposal 属于同一引用链；
- 把 Proposal 推进到 `failed`；
- 把 Run 推进到 `failed`，保存稳定错误码和可重试标记；
- 追加 `run_failed` 审计事件。

执行节点捕获 `ActionError` 后会重新读取 Run：

- 如果执行器已经完成失败落库，则直接使用权威失败事实；
- 如果 Run 仍为 `running`，说明错误发生在执行认领前，调用新增 Store 契约补齐失败；
- 如果 Run 处于其他不符合契约的状态，则不伪装成正常图失败，而是继续抛出状态冲突。

这保证图进入失败终点之前，业务数据库中已经存在一致、可查询、可审计的失败事实。

## 4. 新增回归测试

本次新增三类针对性测试：

- 原 checkpoint 文件丢失并换成空数据库后，已批准 Run 能从业务状态重建并成功执行；
- Run 与 Tool Execution 均停在 `running` 时，恢复沿用原 Execution ID 和幂等键，
  `attempt_count` 递增且只产生一条业务结果；
- 执行器在认领前抛出 `EXECUTION_DATA_INTEGRITY_ERROR` 时，Proposal 与 Run 均进入
  不可重试失败，错误码和 `run_failed` 审计正确落库，且不创建 Tool Execution。

所有新增测试方法均配有中文行为或边界说明，新增代码注释和文档字符串均使用中文。

## 5. 验证结果

Task 5 工作流、运行器、集成与 Store 联合专项测试：

```text
python -m pytest -q tests/workflows/test_action_graph.py tests/workflows/test_action_runtime.py tests/integration/test_action_workflow.py tests/actions/test_action_store.py
```

结果：`88 passed`。

全量回归：

```text
python -m pytest -q
```

结果：`859 passed, 10 skipped`。跳过项与警告均为仓库既有测试环境行为，
本次没有新增失败。

## 6. 对后续任务的影响

- Task 6 的审批 API 可以准确使用 `resume_required`：checkpoint 丢失不会再被静默判断为恢复成功。
- Task 6 的显式恢复 API 可以继续处理审批后 `running` 的崩溃窗口，不会要求重新审批。
- Task 4 的 `claimed/running` 中断重新认领能力现在能够由 Task 5 Runtime 正确到达。
- Run 状态查询可以稳定解释执行认领前的数据完整性失败，不再出现图已结束但 Run 永久运行的状态。
- 后续如果引入多实例 Worker，仍需按设计单独增加数据库租约；本次修复保持单进程 MVP 边界。

## 7. 最终结论

本次已补齐 Task 5 审查发现的三项遗漏：已批准 `running` Run 不再回退到待审批，
checkpoint 丢失可以依据业务数据库从 START 可靠重建，执行认领前异常能够原子形成
Proposal、Run 与审计失败事实。Task 5 的暂停恢复、崩溃恢复和失败一致性现在与设计
及 Task 2、Task 3、Task 4 已建立的不变量保持一致。
