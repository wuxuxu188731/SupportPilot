# Task 2 代码审查遗漏补齐报告

## 1. 报告信息

- 日期：2026-08-29
- 对应设计：`2026-08-28-refund-compensation-approval-durable-execution-design.md`
- 对应任务：Task 2——SQLite Store 与事务不变量
- Task 2 基线提交：`72ded52 实现动作工作流 SQLite 存储与事务不变量（Task 2）`
- 补齐范围：Task 2 完成后的代码审查发现项，不包含 Task 3 应用服务与 RBAC 实现

## 2. 审查发现回顾

Task 2 初始实现已经覆盖 `BEGIN IMMEDIATE` 写事务、并发审批决定、退款与补偿执行时重验、幂等业务结果和生命周期审计，但审查确认仍有以下空档：

1. Run 状态转换只校验调用者提供的预期状态，没有由 Store 强制合法状态边，调用者可以把 `queued` 直接写成 `succeeded`。
2. 创建提案仅验证 `parameters_json` 是 JSON 对象，没有验证原因枚举、类型专属字段、全额退款金额和固定补偿券有效期。
3. 审批决定事务没有按审批时最新退款余额、补偿重复原因及累计上限重验决定版本。
4. 重复认领返回相同执行记录，但不能表达本次调用是否取得执行权；执行结果也可以从 `claimed` 跳过 `running` 直接写入。
5. 公共审计接口只分别校验 Run 和 Proposal 存在，没有验证二者属于同一引用链。

## 3. 已补齐内容

### 3.1 Store 内置 Run 状态机

新增固定状态转换表，`expected_statuses` 现在只承担并发比较职责，不再构成状态跳转授权。当前允许的主要状态边为：

```text
queued -> running | failed
running -> awaiting_approval | succeeded | failed | cancelled
awaiting_approval -> running | failed
failed(retryable) -> running
```

同时增加以下状态字段约束：

- 进入 `failed` 必须提供非空稳定错误码；
- 非失败状态禁止携带错误码或可重试标记；
- `succeeded`、`cancelled` 和不可重试 `failed` 保持终态；
- 执行成功只能把处于 `running` 的 Run 推进到成功终态。

原测试中的 `queued -> awaiting_approval` 路径已调整为 `queued -> running -> awaiting_approval`。

### 3.2 严格的提案领域参数校验

SQLite Store 在创建事务开始前严格解析类型专属参数，并在事务内结合订单事实校验：

- 退款原因必须属于固定 `RefundReasonCode`；
- 补偿原因必须属于固定 `CompensationReasonCode`；
- `other` 原因必须提供非空详细说明；
- 退款参数只能包含 `refund_scope`，取值只能是 `full` 或 `partial`；
- `full` 退款金额必须等于事务内最新可退余额；
- 补偿参数只能包含 `coupon_valid_days`，且必须是整数 30；
- 未知类型字段会被拒绝，不能进入提案版本 JSON。

### 3.3 审批决定事务内重验

批准和修改后批准现在都会在写入决定的同一个 `BEGIN IMMEDIATE` 事务内重新读取订单及成功业务记录，并校验：

- 退款金额不超过审批时可退余额；
- `full` 退款金额等于审批时全部可退余额；
- 同一订单同一补偿原因尚未成功补偿；
- 补偿累计金额加本次金额不超过订单总额的 50%；
- 修改后的原因枚举和类型专属参数仍符合完整领域契约。

普通批准遇到余额或补偿业务冲突时保留对应稳定业务异常；修改后批准的非法版本统一映射为 `APPROVAL_INVALID_CHANGES`。校验失败时，新版本、决定和状态更新全部回滚。

### 3.4 显式执行认领结果

领域契约新增不可变 `ExecutionClaim`：

```text
execution  当前稳定执行记录
acquired   本次调用是否取得执行权
```

语义如下：

