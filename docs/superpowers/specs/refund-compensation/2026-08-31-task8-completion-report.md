# Task 8 端到端验收与运维交付报告

## 1. 报告信息

- 日期：2026-08-31
- 对应设计：`2026-08-28-refund-compensation-approval-durable-execution-design.md`
- 对应任务：Task 8——端到端验收与运维文档
- 基线提交：`12eec63 修复Task 7审查遗漏并补充报告`
- 交付范围：六个验收场景、恢复运维手册、README 能力边界和全量回归

## 2. 六个验收场景

### 2.1 场景 A：退款批准

使用真实 SQLite Action Store、幂等执行器、LangGraph 图和 SQLite checkpointer 验证：

- 提案创建后 Run 进入 `awaiting_approval`；
- 批准前退款记录数为零；
- 管理员批准后自动恢复并进入 `succeeded`；
- 只产生一条指定金额的模拟退款记录；
- 状态查询返回稳定 Execution 和业务结果。

测试：`test_acceptance_scenario_a_refund_approval`。

### 2.2 场景 B：修改后批准

新增完整版本审计验收：

- 原提案为 100 元全额退款；
- 管理员修改为 60 元部分退款后批准；
- 版本 1 保留 100 元和 `refund_scope=full`；
- 版本 2 保存 60 元和 `refund_scope=partial`；
- Approval Decision 引用版本 2；
- 执行结果严格为 60 元；
- 部分退款后订单主状态仍为 `processing`。

测试：`test_acceptance_scenario_b_approved_with_changes`。

### 2.3 场景 C：拒绝

使用真实运行器验证：

- Run 进入 `cancelled`；
- Proposal 进入 `rejected`；
- 不创建 Tool Execution 或退款记录；
- 审批备注保持可查询，可用于解释拒绝原因。

测试：`test_acceptance_scenario_c_rejected`。

### 2.4 场景 D：重复恢复与幂等

新增明确的重复恢复验收：

- 同一已批准并成功的 Run 连续恢复两次；
- 两次返回相同业务结果；
- 恢复前后 Execution ID 不变；
- `business_record_id` 不变；
- 数据库始终只有一条 Tool Execution 和一条退款记录。

测试：`test_acceptance_scenario_d_repeated_resume_is_idempotent`。

### 2.5 场景 E：重启恢复

在 Run 已暂停等待审批后，使用同一业务数据库和 checkpoint 文件重新创建执行器、图、
运行器和应用服务，验证：

- 原 `thread_id` 保持不变；
- 重启后的管理员决定可以恢复原图；
- Run 正常进入 `succeeded`；
- 只产生一条模拟退款记录。

测试：`test_acceptance_scenario_e_restart_recovery`。

### 2.6 场景 F：租户攻击

通过真实 FastAPI 动作路由验证企业 B 使用企业 A 的 Approval ID 或 Run ID 时：

- 审批详情、Run 查询、决定和恢复全部返回 404；
- 非成员在租户依赖层同样返回 404；
- 不创建 Approval Decision、Tool Execution 或退款记录；
- 工作流运行器没有收到恢复调用。

测试：`test_acceptance_scenario_f_cross_tenant_access_returns_404`。

## 3. 运维文档

新增 `docs/refund-compensation-operations.md`，包含：

- 模拟退款/补偿能力边界；
- 业务数据库与 checkpoint 数据库的职责；
- `CHAT_DB_PATH`、`LANGGRAPH_CHECKPOINT_DB_PATH` 和
  `ACTION_WORKFLOW_VERSION` 配置；
- 显式迁移和 API 启动步骤；
- 审批、修改后批准和拒绝请求示例；
- `queued`、`running`、`awaiting_approval`、可重试/不可重试失败及终态的操作矩阵；
- 决定已保存但返回 202、服务重启、checkpoint 丢失和可重试执行失败的恢复步骤；
- 两个 SQLite 数据库的一致备份与恢复；
- 跨租户安全告警和发布前验收命令。

## 4. README 更新

README 现在明确说明：

- 退款与优惠券补偿提案、管理员审批和 LangGraph 暂停恢复已经完成；
- Agent 只暴露两个提案工具和一个只读状态工具；
- 未审批提案不会产生业务副作用；
- 当前结果只是模拟退款和模拟发券；
- 真实支付、真实优惠券、审批前端、通知、Worker 和多实例租约仍不在当前范围；
- 运维人员应阅读新增的恢复运维手册。

## 5. 验证结果

Task 8 专项链路：

```text
python -m pytest -q \
  tests/integration/test_action_workflow.py \
  tests/api/test_action_router.py \
  tests/agent/test_action_agent_integration.py
```

结果：`33 passed`。

全量回归：

```text
python -m pytest -q
```

结果：`916 passed, 10 skipped`。跳过项和警告均为仓库既有测试环境行为，本次没有
新增失败。

## 6. 最终结论

设计第 19 节的六个验收场景现在都有明确、可独立定位的自动化测试；等待审批、修改后
批准、拒绝、重复恢复、服务重启和跨租户攻击均满足预期不变量。运维手册和 README
已经明确部署、备份、恢复、安全边界与模拟执行限制。Task 8 的完成条件已满足，本阶段
可以进入发布前人工环境核对。
