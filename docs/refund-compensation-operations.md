# 退款/补偿审批与可靠执行运维手册

## 1. 能力边界

SupportPilot 当前支持由 Agent 创建退款或优惠券补偿提案，并由当前企业管理员通过
HTTP API 审批。提案创建本身不产生退款、优惠券或订单状态副作用；只有业务数据库中
存在有效批准决定后，确定性 LangGraph 工作流才会调用幂等执行器。

本阶段的退款和补偿都是模拟结果：

- 模拟退款写入 `refund_records`；
- 模拟优惠券补偿写入 `compensation_records`；
- 不连接支付渠道、优惠券平台、CRM 或物流平台；
- 不应向客户表述为真实到账或真实发券；
- 不包含 Worker、消息队列、定时重试、多实例租约、通知或审批前端。

## 2. 数据文件与配置

动作工作流使用两个独立 SQLite 数据库：

| 配置 | 默认值 | 用途 |
| --- | --- | --- |
| `CHAT_DB_PATH` | `chat_history.db` | 业务事实、审批、执行、结果与审计 |
| `LANGGRAPH_CHECKPOINT_DB_PATH` | `langgraph_checkpoint.db` | LangGraph 暂停位置与恢复状态 |
| `ACTION_WORKFLOW_VERSION` | `refund-compensation-v1` | checkpoint 工作流版本标识 |

业务数据库是授权、查询、幂等和审计的权威来源。checkpoint 只表示图运行位置，不能
单独证明某个动作已经批准，也不能替代业务数据库备份。

生产或共享环境必须为两个数据库配置明确的绝对路径，避免工作目录变化后生成新的空
数据库。`ACTION_WORKFLOW_VERSION` 在本阶段保持默认值；修改图拓扑或 State schema
前必须单独制定历史 Run 迁移策略，不能直接复用旧 checkpoint。

## 3. 部署与启动

### 3.1 安装依赖

```powershell
python -m pip install -r requirement.txt
```

### 3.2 配置环境变量

除现有认证、模型和知识库配置外，至少确认以下动作工作流配置：

```powershell
$env:CHAT_DB_PATH = "D:\data\supportpilot\business.db"
$env:LANGGRAPH_CHECKPOINT_DB_PATH = "D:\data\supportpilot\action-checkpoints.db"
$env:ACTION_WORKFLOW_VERSION = "refund-compensation-v1"
```

### 3.3 显式升级数据库

```powershell
python -m alembic upgrade head
python -m alembic current
```

动作工作流表由迁移 `0011_action_approval_workflow` 创建。应用启动时 Store 也会尝试
迁移，但部署时应先显式升级，使迁移失败与 API 启动失败能够分开处理。

### 3.4 启动 API

