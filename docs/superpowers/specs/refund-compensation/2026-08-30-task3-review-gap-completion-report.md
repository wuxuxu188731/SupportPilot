# Task 3 代码审查遗漏补齐报告

## 1. 报告信息

- 日期：2026-08-30
- 对应设计：`2026-08-28-refund-compensation-approval-durable-execution-design.md`
- 对应任务：Task 3——提案与审批应用服务
- Task 3 基线提交：`e183412 实现退款/补偿提案与审批应用服务及测试（Task 3）`
- 补齐范围：Task 3 完成后的代码审查发现项，不包含 Task 4 执行器与 Task 5 LangGraph Runtime 的具体实现

## 2. 审查发现回顾

Task 3 初始实现已具备退款/补偿提案创建、审批列表与详情、审批决定、修改后批准、RBAC、Run 查询和显式恢复等主要能力，但代码审查确认仍有以下五项缺口：

1. 工作流 `start()` 或 `resume()` 同步推进 Run 后，应用服务仍返回事务操作前的旧 Run 快照。
2. 已成功 Run 的重复恢复请求返回 `RUN_NOT_RESUMABLE`，与设计中“幂等返回现有成功结果”的要求冲突。
3. 第二位管理员重试完全相同的审批决定时，服务层按本次重试者计算 `self_approved`，可能与首次不可变决定的实际决定人矛盾。
4. 自审说明由应用服务在审批决定事务提交后单独追加，存在决定已落库但审计缺失、响应失败且未自动恢复的部分成功窗口。
5. 未注入工作流运行器时，创建和恢复结果的错误码为 `None`，不符合基础设施失败必须返回稳定错误码的约定。

## 3. 已补齐内容

### 3.1 工作流调用后重新读取 Run

`ActionWorkflowService` 现在会在以下节点调用 Store 重新读取 Run：

- 提案事务提交并调用 `runner.start()` 之后；
- 审批决定落库并调用 `runner.resume()` 之后；
- 管理员显式恢复调用完成之后。

通过不可变数据类的 `replace()` 替换结果中的 Run 快照，保证返回结果反映本次同步工作流调用完成后的最新业务状态。

### 3.2 成功 Run 恢复的幂等语义

`resume_run()` 现在对 `succeeded` Run 执行只读状态查询：

- 不再调用工作流运行器；
- 返回 `resume_ok=true` 和最新成功 Run；
- 返回已持久化的 `business_record_id` 等稳定业务结果；
- 保持取消、不可重试失败与未审批等非法恢复分支的 `409` 语义。

`ResumeOutcome` 增加了带中文属性注释的 `result` 字段，用于承载幂等恢复获取的稳定业务结果。

### 3.3 幂等审批保留首次实际决定人

`self_approved` 现在始终比较提案人与 `result.decision.decided_by_user_id`，不再使用本次 HTTP/服务调用的重试者。

因此，当第二位管理员重试内容完全相同的决定时：

- 返回首次的 Decision ID；
- 返回首次实际决定人；
- 自审标记与首次不可变决定保持一致；
- 不会产生第二条相互矛盾的决定审计。

### 3.4 自审信息与审批决定原子落库

删除应用服务在决定事务之后单独追加的 `decision_review_note` 事件，将以下信息合并到 SQLite Store 原有的 `decision_recorded` 审计事件：

- `proposer_user_id`；
- `decider_user_id`；
- `self_approved`。

该审计事件现在与新版本创建、Decision 写入、Approval 和 Proposal 状态更新处于同一个 `BEGIN IMMEDIATE` 事务中，避免决定已提交但关键自审审计缺失的部分成功窗口。

### 3.5 运行器不可用的稳定错误码

`_try_start()` 和 `_try_resume()` 在未注入运行器时统一返回：

```text
CHECKPOINT_UNAVAILABLE
```

这使创建结果与 `resume_required=true` 的审批结果都能给出可查询、可映射的稳定基础设施错误，不再使用含义不明的 `None`。

## 4. 新增与调整的测试

本次新增或强化了以下保护场景：

- 运行器首次启动推进到 `awaiting_approval` 后，创建结果返回最新 Run；
- 审批自动恢复推进到 `running` 后，决定结果返回最新 Run；
- 显式恢复推进 Run 后，恢复响应返回最新状态；
- 已成功 Run 重复恢复返回相同 `business_record_id`，不重复调用运行器；
- 第二位管理员重试相同决定时，保留首次决定人和自审结果；
- 同一审批只有一条 `decision_recorded` 审计，并原子包含提案人、决定人与自审标记；
- 未注入运行器时，创建和审批恢复结果返回 `CHECKPOINT_UNAVAILABLE`。

## 5. 验证结果

Task 3 应用服务与 Task 2 Store 联合专项测试：

```text
python -m pytest -q tests/actions/test_action_service.py tests/actions/test_action_store.py
```

结果：`104 passed`。

全量回归：

```text
python -m pytest -q
```

结果：`813 passed, 10 skipped`。跳过项与警告均为仓库既有测试环境行为，本次没有新增失败。

## 6. 对后续任务的影响

- Task 5 运行器可以同步推进 Store 中的 Run，Task 3 会在返回前重新读取最新状态。
- Task 6 恢复 API 对已成功 Run 应返回 `200` 和 `ResumeOutcome.result`，不应再映射为 `409`。
- Task 6 可将运行器未装配或 checkpoint 不可用统一映射为 `CHECKPOINT_UNAVAILABLE`。
- Task 7 提案工具可以直接使用 `ProposalCreationOutcome.creation.run.status` 判断是否已进入 `awaiting_approval`。
- 后续审批查询应以 `approval_decisions.decided_by_user_id` 和 `decision_recorded` 事件为不可变审批人事实，不应以幂等重试请求的调用者覆盖。

## 7. 最终结论

本次补齐后，Task 3 已消除工作流调用后状态过期、成功恢复不幂等、重试审批人归属错误、自审审计非原子以及基础设施错误码缺失五项问题。提案与审批应用服务现在可以作为 Task 4 幂等执行器、Task 5 LangGraph Runtime 与 Task 6 HTTP API 的稳定上层边界。