- 首次认领返回 `acquired=true`；
- `failed_retryable` 使用原幂等键重新认领后返回 `acquired=true`，尝试次数递增；
- 对已有 `claimed`、`running` 或 `succeeded` 记录的重复认领返回稳定记录和 `acquired=false`；
- 调用方只有在 `acquired=true` 时才可以继续推进执行。

并发测试验证两个调用者同时使用相同幂等键时，只有一个结果取得执行权，并且数据库仍只有一条执行记录。

### 3.5 严格执行状态

`record_execution_success()` 和 `record_execution_failure()` 现在只接受 `running` 状态：

- `claimed` 不能跳过 `running` 直接写成功或失败；
- `succeeded` 重放仍返回原稳定结果；
- `failed_terminal` 仍保持不可覆盖；
- 可重试失败必须先按原幂等键重新认领，再推进到 `running`。

### 3.6 审计引用链完整性

统一审计写入辅助方法现在会：

- 校验 `details_json` 是合法 JSON 对象；
- 当携带 `proposal_id` 时，验证 Proposal 属于指定 Run；
- 在同一个写事务内拒绝同企业不同 Run/Proposal 的错误串接。

因此内部生命周期审计与公共恢复审计使用同一套引用链保护。

### 3.7 设计文档同步

主设计文档同步补充了以下规则：

- Store 内置合法 Run 状态边；
- `ExecutionClaim.acquired` 的执行权语义；
- 只有 `running` 执行可以记录结果；
- 创建事务严格验证类型参数；
- 审批事务按最新业务事实重验决定版本；
- 并发重复认领不得重复授予执行权。

## 4. 新增与调整的测试

本次增加或强化了以下场景：

- `queued -> succeeded` 非法跳转被拒绝；
- 合法等待路径必须经过 `running`；
- 非法退款范围、全额退款金额、补偿参数和原因枚举被拒绝；
- `other` 原因缺少详细说明时被拒绝；
- 审批等待期间退款余额变化后拒绝超额批准；
- 修改后批准的 `full` 金额不一致时回滚；
- 审批等待期间出现同原因补偿后拒绝批准；
- 并发认领只有一个 `acquired=true`；
- `claimed` 执行不能直接记录成功或失败；
- 审计 Run 与 Proposal 错配时拒绝写入；
- 完整生命周期测试按正式 Run 状态机运行。

## 5. 验证结果

Task 2 Store 专项测试：

```text
python -m pytest -q tests/actions/test_action_store.py
```

结果：`64 passed`。

Task 1/Task 2 联合专项测试：

```text
python -m pytest -q tests/actions/test_action_store.py tests/actions/test_action_schema.py tests/db/test_migrations.py
```

结果：`108 passed`。

全量回归：

```text
python -m pytest -q
```

结果：`773 passed, 10 skipped`。跳过项和警告均为仓库既有测试环境行为，本次没有新增失败。

## 6. 对后续任务的影响

- Task 3 应用服务仍负责当前 Membership 的 `admin` RBAC、HTTP/工具错误映射和允许修改字段的请求模型约束。
- Task 4/Task 5 调用 `claim_execution()` 时必须检查 `ExecutionClaim.acquired`；为 `false` 时只能读取状态或等待稳定结果，不能再次执行动作。
- Task 4 执行器仍必须加载持久化 Approval Decision 并验证完整授权链，Store 的 Proposal 状态和参数校验不能代替执行前授权。
- Task 5 工作流必须严格执行 `queued -> running -> awaiting_approval`，审批恢复后先进入 `running`，再认领和执行。
- 显式恢复可重试失败时必须沿用原幂等键，重新认领取得执行权后再进入 `running`。

## 7. 最终结论

本次补齐后，Task 2 已覆盖审查发现的状态机绕过、非法业务参数持久化、审批时竞争窗口、重复执行权授予语义不清和审计引用链串接问题。SQLite Store 现在可以作为 Task 3 应用服务和 Task 4 幂等执行器的稳定事务基础。