```powershell
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

启动后确认以下路由存在：

```text
GET  /approvals/
GET  /approvals/{approval_id}/
POST /approvals/{approval_id}/decisions/
GET  /action-runs/{run_id}/
POST /action-runs/{run_id}/resume/
```

所有请求继续使用 Bearer Token 和 `X-Organization-ID`。跨租户 Approval ID 或 Run ID
统一返回 404。

## 4. 标准审批操作

### 4.1 查看待审批项

```text
GET /approvals/?status=pending
GET /approvals/{approval_id}/
```

当前企业的 `agent` 和 `admin` 都可以读取；只有 `admin` 可以决定或恢复。

### 4.2 作出决定

批准：

```json
{
  "decision": "approved",
  "comment": "核实订单与政策后批准"
}
```

修改后批准：

```json
{
  "decision": "approved_with_changes",
  "changes": {
    "amount_cents": 6000,
    "refund_scope": "partial",
    "reason_code": "quality_issue",
    "reason_text": "仅对质量问题商品部分退款"
  },
  "comment": "原全额申请调整为部分退款"
}
```

拒绝：

```json
{
  "decision": "rejected",
  "comment": "当前证据不满足退款政策"
}
```

首次决定通常返回 `201`。如果决定已经保存但自动恢复失败，返回 `202`、
`resume_required=true` 和稳定错误码。此时决定仍然有效，不要提交不同决定，也不要
尝试删除或覆盖原决定；应按照第 5 节执行显式恢复。

完全相同的决定重试返回 `200` 和首次不可变决定。不同决定或不同修改内容返回 409。

## 5. Run 状态与恢复决策

| Run 状态 | 含义 | 操作 |
| --- | --- | --- |
| `queued` | 业务事实已创建，图尚未正常启动 | 管理员调用恢复接口，使图进入等待审批 |
| `running` | 图正在推进，或进程在节点中断 | 短暂等待；确认进程中断后由管理员显式恢复 |
| `awaiting_approval` | 正在等待人工决定 | 通过审批决定接口处理；没有决定时不要调用恢复接口 |
| `failed` 且可重试 | checkpoint 或模拟执行发生临时失败 | 使用原 Run ID 调用恢复接口 |
| `failed` 且不可重试 | 数据完整性或业务校验失败 | 不要反复恢复；保留数据并人工排查 |
| `cancelled` | 审批已拒绝 | 终态，不允许恢复 |
| `succeeded` | 模拟业务结果已持久化 | 无需恢复；重复恢复只读返回原稳定结果 |

显式恢复：

```text
POST /action-runs/{run_id}/resume/
```

恢复必须使用原 Run ID。服务会继续使用原批准版本和原幂等键；禁止通过新建 Run、修改
数据库或构造 checkpoint 绕过原审批事实。

## 6. 故障处理

### 6.1 决定已保存但返回 202

1. 保存响应中的 `run_id`、`decision_id` 和稳定错误码。
2. 使用 `GET /action-runs/{run_id}/` 确认决定和 Run 状态。
3. 修复 checkpoint 文件权限、磁盘空间或 SQLite 锁定问题。
4. 由当前企业管理员调用恢复接口。
5. 再次查询 Run；成功时应返回同一个 Execution ID 和业务结果 ID。

不要重新审批，也不要生成新的幂等键。

### 6.2 服务在等待审批时重启

只要业务数据库和 checkpoint 文件仍在原路径，重新启动应用后可直接提交审批决定。
运行器使用原 `thread_id` 从审批中断位置继续。

如果 checkpoint 文件丢失，但业务数据库中的 Run、Proposal、Approval 和 Decision
完整，批准后的恢复可以从业务事实重建图路由。业务数据库丢失时不能用 checkpoint
反向重建审批授权，必须从一致备份恢复业务数据库。

### 6.3 可重试执行失败

查询 Run，确认：

```text
status=failed
last_error_retryable=true
```

管理员调用显式恢复。成功恢复会沿用原 Execution ID 和幂等键，增加
`attempt_count`，不会产生第二条退款或补偿记录。

### 6.4 不可重试失败

常见稳定错误码包括：

```text
EXECUTION_DATA_INTEGRITY_ERROR
ACTION_CURRENCY_MISMATCH
ACTION_REFUND_BALANCE_EXCEEDED
ACTION_COMPENSATION_DUPLICATE
ACTION_COMPENSATION_CAP_EXCEEDED
```

保留 Run、提案版本、决定、执行和审计数据，检查订单事实及引用链。不要直接修改
`action_proposal_versions`、`approval_decisions` 或成功业务结果；数据库触发器会拒绝
部分非法修改，但触发器不能替代运维流程。

### 6.5 跨租户访问告警

企业 B 使用企业 A 的 Approval ID 或 Run ID 时应始终返回 404，且企业 A 的决定、
checkpoint、Execution 和业务结果均不变化。如果观察到 403、资源详情或任何状态
变化，应立即停止服务并按安全事件处理。

## 7. 备份与恢复

### 7.1 一致备份

1. 停止 API，确认没有进程继续写入两个 SQLite 数据库。
2. 同时备份业务数据库和 checkpoint 数据库。
3. 如果存在对应的 `-wal`、`-shm` 文件，也一并备份。
4. 记录当前应用提交、Alembic 版本和 `ACTION_WORKFLOW_VERSION`。

不要只备份 checkpoint，也不要在 API 仍写入时分别复制两个主数据库文件。

### 7.2 恢复

1. 停止 API。
2. 恢复同一备份批次的业务数据库、checkpoint 数据库及其 WAL/SHM 文件。
3. 恢复原路径和文件权限。
4. 确认 `ACTION_WORKFLOW_VERSION` 与备份时一致。
5. 运行 `python -m alembic current`，确认业务库版本。
6. 启动 API，通过审批详情和 Run 查询核对关键记录。
7. 只对明确可恢复的 Run 执行显式恢复。

迁移 `0011` 在动作工作流表存在业务数据时会拒绝降级，防止静默删除审批与执行事实。
如确需降级，必须先制定数据导出、归档和清理方案，不能直接对生产库执行
`alembic downgrade`。

## 8. 验收与回归

六个设计验收场景的自动化测试位于：

```text
tests/integration/test_action_workflow.py   场景 A-E
tests/api/test_action_router.py             场景 F
```

专项验收：

```powershell
python -m pytest -q tests/integration/test_action_workflow.py
python -m pytest -q tests/api/test_action_router.py::test_acceptance_scenario_f_cross_tenant_access_returns_404
```

全量回归：

```powershell
python -m pytest -q
```

发布前必须满足：未审批执行数为零、非管理员审批成功数为零、跨租户泄漏和状态修改数
为零、相同幂等键只有一个业务结果，且重启恢复测试通过。
