# Task 4 代码审查与高等级遗漏补齐报告

## 1. 报告信息

- 日期：2026-08-30
- 对应设计：`2026-08-28-refund-compensation-approval-durable-execution-design.md`
- 对应任务：Task 4——幂等模拟执行器
- Task 4 基线提交：`37c105b 实现幂等模拟执行器及测试（Task 4）`
- 本次修复范围：Task 4 代码审查发现的两项高等级遗漏

## 2. 审查结论回顾

Task 4 初始实现已覆盖完整批准决定的主要校验、版本化幂等键、首次认领、成功结果重放、退款与补偿上限重验、模拟业务结果以及可重试失败等正常路径。代码审查发现以下问题。

### 2.1 高：进程中断后执行记录永久卡在 claimed/running

初始执行流程分别提交了认领、进入 `running`、调用模拟适配器和成功结果写入。如果进程在认领之后、成功结果落库之前中断，数据库会保留 `claimed` 或 `running` 记录。

恢复调用时，既有 `claim_execution()` 只会返回 `acquired=false`，执行器随后抛出 `RUN_STATE_CONFLICT`，没有任何路径可以再次获取执行权。这会直接阻断 Task 5 的重启恢复语义。

### 2.2 高：refunded 标记使用事务外余额快照

初始实现在适配器调用前读取可退余额，并由适配器计算 `mark_order_refunded`。Store 在写入成功结果时重新计算余额，但要求事务内结果与事务外快照完全一致。

如果在两次读取之间发生另一笔合法退款，当前退款即使仍未超额，也可能因为旧快照与最新余额对应的 refunded 标记不同，被错误标记为不可重试的数据完整性失败。

### 2.3 中：执行前授权链校验仍可进一步收紧

执行器已校验 Approval、Decision 和 Version 的主要关系，但对 Run 冗余 Proposal 指针、Run 工作流类型、Proposal 状态以及 Approval/Decision 状态的严格对应仍可增加执行器级别的明确校验。

该项为中等级发现，不在用户指定的本次两项高等级修复范围内，本次未修改。

### 2.4 中：成功结果重放的 JSON 类型校验不够严格

`_replay_success()` 尚未对成功结果执行精确字段集合、JSON 对象类型、非空业务结果标识以及 `order_marked_refunded` 布尔类型校验。损坏数据可能引发原始类型异常或被错误布尔化。

该项为中等级发现，不在用户指定的本次两项高等级修复范围内，本次未修改。

## 3. 高等级遗漏修复结果

### 3.1 单进程幂等键互斥

`IdempotentActionExecutor` 现在为每个服务端幂等键维护进程内共享执行锁：

- 锁表在模块级共享，多个 Executor 实例也使用同一把幂等键锁；
- 同一进程内的第二个并发调用会等待第一个调用完成；
- 第一个调用成功后，第二个调用只重放既有成功结果，不重复调用适配器；
- 进程重启后内存锁表自然清空，新进程可判定数据库中的 `claimed/running` 记录为上次进程的中断残留。

该方案与当前设计的单进程 MVP 范围一致。设计已将多实例分布式调度列为非目标；未来引入多实例时，必须改用数据库租约、所有者标识与过期时间，不能继续仅依赖进程内锁。

### 3.2 中断执行重新认领

`ActionStore` 新增 `reclaim_interrupted_execution()` 契约，SQLite 实现在一个 `BEGIN IMMEDIATE` 事务中：

1. 限定企业并重新读取执行记录。
2. 只允许重新认领 `claimed` 或 `running`。
3. 沿用原 Execution ID 与原幂等键。
4. 把执行恢复到 `claimed`，递增 `attempt_count`。
5. 保持 Proposal 为 `executing`，追加新的执行认领审计。

执行器只在持有对应幂等键进程锁时调用该契约，避免把本进程中仍在正常运行的执行误判为中断残留。

### 3.3 refunded 决策收口到 Store 成功事务

退款执行现在不再在适配器前读取可退余额，`SimulatedRefundAdapter` 也不再返回 `mark_order_refunded`。适配器只生成模拟业务结果标识。

SQLite Store 在成功写入的 `BEGIN IMMEDIATE` 事务中完成：

1. 重新计算最新已退款金额与可退余额。
2. 校验本次金额不超过当前余额。
3. 根据本次金额是否等于事务内剩余余额计算 `expected_mark_refunded`。
4. 原子写入退款记录、Execution 成功结果、Proposal/Run 终态和订单状态。

Task 2 已有的 `mark_order_refunded` 参数为保持当前 Store 接口兼容暂时保留，但已不参与任何业务判断。

## 4. 新增与调整的测试

本次新增或调整了以下场景：

- 执行记录停留在 `claimed` 时，可沿用原 ID 和幂等键重新认领并成功执行；
- 执行记录停留在 `running` 时，可沿用原 ID 和幂等键重新认领并成功执行；
- 中断恢复的 `attempt_count` 递增且不产生第二条业务结果；
- 同进程两个并发调用时，适配器只执行一次，等待者重放相同结果；
- 适配器运行期间插入另一笔退款后，当前合法退款仍成功，并按事务内最新余额把订单标记为 `refunded`；
- Store 忽略事务外的 refunded 提示值，仅根据事务内余额返回权威结果；
- 退款适配器单元测试改为仅验证模拟业务结果 ID，不再验证事务外余额决策。

## 5. 验证结果

Task 4 Executor 与 Action Store 联合专项测试：

```text
python -m pytest -q tests/actions/test_action_executor.py tests/actions/test_action_store.py
```

结果：`86 passed`。

全量回归：

```text
python -m pytest -q
```

结果：`835 passed, 10 skipped`。跳过项和警告均为仓库既有测试环境行为，本次没有新增失败。

## 6. 对后续任务的影响

- Task 5 在执行节点因进程退出而中断后，可在重启时重放同一节点，Task 4 会重新认领原执行记录。
- 中断恢复继续使用原幂等键，因此不会绕过现有唯一约束或生成第二个业务结果。
- 当前互斥保证以单进程部署为前提；引入多实例、Worker 或消息队列前，必须单独设计分布式执行租约。
- Task 5 不应传入或依赖事务外计算的 refunded 标记，应以 `ExecutionOutcome.order_marked_refunded` 的 Store 事务结果为准。
- 两项中等级发现仍保留，后续如需进入安全强化或数据损坏演练，建议在 Task 5 开始前一并补齐。

## 7. 最终结论

本次已修复 Task 4 代码审查发现的两项高等级遗漏：单进程 MVP 中的 `claimed/running` 中断记录现在可以安全重新认领，同时不会抢占本进程的活跃执行；订单 `refunded` 状态现在完全以 Store 成功事务内的最新余额为准，不再受事务外快照竞争影响。
