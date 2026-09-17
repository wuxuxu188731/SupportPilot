# SupportPilot 数据库迁移

## 1. 数据库位置

应用和 Alembic 都读取 `CHAT_DB_PATH`。未设置时使用项目根目录下的
`chat_history.db`。

## 2. 升级前备份

先停止 API 进程，确认没有进程继续写入 SQLite，然后复制以下实际存在的文件：

- `chat_history.db`
- `chat_history.db-wal`
- `chat_history.db-shm`

不要只复制主 `.db` 文件后继续让旧进程运行。

## 3. 查看版本

```powershell
python -m alembic current
python -m alembic history
```

## 4. 升级

```powershell
python -m alembic upgrade head
```

应用启动时 Store 也会调用同一迁移入口。部署时仍应先显式执行升级，再启动 API，
这样迁移失败不会与服务启动混在一起。

## 5. 从当前旧结构首次升级

如果数据库包含完整的 `users`、`conversations`、`messages`，但没有
`alembic_version`，应用会先把它标记为 `0001_baseline`，随后执行租户迁移。

每个已有用户会得到一个 `<username> organization`，角色为 `admin`。已有会话会归属
该用户的个人企业。

如果旧会话的 `user_id` 在 `users` 中不存在，升级会以
`unowned legacy conversations require mapping` 失败。恢复备份并先清理开发数据，
或者建立明确的旧用户映射；不要为孤儿会话猜测企业。

## 6. 回滚最近一次迁移

```powershell
python -m alembic downgrade -1
```

从 `0003_conversation_tenant_scope` 回滚到
`0002_organization_memberships` 会移除 `conversations.organization_id`，但保留原有
会话、用户、企业和成员。

**回滚 `0014_message_citations` 会删除全部引用行（`message_citations` 表）
与 `messages.answer_incomplete` 列**，消息本体与其他表都不受影响；回滚后会话
历史响应回到「只有问答文本」，刷新页面不再恢复引用卡片。引用数据一旦删除
无法恢复（当时就没有别的存储位置），因此该迁移在**库里已经存在引用行时拒绝
降级**（`message_citations 已存在引用数据，拒绝降级以避免丢失持久化的引用`），
与其他破坏性迁移（0010 / 0012）保持同一口径。升级 `0014` 本身是轻量操作
（`CREATE TABLE` + 一条可空 `ADD COLUMN`），不重建 `messages`；但为了给
`messages.answer_incomplete` 加上取值约束，迁移内部会用
`batch_alter_table(recreate="always")` 重建一次 `messages`（保留既有唯一约束
`uq_messages_conversation_seq` 与索引 `idx_messages_conversation_seq`），
因此本次升级同样适用第 2 节的**升级前备份**要求。

继续回滚会删除企业和成员表。回滚到 `base` 会删除用户、会话和消息表，属于破坏性
操作，只能在确认备份可恢复后执行。

## 7. 验收

```powershell
python -m pytest tests/db/test_migrations.py -q
python -m pytest tests/organizations tests/application/test_organization_service.py -q
python -m pytest tests/api/test_organization_router.py tests/api/test_dependencies.py -q
python -m pytest tests/sessions tests/api/test_router.py tests/test_main.py -q
python -m pytest -q
```

`tests/db/test_migrations.py` 已包含 `0014_message_citations` 的用例：
建表列集合、读路径索引、唯一约束与六条 CHECK 真的生效、孤儿引用被外键拦下、
`messages.answer_incomplete` 只接受 `NULL`/`0`/`1`、无引用数据时可对称降级
（表与列都消失、消息保留）、有引用数据时拒绝降级、以及从 `0013` 原地升级
不影响既有消息。
