# SupportPilot 模拟业务数据

## 范围

Seeder 为一个已经存在的企业生成：

- 2 个虚构客户
- 3 个订单
- 2 条物流记录
- 2 个工单
- 3 条工单评论

数据只用于本地开发、自动化测试和公开演示，不包含真实个人信息。

## 前置条件

1. 已执行数据库迁移。
2. 已注册用户并创建企业。
3. 执行用户在目标企业中的角色为 `admin`。
4. 从 `/auth/me` 获取用户 ID，从 `/organizations/` 获取企业 ID。

## 运行

```powershell
python -m app.demo_data.cli `
  --database-path .\chat_history.db `
  --organization-id "<organization-id>" `
  --actor-user-id "<admin-user-id>"
```

为了得到完全固定的测试时间：

```powershell
python -m app.demo_data.cli `
  --database-path .\chat_history.db `
  --organization-id "<organization-id>" `
  --actor-user-id "<admin-user-id>" `
  --reference-time "2026-07-28T08:00:00+00:00"
```

## 场景

| 订单号 | 状态 | 物流 | 对应问题 |
|---|---|---|---|
| `ORD-DELAY-001` | `processing` | 无 | 超过承诺时间仍未发货 |
| `ORD-TRANSIT-001` | `shipped` | `in_transit` | 查询运输进度 |
| `ORD-DELIVERED-001` | `delivered` | `delivered` | 签收后商品破损 |

相关工单：

- `TKT-DELAY-001`
- `TKT-DAMAGE-001`

## 重复执行

Seeder 是幂等的。对同一个企业重复执行不会创建重复数据。如果相同业务编号已经被
其他不兼容数据占用，Seeder 会抛出 `DemoDataConflictError`，不会覆盖原数据。

## 多租户验证

可以对两个企业分别执行 Seeder。两个企业都能拥有 `ORD-DELAY-001`，但查询时必须
提供各自的 `organization_id`，Store 不会返回另一个企业的数据。

## 清理

本阶段不提供清理命令，避免误删真实开发数据。需要重置纯演示环境时，停止 API，
备份数据库，然后删除整个专用演示数据库并重新执行 Alembic 和 Seeder。不要在混有
手工测试数据的数据库中执行批量删除。
