# Task 6 高等级审查遗漏补齐报告

## 1. 报告信息

- 日期：2026-08-30
- 对应设计：`2026-08-28-refund-compensation-approval-durable-execution-design.md`
- 对应任务：Task 6——审批与 Run API
- Task 6 基线提交：`b935741 实现退款/补偿审批与Run的HTTP API及测试（Task 6）`
- 本次范围：修复 Task 6 代码审查发现的两项高等级遗漏

## 2. 审查发现的问题

### 2.1 高：修改金额接受非整数 JSON 类型

`DecisionChanges.amount_cents` 原来使用普通 Pydantic `int`。Pydantic 会把部分
浮点数、数字字符串和布尔值隐式转换成整数，例如：

```text
800.0  -> 800
"800"  -> 800
true   -> 1
```

审查期间通过真实 HTTP API 复现：提交 `amount_cents: true` 后接口返回 `201`，
数据库生成了金额为 1 分的新批准版本。

这违反设计中的以下不变量：

- 金额始终使用整数分；
- 禁止浮点金额；
- 请求结构不应通过隐式类型转换改变业务含义。

### 2.2 高：并发相同决定不能原子区分首次创建与幂等重放

原 Router 在调用决定服务前先查询审批详情，通过事务外的 `was_decided` 判断本次
请求应返回 `201/202` 还是 `200`。该“先读后写”存在竞争窗口：两个相同决定并发
请求可能都先观察到“尚未决定”，随后 Store 虽然只创建一条决定，但 Router 无法
判断谁是首次创建者。

审查期间强制两个请求同时完成预查询后的复现结果为：

```text
statuses        [201, 202]
resume_required [false, true]
decision_ids    [相同 ID, 相同 ID]
```

数据库决定保持唯一，但第二个幂等请求没有返回设计要求的 `200`，并且再次触发了
自动恢复，造成错误的 `resume_required=true`。这破坏了 API 的并发幂等语义，
也会让同一 checkpoint 被重复推进。

## 3. 已完成的修改

### 3.1 使用严格整数校验金额

`DecisionChanges.amount_cents` 现在使用：

```text
Field(gt=0, strict=True)
```

新的输入边界为：

- 只接受 JSON 正整数；
- 浮点数即使没有小数部分也返回 `422`；
- 数字字符串返回 `422`；
- 布尔值不再被当作整数 0/1，统一返回 `422`；
- 校验失败不创建 Approval Decision 或新 Proposal Version。

### 3.2 在决定事务内返回 created 标记

领域结果 `DecisionResult` 新增不可变 `created` 属性：

- `created=true`：本次 SQLite 决定事务首次插入决定；
- `created=false`：事务读取并返回已有的相同决定，属于幂等重放。

SQLite Store 在同一个 `BEGIN IMMEDIATE` 事务内确定该标记：

- 首次成功插入决定后返回 `created=true`；
- 发现已有相同决定时返回 `created=false`；
- 已有不同决定仍返回 `APPROVAL_ALREADY_DECIDED` 冲突。

因此，首次/重放判定不再依赖 Router 的事务外预查询，在并发情况下仍然可靠。

### 3.3 幂等决定不再重复自动恢复

应用服务现在只对 `created=true` 的首次决定调用自动恢复。对于
`created=false` 的相同决定重放：

- 返回首次不可变决定；
- 重新读取并返回当前 Run 状态；
- 保留首次实际决定人与自审标记；
- 不再次推进 LangGraph checkpoint；
- 如果首次恢复失败且 Run 仍在 `queued/awaiting_approval`，响应继续标记
  `resume_required=true`，调用方使用显式恢复 API 继续。

Router 直接依据事务返回的 `outcome.result.created` 选择状态码：

```text
created=true, resume_required=false -> 201
created=true, resume_required=true  -> 202
created=false                       -> 200
```

原有的审批详情预查询和 `was_decided` 竞争窗口已删除。

## 4. 新增和强化的测试

本次增加或强化以下保护场景：

- `amount_cents=800.0` 返回 `422`；
- `amount_cents="800"` 返回 `422`；
- `amount_cents=true` 返回 `422`；
- 上述非法金额不会产生审批决定；
- 两个管理员并发提交相同决定时，响应状态码严格为一个 `201` 和一个 `200`；
- 并发响应返回同一个 Decision ID；
- 数据库中只有一条 Approval Decision；
- 自动恢复只调用一次；
- Store 顺序和并发测试均验证只有一个结果 `created=true`；
- 应用服务幂等重放保留首次决定人、自审事实及单条决定审计。

所有新增测试方法均配有中文行为或边界说明，新增实体属性带有中文属性注释。

## 5. 验证结果

动作 API、Store 与应用服务专项测试：

```text
python -m pytest -q tests/api/test_action_router.py tests/actions/test_action_store.py tests/actions/test_action_service.py
```

结果：`126 passed`。

新增边界的定向测试：

```text
python -m pytest -q \
  tests/actions/test_action_store.py::test_decide_approval_same_content_returns_first_decision \
  tests/actions/test_action_store.py::test_decide_approval_concurrent_same_content_single_decision \
  tests/actions/test_action_service.py::test_duplicate_same_decision_keeps_original_decider_and_single_audit \
  tests/api/test_action_router.py::test_concurrent_same_decision_returns_created_and_replayed_once \
  tests/api/test_action_router.py::test_changes_amount_rejects_coercible_non_integer_types
```

结果：`7 passed`（最后一项参数化为三个输入场景）。

全量回归：

```text
python -m pytest -q
```

结果：`881 passed, 10 skipped`。跳过项和警告均为仓库既有测试环境行为，
本次没有新增失败。

## 6. 尚未纳入本次范围的中低等级发现

本次按要求只修复两项高等级遗漏。此前审查报告中的以下中低等级项未修改：

- 请求校验错误缺少统一稳定错误码，并可能回显原始非法输入；
- 版本参数响应解析未执行完整的对象、字段集合与类型校验；
- Run 执行结果仍以任意字典形式返回，缺少响应字段白名单；
- 审批列表和详情尚未返回用户可读的订单号。

这些问题不影响本次两项高等级修复的正确性，建议在 Task 7 前的安全强化中补齐。

## 7. 最终结论

本次已修复 Task 6 的两项高等级遗漏：审批修改金额现在只接受严格 JSON 正整数；
并发相同决定由数据库事务原子区分首次创建与幂等重放，状态码稳定为 `201/200`，
并且不会重复触发自动恢复。Task 6 的金融输入边界和并发审批幂等语义现已符合设计要求。
