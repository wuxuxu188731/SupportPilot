# Minimal Multi-Tenancy Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有登录与用户级会话隔离之上，增加企业、成员关系、`admin`/`agent` 最小 RBAC、经服务端验证的租户上下文和 Alembic 数据库迁移，使同一套 SupportPilot 服务可以安全地供多个企业使用。

**Architecture:** JWT 继续只证明“当前用户是谁”，租户范围接口通过 `X-Organization-ID` 选择当前企业，服务端根据 `memberships` 表验证用户是否属于该企业，绝不把请求头本身当作授权依据。会话继续属于创建它的用户，同时新增企业边界，所有会话读写必须同时匹配 `organization_id + user_id + conversation_id`。当前同步 `sqlite3` Store 保留，表结构的创建、升级和回滚统一交给 Alembic，为以后迁移 SQLAlchemy/PostgreSQL 留出边界。

**Tech Stack:** Python 3.10+、FastAPI 0.115、Pydantic 2、SQLite、标准库 `sqlite3`、SQLAlchemy 2.x（仅供 Alembic 使用）、Alembic 1.x、pytest、FastAPI TestClient。

## Global Constraints

- 本计划只实现最小企业租户基础；不实现订单、物流、工单、RAG、退款审批、Refresh Token、Redis、LangGraph、PostgreSQL 或前端。
- 第一版角色固定为 `admin` 和 `agent`；不实现 `viewer`、自定义角色、细粒度权限表、成员删除和角色修改。
- Access Token 继续只包含 `user_id`；不得把固定 `organization_id` 写入 Token，避免用户加入多个企业后必须为每个企业重新登录。
- `X-Organization-ID` 只用于选择当前企业；权限必须由服务端查询 `memberships` 后决定。
- 缺少或空白 `X-Organization-ID` 返回 `400 organization context required`；当前用户不属于该企业时返回 `404 organization not found`，避免泄露企业是否存在。
- 企业内 `admin` 和 `agent` 都可以使用会话接口；只有 `admin` 可以向企业添加成员。
- 会话仍为创建者私有；同企业的另一个客服不能读取该用户的会话。每次会话查询同时过滤企业、用户和会话 ID。
- 企业名称允许重复，`organization_id` 才是唯一标识；名称去除首尾空白后长度必须为 2～100 个字符。
- 新注册用户默认没有企业；用户先调用 `POST /organizations/` 创建企业并自动成为该企业 `admin`。
- Alembic 是数据库 DDL 的唯一事实来源；`SQLiteUserStore`、`SQLiteSessionStore` 和新增 Store 不再各自执行 `CREATE TABLE IF NOT EXISTS`。
- 旧数据库只有在同时具有 `users`、`conversations`、`messages` 三张现有表且不存在 `alembic_version` 时，才允许自动标记为基线版本；部分旧结构必须拒绝启动。
- 旧数据库升级时，为每个已有用户创建一个个人企业和 `admin` 成员关系。若存在找不到对应 `users.id` 的旧会话，租户范围迁移必须失败，不能猜测数据归属。
- 修改真实数据库前先备份 `.db`、`.db-wal`、`.db-shm`；计划中的自动化测试全部使用 `tmp_path` 临时数据库。
- 保持现有同步 FastAPI 和同步 `sqlite3` 风格；本计划不顺带重构 Agent Runner、消息协议或工具系统。
- 每个 Task 必须按失败测试 → 最小实现 → 局部测试 → 全量回归 → 独立提交的顺序完成。

---

## 1. 完成后的请求模型

```text
Authorization: Bearer <access-token>
X-Organization-ID: <organization-id>
```

认证和租户授权分为两个步骤：

```text
Bearer Token
  → AuthService.get_user_from_token()
  → User
  → OrganizationService.get_tenant_context(user_id, organization_id)
  → TenantContext(user_id, organization_id, role)
  → ChatService
```

`X-Organization-ID` 是“我要操作哪个企业”，不是“我有权操作这个企业”。只有
`memberships` 查询成功后才能构造 `TenantContext`。

## 2. API 契约

| 方法 | 路径 | 认证 | 租户头 | 允许角色 | 成功响应 |
|---|---|---:|---:|---|---|
| POST | `/organizations/` | 是 | 否 | 任意已登录用户 | `201 OrganizationResponse` |
| GET | `/organizations/` | 是 | 否 | 任意已登录用户 | `200 list[OrganizationAccessResponse]` |
| POST | `/organizations/{organization_id}/members/` | 是 | 否 | 该企业 `admin` | `201 MembershipResponse` |
| POST | `/conversations/` | 是 | 是 | `admin`、`agent` | `201 ConversationCreated` |
| POST | `/conversations/{conversation_id}/chat/` | 是 | 是 | `admin`、`agent` | `200 LLMResponse` |
| PUT | `/conversations/{conversation_id}/system-prompt/` | 是 | 是 | `admin`、`agent` | `200 SystemPromptUpdated` |

创建企业：

```json
{
  "name": "Acme Support"
}
```

企业列表：

```json
[
  {
    "organization_id": "f6c7...",
    "name": "Acme Support",
    "role": "admin"
  }
]
```

添加已有注册用户：

```json
{
  "username": "bob",
  "role": "agent"
}
```

固定错误映射：

| 条件 | HTTP 状态 | detail |
|---|---:|---|
| 未登录或 Token 无效 | 401 | 沿用现有认证错误 |
| 企业名称非法 | 422 | `organization name must contain 2-100 characters` |
| 缺少租户请求头 | 400 | `organization context required` |
| 当前用户不是企业成员 | 404 | `organization not found` |
| `agent` 尝试添加成员 | 403 | `admin role required` |
| 待添加用户名不存在 | 404 | `user not found` |
| 用户已经是企业成员 | 409 | `user is already an organization member` |

## 3. 数据模型与约束

```text
users
  id PK

organizations
  id PK
  name
  created_at

memberships
  organization_id FK -> organizations.id
  user_id FK -> users.id
  role CHECK IN ('admin', 'agent')
  created_at
  PK (organization_id, user_id)

conversations
  id PK
  organization_id
  user_id
  ...
  FK (organization_id, user_id)
    -> memberships(organization_id, user_id)
```

关键不变量：

1. 创建企业和创建创建者的 `admin` membership 必须在一个事务中完成。
2. 一个用户在同一个企业只能有一条 membership。
3. 没有 membership 的用户不能在该企业创建会话。
4. 会话访问必须同时匹配 `organization_id` 和 `user_id`。
5. 非成员访问真实存在的企业、会话时仍返回 404。
6. Alembic 最近一次迁移可以安全回滚，并保留迁移前已有的用户与会话。

## 4. 文件结构与职责

```text
alembic.ini
migrations/
  env.py
  script.py.mako
  versions/
    0001_baseline.py                       # 现有 users/conversations/messages 基线
    0002_organization_memberships.py       # 企业、成员、旧用户个人企业回填
    0003_conversation_tenant_scope.py      # conversations.organization_id 与回填
app/
  db/
    __init__.py
    migrations.py                          # 程序化 upgrade、旧库基线识别
  organizations/
    __init__.py
    base.py                                # 领域对象、角色、Store 协议与异常
    sqlite_store.py                        # 企业和成员 SQLite 持久化
  application/
    organization_service.py                # 企业用例、成员管理和 RBAC
    chat_service.py                        # 消费 TenantContext
  api/
    organization_router.py                 # 企业和成员 HTTP API
    dependencies.py                        # Bearer User + Header → TenantContext
    router.py                              # 会话接口消费 TenantContext
  schemas/
    organization.py                        # 企业 HTTP Schema
  sessions/
    base.py                                # Conversation 增加 organization_id
    sqlite_store.py                        # 三元范围过滤
  users/
    sqlite_store.py                        # 使用 Alembic，不再内建 DDL
tests/
  db/
    test_migrations.py
  organizations/
    test_sqlite_store.py
  application/
    test_organization_service.py
    test_chat_service.py
  api/
    test_organization_router.py
    test_dependencies.py
    test_router.py
  sessions/
    test_sqlite_store.py
  test_main.py
docs/
  database-migrations.md                   # 升级、备份、回滚和故障恢复
```

---

### Task 1: 建立 Alembic 基线并移除 Store 内建 DDL

**独立验收产物：** 空 SQLite 文件可以升级到现有三表结构；现有三表数据库可以被安全标记为基线；用户和会话 Store 不再直接执行 DDL；基线可以回滚到空数据库。

**Files:**
- Modify: `requirement.txt`
- Create: `alembic.ini`
- Create: `migrations/env.py`
- Create: `migrations/script.py.mako`
- Create: `migrations/versions/0001_baseline.py`
- Create: `app/db/__init__.py`
- Create: `app/db/migrations.py`
- Modify: `app/users/sqlite_store.py`
- Modify: `app/sessions/sqlite_store.py`
- Create: `tests/db/test_migrations.py`

**Interfaces:**
- Produces: `DatabaseMigrationError(RuntimeError)`
- Produces: `upgrade_database(database_path: str | Path) -> None`
- Produces: Alembic revision `0001_baseline`
- Consumed by: all SQLite Store constructors and later migration tests

- [ ] **Step 1: 增加 Alembic 依赖**

在 `requirement.txt` 末尾增加：

```text
SQLAlchemy>=2.0,<3.0
alembic>=1.14,<2.0
```

安装并确认命令可用：

```powershell
python -m pip install -r requirement.txt
python -m alembic --version
```

Expected: 输出 `alembic 1.x`。

- [ ] **Step 2: 写迁移失败测试**

创建 `tests/db/test_migrations.py`：

```python
import sqlite3

import pytest
from alembic import command
from alembic.config import Config

from app.db.migrations import DatabaseMigrationError, upgrade_database


BUSINESS_BASELINE_TABLES = {"users", "conversations", "messages"}
EXPECTED_BASELINE_TABLES = BUSINESS_BASELINE_TABLES | {"alembic_version"}


def table_names(database_path):
    with sqlite3.connect(database_path) as connection:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }


def alembic_config(database_path):
    config = Config("alembic.ini")
    config.attributes["database_path"] = database_path
    return config


def create_legacy_schema(database_path):
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE conversations (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                system_prompt TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                role TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(conversation_id) REFERENCES conversations(id),
                UNIQUE(conversation_id, seq)
            );
            """
        )


def test_fresh_database_upgrades_to_baseline(tmp_path):
    database_path = tmp_path / "fresh.db"

    upgrade_database(database_path)

    assert EXPECTED_BASELINE_TABLES <= table_names(database_path)


def test_existing_current_schema_is_stamped_without_data_loss(tmp_path):
    database_path = tmp_path / "legacy.db"
    create_legacy_schema(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO users(id, username, password_hash)
            VALUES ('user-1', 'alice', 'hash')
            """
        )

    upgrade_database(database_path)

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT id, username FROM users"
        ).fetchone()
        revision = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0]

    assert row == ("user-1", "alice")
    assert revision is not None


def test_partial_legacy_schema_is_rejected(tmp_path):
    database_path = tmp_path / "partial.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE users(id TEXT PRIMARY KEY)")

    with pytest.raises(DatabaseMigrationError):
        upgrade_database(database_path)


def test_complete_table_names_with_wrong_columns_are_rejected(tmp_path):
    database_path = tmp_path / "wrong-columns.db"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE users(id TEXT PRIMARY KEY);
            CREATE TABLE conversations(id TEXT PRIMARY KEY);
            CREATE TABLE messages(id INTEGER PRIMARY KEY);
            """
        )

    with pytest.raises(DatabaseMigrationError):
        upgrade_database(database_path)


def test_baseline_downgrades_to_empty_database(tmp_path):
    database_path = tmp_path / "rollback.db"
    upgrade_database(database_path)

    command.downgrade(alembic_config(database_path), "base")

    assert not BUSINESS_BASELINE_TABLES.intersection(
        table_names(database_path)
    )
```

- [ ] **Step 3: 运行测试并确认失败**

Run:

```powershell
python -m pytest tests/db/test_migrations.py -q
```

Expected: collection fails because `app.db.migrations` does not exist。

- [ ] **Step 4: 创建 Alembic 配置**

创建 `alembic.ini`：

```ini
[alembic]
script_location = %(here)s/migrations
prepend_sys_path = .
sqlalchemy.url = sqlite:///chat_history.db

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARN
handlers = console
qualname =

[logger_sqlalchemy]
level = WARN
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
datefmt = %H:%M:%S
```

创建 `migrations/env.py`：

```python
from logging.config import fileConfig
import os
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None


def database_url() -> str:
    injected_path = config.attributes.get("database_path")
    if injected_path is not None:
        return f"sqlite:///{Path(injected_path).resolve().as_posix()}"

    env_path = os.getenv("CHAT_DB_PATH")
    if env_path:
        return f"sqlite:///{Path(env_path).resolve().as_posix()}"

    return config.get_main_option("sqlalchemy.url")


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = database_url()
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

创建 `migrations/script.py.mako`：

```mako
"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision: str = ${repr(up_revision)}
down_revision: Union[str, None] = ${repr(down_revision)}
branch_labels: Union[str, Sequence[str], None] = ${repr(branch_labels)}
depends_on: Union[str, Sequence[str], None] = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
```

- [ ] **Step 5: 创建现有数据库基线迁移**

创建 `migrations/versions/0001_baseline.py`：

```python
from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("username", sa.Text(collation="NOCASE"), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint("username"),
    )
    op.create_index(
        "idx_users_username",
        "users",
        ["username"],
        unique=True,
    )

    op.create_table(
        "conversations",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index(
        "idx_conversations_user",
        "conversations",
        ["user_id", "updated_at"],
        unique=False,
    )

    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("conversation_id", sa.Text(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
        ),
        sa.UniqueConstraint(
            "conversation_id",
            "seq",
            name="uq_messages_conversation_seq",
        ),
    )
    op.create_index(
        "idx_messages_conversation_seq",
        "messages",
        ["conversation_id", "seq"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_messages_conversation_seq", table_name="messages")
    op.drop_table("messages")
    op.drop_index("idx_conversations_user", table_name="conversations")
    op.drop_table("conversations")
    op.drop_index("idx_users_username", table_name="users")
    op.drop_table("users")
```

- [ ] **Step 6: 实现升级入口和旧数据库识别**

创建空文件 `app/db/__init__.py`，创建 `app/db/migrations.py`：

```python
import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BASELINE_REVISION = "0001_baseline"
BASELINE_TABLES = {"users", "conversations", "messages"}
BASELINE_COLUMNS = {
    "users": {
        "id",
        "username",
        "password_hash",
        "created_at",
    },
    "conversations": {
        "id",
        "user_id",
        "system_prompt",
        "created_at",
        "updated_at",
    },
    "messages": {
        "id",
        "conversation_id",
        "seq",
        "role",
        "payload_json",
        "created_at",
    },
}


class DatabaseMigrationError(RuntimeError):
    pass


def _table_names(database_path: Path) -> set[str]:
    if not database_path.exists():
        return set()
    with sqlite3.connect(database_path) as connection:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }


def _config(database_path: Path) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["database_path"] = database_path
    return config


def _validate_legacy_schema(database_path: Path) -> None:
    with sqlite3.connect(database_path) as connection:
        for table_name, expected_columns in BASELINE_COLUMNS.items():
            actual_columns = {
                row[1]
                for row in connection.execute(
                    f"PRAGMA table_info({table_name})"
                ).fetchall()
            }
            if actual_columns != expected_columns:
                raise DatabaseMigrationError(
                    f"legacy table {table_name} does not match baseline"
                )


def upgrade_database(database_path: str | Path) -> None:
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tables = _table_names(path)
    has_version_table = "alembic_version" in tables
    business_tables = tables.intersection(BASELINE_TABLES)

    if not has_version_table and business_tables:
        if business_tables != BASELINE_TABLES:
            raise DatabaseMigrationError(
                "partial legacy schema detected; restore a complete backup"
            )
        _validate_legacy_schema(path)
        command.stamp(_config(path), BASELINE_REVISION)

    command.upgrade(_config(path), "head")
```

- [ ] **Step 7: 让 SQLite Store 使用 Alembic**

在 `app/users/sqlite_store.py` 和 `app/sessions/sqlite_store.py` 中导入：

```python
from app.db.migrations import upgrade_database
```

把两个 Store 构造函数中的：

```python
self._initialize()
```

替换为：

```python
upgrade_database(self._database_path)
```

删除两个文件中的 `_initialize()` 方法及其中所有 `CREATE TABLE`、`CREATE INDEX`
脚本。不要修改 CRUD SQL。

- [ ] **Step 8: 运行局部测试与现有 Store 回归**

Run:

```powershell
python -m pytest tests/db/test_migrations.py tests/users/test_sqlite_user_store.py tests/sessions/test_sqlite_store.py -q
```

Expected: 全部 PASS；`test_existing_current_schema_is_stamped_without_data_loss`
确认旧数据未丢失。

- [ ] **Step 9: 运行全量回归**

Run:

```powershell
python -m pytest -q
```

Expected: 当前全量测试全部 PASS。

- [ ] **Step 10: 提交**

```powershell
git add requirement.txt alembic.ini migrations app/db app/users/sqlite_store.py app/sessions/sqlite_store.py tests/db
git commit -m "build: manage sqlite schema with alembic"
```

---

### Task 2: 增加企业和成员关系迁移

**独立验收产物：** 数据库拥有 `organizations`、`memberships`；角色受数据库约束；旧数据库中的每个已有用户会得到一个个人企业和 `admin` membership；回滚后用户和会话仍存在。

**Files:**
- Create: `migrations/versions/0002_organization_memberships.py`
- Modify: `tests/db/test_migrations.py`

**Interfaces:**
- Consumes: `0001_baseline`
- Produces: Alembic revision `0002_organization_memberships`
- Produces tables: `organizations`、`memberships`
- Produces roles: exactly `admin`、`agent`

- [ ] **Step 1: 扩展迁移失败测试**

在 `tests/db/test_migrations.py` 增加：

```python
def test_upgrade_creates_organization_and_membership_schema(tmp_path):
    database_path = tmp_path / "tenant-schema.db"

    upgrade_database(database_path)

    assert {"organizations", "memberships"} <= table_names(database_path)
    with sqlite3.connect(database_path) as connection:
        membership_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(memberships)"
            ).fetchall()
        }

    assert membership_columns == {
        "organization_id",
        "user_id",
        "role",
        "created_at",
    }


def test_existing_users_receive_personal_admin_memberships(tmp_path):
    database_path = tmp_path / "legacy-users.db"
    create_legacy_schema(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO users(id, username, password_hash)
            VALUES ('user-1', 'alice', 'hash')
            """
        )

    upgrade_database(database_path)

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            """
            SELECT o.name, m.user_id, m.role
            FROM memberships AS m
            JOIN organizations AS o ON o.id = m.organization_id
            """
        ).fetchone()

    assert row == ("alice organization", "user-1", "admin")


def test_latest_tenant_migration_can_rollback_without_losing_users(tmp_path):
    database_path = tmp_path / "tenant-rollback.db"
    upgrade_database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO users(id, username, password_hash)
            VALUES ('user-1', 'alice', 'hash')
            """
        )

    command.downgrade(
        alembic_config(database_path),
        "0001_baseline",
    )

    assert "organizations" not in table_names(database_path)
    assert "memberships" not in table_names(database_path)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT username FROM users WHERE id = 'user-1'"
        ).fetchone() == ("alice",)
```

- [ ] **Step 2: 运行新测试并确认失败**

Run:

```powershell
python -m pytest tests/db/test_migrations.py -q
```

Expected: FAIL because `organizations` and `memberships` do not exist。

- [ ] **Step 3: 创建企业和成员迁移**

创建 `migrations/versions/0002_organization_memberships.py`：

```python
from typing import Sequence
from uuid import uuid4

from alembic import op
import sqlalchemy as sa


revision: str = "0002_organization_memberships"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_table(
        "memberships",
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "role IN ('admin', 'agent')",
            name="ck_memberships_role",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("organization_id", "user_id"),
    )
    op.create_index(
        "idx_memberships_user",
        "memberships",
        ["user_id", "organization_id"],
        unique=False,
    )

    connection = op.get_bind()
    users = connection.execute(
        sa.text("SELECT id, username FROM users ORDER BY id")
    ).mappings()
    for user in users:
        organization_id = str(uuid4())
        connection.execute(
            sa.text(
                """
                INSERT INTO organizations(id, name)
                VALUES (:organization_id, :name)
                """
            ),
            {
                "organization_id": organization_id,
                "name": f"{user['username']} organization",
            },
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO memberships(organization_id, user_id, role)
                VALUES (:organization_id, :user_id, 'admin')
                """
            ),
            {
                "organization_id": organization_id,
                "user_id": user["id"],
            },
        )


def downgrade() -> None:
    op.drop_index("idx_memberships_user", table_name="memberships")
    op.drop_table("memberships")
    op.drop_table("organizations")
```

- [ ] **Step 4: 验证数据库约束**

在 `tests/db/test_migrations.py` 增加：

```python
def test_membership_rejects_unknown_role_and_duplicate_user(tmp_path):
    database_path = tmp_path / "constraints.db"
    upgrade_database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO users(id, username, password_hash)
            VALUES ('user-1', 'alice', 'hash')
            """
        )
        connection.execute(
            """
            INSERT INTO organizations(id, name)
            VALUES ('org-1', 'Acme')
            """
        )
        connection.execute(
            """
            INSERT INTO memberships(organization_id, user_id, role)
            VALUES ('org-1', 'user-1', 'admin')
            """
        )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO memberships(organization_id, user_id, role)
                VALUES ('org-1', 'user-1', 'agent')
                """
            )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO memberships(organization_id, user_id, role)
                VALUES ('org-1', 'missing-user', 'owner')
                """
            )
```

注意：测试连接必须先执行：

```python
connection.execute("PRAGMA foreign_keys = ON")
```

将这行放在第一条 `INSERT` 之前，确保外键约束在测试连接中生效。

- [ ] **Step 5: 运行迁移测试和全量回归**

Run:

```powershell
python -m pytest tests/db/test_migrations.py -q
python -m pytest -q
```

Expected: 两条命令全部 PASS。

- [ ] **Step 6: 提交**

```powershell
git add migrations/versions/0002_organization_memberships.py tests/db/test_migrations.py
git commit -m "feat: add organization membership schema"
```

---

### Task 3: 实现企业与成员 Store

**独立验收产物：** 可以原子创建企业及首位管理员、读取用户可访问的企业、读取成员关系、添加成员，并能区分成员不存在和重复成员。

**Files:**
- Create: `app/organizations/__init__.py`
- Create: `app/organizations/base.py`
- Create: `app/organizations/sqlite_store.py`
- Create: `tests/organizations/test_sqlite_store.py`

**Interfaces:**
- Produces: `MembershipRole.ADMIN`、`MembershipRole.AGENT`
- Produces: `Organization`
- Produces: `Membership`
- Produces: `OrganizationAccess`
- Produces: `OrganizationStore.create_with_admin(...)`
- Produces: `OrganizationStore.list_for_user(...)`
- Produces: `OrganizationStore.get_membership(...)`
- Produces: `OrganizationStore.add_membership(...)`
- Produces: `MembershipNotFoundError`、`MembershipAlreadyExistsError`

- [ ] **Step 1: 写 Store 失败测试**

创建 `tests/organizations/test_sqlite_store.py`：

```python
import pytest

from app.organizations.base import (
    MembershipAlreadyExistsError,
    MembershipNotFoundError,
    MembershipRole,
)
from app.organizations.sqlite_store import SQLiteOrganizationStore
from app.users.sqlite_store import SQLiteUserStore


def build_stores(tmp_path):
    database_path = tmp_path / "app.db"
    return (
        SQLiteUserStore(database_path),
        SQLiteOrganizationStore(database_path),
    )


def test_create_organization_also_creates_admin_membership(tmp_path):
    users, organizations = build_stores(tmp_path)
    alice = users.create_user(username="alice", password_hash="hash")

    organization = organizations.create_with_admin(
        name="Acme Support",
        admin_user_id=alice.user_id,
    )

    membership = organizations.get_membership(
        organization_id=organization.organization_id,
        user_id=alice.user_id,
    )
    assert organization.name == "Acme Support"
    assert membership.role is MembershipRole.ADMIN


def test_list_for_user_returns_only_joined_organizations(tmp_path):
    users, organizations = build_stores(tmp_path)
    alice = users.create_user(username="alice", password_hash="hash")
    bob = users.create_user(username="bob", password_hash="hash")
    first = organizations.create_with_admin(
        name="Alice Company",
        admin_user_id=alice.user_id,
    )
    organizations.create_with_admin(
        name="Bob Company",
        admin_user_id=bob.user_id,
    )

    result = organizations.list_for_user(user_id=alice.user_id)

    assert [(item.organization_id, item.name, item.role) for item in result] == [
        (first.organization_id, "Alice Company", MembershipRole.ADMIN)
    ]


def test_add_membership_round_trip_and_duplicate_rejected(tmp_path):
    users, organizations = build_stores(tmp_path)
    alice = users.create_user(username="alice", password_hash="hash")
    bob = users.create_user(username="bob", password_hash="hash")
    organization = organizations.create_with_admin(
        name="Acme",
        admin_user_id=alice.user_id,
    )

    created = organizations.add_membership(
        organization_id=organization.organization_id,
        user_id=bob.user_id,
        role=MembershipRole.AGENT,
    )

    assert organizations.get_membership(
        organization_id=organization.organization_id,
        user_id=bob.user_id,
    ) == created
    with pytest.raises(MembershipAlreadyExistsError):
        organizations.add_membership(
            organization_id=organization.organization_id,
            user_id=bob.user_id,
            role=MembershipRole.ADMIN,
        )


def test_missing_membership_raises_domain_error(tmp_path):
    users, organizations = build_stores(tmp_path)
    alice = users.create_user(username="alice", password_hash="hash")
    bob = users.create_user(username="bob", password_hash="hash")
    organization = organizations.create_with_admin(
        name="Acme",
        admin_user_id=alice.user_id,
    )

    with pytest.raises(MembershipNotFoundError):
        organizations.get_membership(
            organization_id=organization.organization_id,
            user_id=bob.user_id,
        )
```

- [ ] **Step 2: 运行测试并确认失败**

Run:

```powershell
python -m pytest tests/organizations/test_sqlite_store.py -q
```

Expected: collection fails because `app.organizations` does not exist。

- [ ] **Step 3: 定义企业领域对象和 Store 协议**

创建空文件 `app/organizations/__init__.py`，创建 `app/organizations/base.py`：

```python
from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class MembershipRole(str, Enum):
    ADMIN = "admin"
    AGENT = "agent"


@dataclass(frozen=True)
class Organization:
    organization_id: str
    name: str
    created_at: str


@dataclass(frozen=True)
class Membership:
    organization_id: str
    user_id: str
    role: MembershipRole
    created_at: str


@dataclass(frozen=True)
class OrganizationAccess:
    organization_id: str
    name: str
    role: MembershipRole


class MembershipNotFoundError(LookupError):
    pass


class MembershipAlreadyExistsError(ValueError):
    pass


class OrganizationStore(Protocol):
    def create_with_admin(
        self,
        *,
        name: str,
        admin_user_id: str,
    ) -> Organization:
        raise NotImplementedError

    def list_for_user(self, *, user_id: str) -> list[OrganizationAccess]:
        raise NotImplementedError

    def get_membership(
        self,
        *,
        organization_id: str,
        user_id: str,
    ) -> Membership:
        raise NotImplementedError

    def add_membership(
        self,
        *,
        organization_id: str,
        user_id: str,
        role: MembershipRole,
    ) -> Membership:
        raise NotImplementedError
```

- [ ] **Step 4: 实现 SQLiteOrganizationStore**

创建 `app/organizations/sqlite_store.py`：

```python
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from app.db.migrations import upgrade_database
from app.organizations.base import (
    Membership,
    MembershipAlreadyExistsError,
    MembershipNotFoundError,
    MembershipRole,
    Organization,
    OrganizationAccess,
    OrganizationStore,
)


class SQLiteOrganizationStore(OrganizationStore):
    def __init__(self, database_path: str | Path):
        self._database_path = database_path
        upgrade_database(database_path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _to_membership(row: sqlite3.Row) -> Membership:
        return Membership(
            organization_id=row["organization_id"],
            user_id=row["user_id"],
            role=MembershipRole(row["role"]),
            created_at=row["created_at"],
        )

    def create_with_admin(
        self,
        *,
        name: str,
        admin_user_id: str,
    ) -> Organization:
        organization_id = str(uuid4())
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO organizations(id, name)
                VALUES (?, ?)
                """,
                (organization_id, name),
            )
            connection.execute(
                """
                INSERT INTO memberships(organization_id, user_id, role)
                VALUES (?, ?, ?)
                """,
                (
                    organization_id,
                    admin_user_id,
                    MembershipRole.ADMIN.value,
                ),
            )
            row = connection.execute(
                """
                SELECT id, name, created_at
                FROM organizations
                WHERE id = ?
                """,
                (organization_id,),
            ).fetchone()

        if row is None:
            raise RuntimeError("created organization could not be reloaded")
        return Organization(
            organization_id=row["id"],
            name=row["name"],
            created_at=row["created_at"],
        )

    def list_for_user(self, *, user_id: str) -> list[OrganizationAccess]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT o.id, o.name, m.role
                FROM memberships AS m
                JOIN organizations AS o ON o.id = m.organization_id
                WHERE m.user_id = ?
                ORDER BY o.created_at, o.id
                """,
                (user_id,),
            ).fetchall()
        return [
            OrganizationAccess(
                organization_id=row["id"],
                name=row["name"],
                role=MembershipRole(row["role"]),
            )
            for row in rows
        ]

    def get_membership(
        self,
        *,
        organization_id: str,
        user_id: str,
    ) -> Membership:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT organization_id, user_id, role, created_at
                FROM memberships
                WHERE organization_id = ? AND user_id = ?
                """,
                (organization_id, user_id),
            ).fetchone()
        if row is None:
            raise MembershipNotFoundError("membership not found")
        return self._to_membership(row)

    def add_membership(
        self,
        *,
        organization_id: str,
        user_id: str,
        role: MembershipRole,
    ) -> Membership:
        try:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO memberships(organization_id, user_id, role)
                    VALUES (?, ?, ?)
                    """,
                    (organization_id, user_id, role.value),
                )
        except sqlite3.IntegrityError as exc:
            raise MembershipAlreadyExistsError(
                "membership already exists"
            ) from exc
        return self.get_membership(
            organization_id=organization_id,
            user_id=user_id,
        )
```

- [ ] **Step 5: 运行局部测试和全量回归**

Run:

```powershell
python -m pytest tests/organizations/test_sqlite_store.py -q
python -m pytest -q
```

Expected: 两条命令全部 PASS。

- [ ] **Step 6: 提交**

```powershell
git add app/organizations tests/organizations
git commit -m "feat: add organization membership store"
```

---

### Task 4: 实现企业用例和最小 RBAC

**独立验收产物：** 应用服务可以校验企业名称、创建企业、列出当前用户的企业、构造可信 `TenantContext`；只有企业管理员可以添加已有注册用户。

**Files:**
- Create: `app/application/organization_service.py`
- Create: `tests/application/test_organization_service.py`

**Interfaces:**
- Produces: `TenantContext(user_id: str, organization_id: str, role: MembershipRole)`
- Produces: `OrganizationService.create_organization(...)`
- Produces: `OrganizationService.list_organizations(...)`
- Produces: `OrganizationService.get_tenant_context(...)`
- Produces: `OrganizationService.add_member(...)`
- Produces: `InvalidOrganizationNameError`、`OrganizationAccessDeniedError`、`AdminRoleRequiredError`

- [ ] **Step 1: 写应用服务失败测试**

创建 `tests/application/test_organization_service.py`：

```python
import pytest

from app.application.organization_service import (
    AdminRoleRequiredError,
    InvalidOrganizationNameError,
    OrganizationAccessDeniedError,
    OrganizationService,
)
from app.organizations.base import MembershipRole
from app.organizations.sqlite_store import SQLiteOrganizationStore
from app.users.sqlite_store import SQLiteUserStore


def build_service(tmp_path):
    database_path = tmp_path / "app.db"
    users = SQLiteUserStore(database_path)
    organizations = SQLiteOrganizationStore(database_path)
    return OrganizationService(
        organization_store=organizations,
        user_store=users,
    ), users


def test_create_normalizes_name_and_grants_admin_role(tmp_path):
    service, users = build_service(tmp_path)
    alice = users.create_user(username="alice", password_hash="hash")

    organization = service.create_organization(
        user_id=alice.user_id,
        name="  Acme Support  ",
    )
    context = service.get_tenant_context(
        user_id=alice.user_id,
        organization_id=organization.organization_id,
    )

    assert organization.name == "Acme Support"
    assert context.role is MembershipRole.ADMIN


@pytest.mark.parametrize("name", [" ", "A", "x" * 101])
def test_create_rejects_invalid_name(tmp_path, name):
    service, users = build_service(tmp_path)
    alice = users.create_user(username="alice", password_hash="hash")

    with pytest.raises(InvalidOrganizationNameError):
        service.create_organization(user_id=alice.user_id, name=name)


def test_non_member_cannot_obtain_tenant_context(tmp_path):
    service, users = build_service(tmp_path)
    alice = users.create_user(username="alice", password_hash="hash")
    bob = users.create_user(username="bob", password_hash="hash")
    organization = service.create_organization(
        user_id=alice.user_id,
        name="Acme",
    )

    with pytest.raises(OrganizationAccessDeniedError):
        service.get_tenant_context(
            user_id=bob.user_id,
            organization_id=organization.organization_id,
        )


def test_admin_can_add_existing_user_as_agent(tmp_path):
    service, users = build_service(tmp_path)
    alice = users.create_user(username="alice", password_hash="hash")
    bob = users.create_user(username="bob", password_hash="hash")
    organization = service.create_organization(
        user_id=alice.user_id,
        name="Acme",
    )

    membership = service.add_member(
        actor_user_id=alice.user_id,
        organization_id=organization.organization_id,
        username=bob.username,
        role=MembershipRole.AGENT,
    )

    assert membership.user_id == bob.user_id
    assert membership.role is MembershipRole.AGENT


def test_agent_cannot_add_another_member(tmp_path):
    service, users = build_service(tmp_path)
    alice = users.create_user(username="alice", password_hash="hash")
    bob = users.create_user(username="bob", password_hash="hash")
    charlie = users.create_user(username="charlie", password_hash="hash")
    organization = service.create_organization(
        user_id=alice.user_id,
        name="Acme",
    )
    service.add_member(
        actor_user_id=alice.user_id,
        organization_id=organization.organization_id,
        username=bob.username,
        role=MembershipRole.AGENT,
    )

    with pytest.raises(AdminRoleRequiredError):
        service.add_member(
            actor_user_id=bob.user_id,
            organization_id=organization.organization_id,
            username=charlie.username,
            role=MembershipRole.AGENT,
        )
```

- [ ] **Step 2: 运行测试并确认失败**

Run:

```powershell
python -m pytest tests/application/test_organization_service.py -q
```

Expected: collection fails because `organization_service` does not exist。

- [ ] **Step 3: 实现企业应用服务**

创建 `app/application/organization_service.py`：

```python
from dataclasses import dataclass

from app.organizations.base import (
    Membership,
    MembershipNotFoundError,
    MembershipRole,
    Organization,
    OrganizationAccess,
    OrganizationStore,
)
from app.users.base import UserStore


MIN_ORGANIZATION_NAME_LENGTH = 2
MAX_ORGANIZATION_NAME_LENGTH = 100


@dataclass(frozen=True)
class TenantContext:
    user_id: str
    organization_id: str
    role: MembershipRole


class InvalidOrganizationNameError(ValueError):
    pass


class OrganizationAccessDeniedError(LookupError):
    pass


class AdminRoleRequiredError(PermissionError):
    pass


class OrganizationService:
    def __init__(
        self,
        *,
        organization_store: OrganizationStore,
        user_store: UserStore,
    ):
        self._organization_store = organization_store
        self._user_store = user_store

    @staticmethod
    def _normalize_name(name: str) -> str:
        normalized = name.strip()
        if not (
            MIN_ORGANIZATION_NAME_LENGTH
            <= len(normalized)
            <= MAX_ORGANIZATION_NAME_LENGTH
        ):
            raise InvalidOrganizationNameError(
                "organization name must contain 2-100 characters"
            )
        return normalized

    def create_organization(
        self,
        *,
        user_id: str,
        name: str,
    ) -> Organization:
        return self._organization_store.create_with_admin(
            name=self._normalize_name(name),
            admin_user_id=user_id,
        )

    def list_organizations(
        self,
        *,
        user_id: str,
    ) -> list[OrganizationAccess]:
        return self._organization_store.list_for_user(user_id=user_id)

    def get_tenant_context(
        self,
        *,
        user_id: str,
        organization_id: str,
    ) -> TenantContext:
        try:
            membership = self._organization_store.get_membership(
                organization_id=organization_id,
                user_id=user_id,
            )
        except MembershipNotFoundError as exc:
            raise OrganizationAccessDeniedError(
                "organization not found"
            ) from exc
        return TenantContext(
            user_id=user_id,
            organization_id=organization_id,
            role=membership.role,
        )

    def add_member(
        self,
        *,
        actor_user_id: str,
        organization_id: str,
        username: str,
        role: MembershipRole,
    ) -> Membership:
        context = self.get_tenant_context(
            user_id=actor_user_id,
            organization_id=organization_id,
        )
        if context.role is not MembershipRole.ADMIN:
            raise AdminRoleRequiredError("admin role required")

        target_user = self._user_store.get_by_username(
            username=username.strip().casefold()
        )
        return self._organization_store.add_membership(
            organization_id=organization_id,
            user_id=target_user.user_id,
            role=role,
        )
```

- [ ] **Step 4: 增加非成员管理员操作测试**

在 `tests/application/test_organization_service.py` 增加：

```python
def test_non_member_add_member_hides_organization(tmp_path):
    service, users = build_service(tmp_path)
    alice = users.create_user(username="alice", password_hash="hash")
    bob = users.create_user(username="bob", password_hash="hash")
    organization = service.create_organization(
        user_id=alice.user_id,
        name="Acme",
    )

    with pytest.raises(OrganizationAccessDeniedError):
        service.add_member(
            actor_user_id=bob.user_id,
            organization_id=organization.organization_id,
            username=alice.username,
            role=MembershipRole.AGENT,
        )
```

- [ ] **Step 5: 运行局部测试和全量回归**

Run:

```powershell
python -m pytest tests/application/test_organization_service.py -q
python -m pytest -q
```

Expected: 两条命令全部 PASS。

- [ ] **Step 6: 提交**

```powershell
git add app/application/organization_service.py tests/application/test_organization_service.py
git commit -m "feat: add organization service and minimal rbac"
```

---

### Task 5: 暴露企业和成员管理 API

**独立验收产物：** 已登录用户可以创建和列出企业；企业管理员可以添加成员；非管理员、重复成员和未知用户名得到固定 HTTP 错误。

**Files:**
- Create: `app/schemas/organization.py`
- Create: `app/api/organization_router.py`
- Create: `tests/api/test_organization_router.py`

**Interfaces:**
- Consumes: `OrganizationService`
- Consumes: existing `get_current_user() -> User`
- Produces: `create_organization_router(...) -> APIRouter`
- Produces API: `POST /organizations/`
- Produces API: `GET /organizations/`
- Produces API: `POST /organizations/{organization_id}/members/`

- [ ] **Step 1: 写 Router 失败测试**

创建 `tests/api/test_organization_router.py`：

```python
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.organization_router import create_organization_router
from app.application.organization_service import OrganizationService
from app.organizations.base import MembershipRole
from app.organizations.sqlite_store import SQLiteOrganizationStore
from app.users.base import User
from app.users.sqlite_store import SQLiteUserStore


CURRENT_USER = User(
    user_id="authenticated-user",
    username="alice",
    password_hash="hash",
    created_at="2026-07-26 00:00:00",
)


def build_client(tmp_path):
    database_path = tmp_path / "app.db"
    users = SQLiteUserStore(database_path)
    users.create_user(
        username=CURRENT_USER.username,
        password_hash=CURRENT_USER.password_hash,
    )
    stored_alice = users.get_by_username(username="alice")
    current_user = User(
        user_id=stored_alice.user_id,
        username=stored_alice.username,
        password_hash=stored_alice.password_hash,
        created_at=stored_alice.created_at,
    )
    service = OrganizationService(
        organization_store=SQLiteOrganizationStore(database_path),
        user_store=users,
    )

    def get_current_user():
        return current_user

    app = FastAPI()
    app.include_router(
        create_organization_router(
            organization_service=service,
            get_current_user=get_current_user,
        )
    )
    return TestClient(app), service, users, current_user


def test_create_and_list_organizations(tmp_path):
    client, _, _, _ = build_client(tmp_path)

    created = client.post(
        "/organizations/",
        json={"name": "  Acme Support  "},
    )
    listed = client.get("/organizations/")

    assert created.status_code == 201
    assert created.json()["name"] == "Acme Support"
    assert listed.status_code == 200
    assert listed.json() == [
        {
            "organization_id": created.json()["organization_id"],
            "name": "Acme Support",
            "role": "admin",
        }
    ]


def test_admin_can_add_member_and_duplicate_returns_409(tmp_path):
    client, _, users, _ = build_client(tmp_path)
    users.create_user(username="bob", password_hash="hash")
    organization_id = client.post(
        "/organizations/",
        json={"name": "Acme"},
    ).json()["organization_id"]

    first = client.post(
        f"/organizations/{organization_id}/members/",
        json={"username": "bob", "role": "agent"},
    )
    duplicate = client.post(
        f"/organizations/{organization_id}/members/",
        json={"username": "bob", "role": "agent"},
    )

    assert first.status_code == 201
    assert first.json()["role"] == "agent"
    assert duplicate.status_code == 409
    assert duplicate.json() == {
        "detail": "user is already an organization member"
    }


def test_unknown_member_username_returns_404(tmp_path):
    client, _, _, _ = build_client(tmp_path)
    organization_id = client.post(
        "/organizations/",
        json={"name": "Acme"},
    ).json()["organization_id"]

    response = client.post(
        f"/organizations/{organization_id}/members/",
        json={"username": "missing", "role": "agent"},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "user not found"}
```

- [ ] **Step 2: 运行测试并确认失败**

Run:

```powershell
python -m pytest tests/api/test_organization_router.py -q
```

Expected: collection fails because organization schemas and router do not exist。

- [ ] **Step 3: 创建 HTTP Schema**

创建 `app/schemas/organization.py`：

```python
from pydantic import BaseModel, ConfigDict, Field

from app.organizations.base import (
    Membership,
    MembershipRole,
    Organization,
    OrganizationAccess,
)


class CreateOrganizationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)


class AddMemberRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=3, max_length=32)
    role: MembershipRole


class OrganizationResponse(BaseModel):
    organization_id: str
    name: str

    @classmethod
    def from_domain(
        cls,
        organization: Organization,
    ) -> "OrganizationResponse":
        return cls(
            organization_id=organization.organization_id,
            name=organization.name,
        )


class OrganizationAccessResponse(BaseModel):
    organization_id: str
    name: str
    role: MembershipRole

    @classmethod
    def from_domain(
        cls,
        access: OrganizationAccess,
    ) -> "OrganizationAccessResponse":
        return cls(
            organization_id=access.organization_id,
            name=access.name,
            role=access.role,
        )


class MembershipResponse(BaseModel):
    organization_id: str
    user_id: str
    role: MembershipRole

    @classmethod
    def from_domain(
        cls,
        membership: Membership,
    ) -> "MembershipResponse":
        return cls(
            organization_id=membership.organization_id,
            user_id=membership.user_id,
            role=membership.role,
        )
```

- [ ] **Step 4: 实现企业 Router 和固定错误映射**

创建 `app/api/organization_router.py`：

```python
from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException

from app.application.organization_service import (
    AdminRoleRequiredError,
    InvalidOrganizationNameError,
    OrganizationAccessDeniedError,
    OrganizationService,
)
from app.organizations.base import MembershipAlreadyExistsError
from app.schemas.organization import (
    AddMemberRequest,
    CreateOrganizationRequest,
    MembershipResponse,
    OrganizationAccessResponse,
    OrganizationResponse,
)
from app.users.base import User, UserNotFoundError


def create_organization_router(
    *,
    organization_service: OrganizationService,
    get_current_user: Callable[..., User],
) -> APIRouter:
    router = APIRouter(
        prefix="/organizations",
        tags=["organizations"],
    )

    @router.post("/", response_model=OrganizationResponse, status_code=201)
    def create_organization(
        request: CreateOrganizationRequest,
        current_user: User = Depends(get_current_user),
    ) -> OrganizationResponse:
        try:
            organization = organization_service.create_organization(
                user_id=current_user.user_id,
                name=request.name,
            )
        except InvalidOrganizationNameError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return OrganizationResponse.from_domain(organization)

    @router.get("/", response_model=list[OrganizationAccessResponse])
    def list_organizations(
        current_user: User = Depends(get_current_user),
    ) -> list[OrganizationAccessResponse]:
        result = organization_service.list_organizations(
            user_id=current_user.user_id
        )
        return [
            OrganizationAccessResponse.from_domain(item)
            for item in result
        ]

    @router.post(
        "/{organization_id}/members/",
        response_model=MembershipResponse,
        status_code=201,
    )
    def add_member(
        organization_id: str,
        request: AddMemberRequest,
        current_user: User = Depends(get_current_user),
    ) -> MembershipResponse:
        try:
            membership = organization_service.add_member(
                actor_user_id=current_user.user_id,
                organization_id=organization_id,
                username=request.username,
                role=request.role,
            )
        except OrganizationAccessDeniedError as exc:
            raise HTTPException(
                status_code=404,
                detail="organization not found",
            ) from exc
        except AdminRoleRequiredError as exc:
            raise HTTPException(
                status_code=403,
                detail="admin role required",
            ) from exc
        except UserNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail="user not found",
            ) from exc
        except MembershipAlreadyExistsError as exc:
            raise HTTPException(
                status_code=409,
                detail="user is already an organization member",
            ) from exc
        return MembershipResponse.from_domain(membership)

    return router
```

- [ ] **Step 5: 增加 agent 禁止添加成员的 Router 测试**

在 `tests/api/test_organization_router.py` 增加一个独立的真实服务测试：

```python
def test_agent_cannot_add_member(tmp_path):
    client, service, users, alice = build_client(tmp_path)
    bob = users.create_user(username="bob", password_hash="hash")
    users.create_user(username="charlie", password_hash="hash")
    organization = service.create_organization(
        user_id=alice.user_id,
        name="Acme",
    )
    service.add_member(
        actor_user_id=alice.user_id,
        organization_id=organization.organization_id,
        username=bob.username,
        role=MembershipRole.AGENT,
    )

    app = FastAPI()

    def get_bob():
        return bob

    app.include_router(
        create_organization_router(
            organization_service=service,
            get_current_user=get_bob,
        )
    )
    response = TestClient(app).post(
        f"/organizations/{organization.organization_id}/members/",
        json={"username": "charlie", "role": "agent"},
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "admin role required"}
```

- [ ] **Step 6: 运行局部测试和全量回归**

Run:

```powershell
python -m pytest tests/api/test_organization_router.py -q
python -m pytest -q
```

Expected: 两条命令全部 PASS。

- [ ] **Step 7: 提交**

```powershell
git add app/schemas/organization.py app/api/organization_router.py tests/api/test_organization_router.py
git commit -m "feat: add organization management api"
```

---

### Task 6: 构造经过验证的租户上下文依赖

**独立验收产物：** FastAPI 可以把 Bearer Token 得到的 `User` 与 `X-Organization-ID` 合并为可信 `TenantContext`；缺少请求头返回 400；非成员返回 404；管理员和客服都能获得上下文。

**Files:**
- Modify: `app/api/dependencies.py`
- Create: `tests/api/test_dependencies.py`

**Interfaces:**
- Consumes: `OrganizationService.get_tenant_context(...)`
- Consumes: existing `get_current_user() -> User`
- Produces: `create_current_tenant_dependency(...) -> Callable[..., TenantContext]`

- [ ] **Step 1: 写租户依赖失败测试**

创建 `tests/api/test_dependencies.py`：

```python
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.api.dependencies import create_current_tenant_dependency
from app.application.organization_service import (
    OrganizationService,
    TenantContext,
)
from app.organizations.sqlite_store import SQLiteOrganizationStore
from app.users.sqlite_store import SQLiteUserStore


def build_client(tmp_path, *, member: bool):
    database_path = tmp_path / "app.db"
    users = SQLiteUserStore(database_path)
    organizations = SQLiteOrganizationStore(database_path)
    service = OrganizationService(
        organization_store=organizations,
        user_store=users,
    )
    alice = users.create_user(username="alice", password_hash="hash")
    bob = users.create_user(username="bob", password_hash="hash")
    organization = service.create_organization(
        user_id=alice.user_id,
        name="Acme",
    )
    current_user = alice if member else bob

    def get_current_user():
        return current_user

    get_current_tenant = create_current_tenant_dependency(
        organization_service=service,
        get_current_user=get_current_user,
    )
    app = FastAPI()

    @app.get("/tenant")
    def tenant(context: TenantContext = Depends(get_current_tenant)):
        return {
            "user_id": context.user_id,
            "organization_id": context.organization_id,
            "role": context.role,
        }

    return TestClient(app), organization


def test_member_header_builds_tenant_context(tmp_path):
    client, organization = build_client(tmp_path, member=True)

    response = client.get(
        "/tenant",
        headers={"X-Organization-ID": organization.organization_id},
    )

    assert response.status_code == 200
    assert response.json()["organization_id"] == organization.organization_id
    assert response.json()["role"] == "admin"


def test_missing_or_blank_header_returns_400(tmp_path):
    client, _ = build_client(tmp_path, member=True)

    missing = client.get("/tenant")
    blank = client.get(
        "/tenant",
        headers={"X-Organization-ID": "   "},
    )

    assert missing.status_code == 400
    assert blank.status_code == 400
    assert missing.json() == {"detail": "organization context required"}


def test_non_member_receives_hidden_404(tmp_path):
    client, organization = build_client(tmp_path, member=False)

    response = client.get(
        "/tenant",
        headers={"X-Organization-ID": organization.organization_id},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "organization not found"}
```

- [ ] **Step 2: 运行测试并确认失败**

Run:

```powershell
python -m pytest tests/api/test_dependencies.py -q
```

Expected: import fails because `create_current_tenant_dependency` does not exist。

- [ ] **Step 3: 实现依赖**

在 `app/api/dependencies.py` 增加导入：

```python
from typing import Annotated

from fastapi import Header

from app.application.organization_service import (
    OrganizationAccessDeniedError,
    OrganizationService,
    TenantContext,
)
```

在现有 `create_current_user_dependency` 之后增加：

```python
def create_current_tenant_dependency(
    *,
    organization_service: OrganizationService,
    get_current_user: Callable[..., User],
) -> Callable[..., TenantContext]:
    def get_current_tenant(
        current_user: User = Depends(get_current_user),
        organization_id: Annotated[
            str | None,
            Header(alias="X-Organization-ID"),
        ] = None,
    ) -> TenantContext:
        normalized = (organization_id or "").strip()
        if not normalized:
            raise HTTPException(
                status_code=400,
                detail="organization context required",
            )
        try:
            return organization_service.get_tenant_context(
                user_id=current_user.user_id,
                organization_id=normalized,
            )
        except OrganizationAccessDeniedError as exc:
            raise HTTPException(
                status_code=404,
                detail="organization not found",
            ) from exc

    return get_current_tenant
```

- [ ] **Step 4: 运行局部测试和认证回归**

Run:

```powershell
python -m pytest tests/api/test_dependencies.py tests/api/test_auth_router.py -q
python -m pytest -q
```

Expected: 两条命令全部 PASS；原有 Bearer 认证行为不变。

- [ ] **Step 5: 提交**

```powershell
git add app/api/dependencies.py tests/api/test_dependencies.py
git commit -m "feat: resolve verified tenant context"
```

---

### Task 7: 将会话端到端迁移为企业与用户双重隔离

**独立验收产物：** `conversations.organization_id` 为非空并受 membership 外键约束；旧会话可以根据已有用户的个人企业回填；SessionStore、ChatService、Router 和生产装配都要求 `TenantContext`；跨企业和同企业跨用户访问都失败，且全量测试保持通过。

**Files:**
- Create: `migrations/versions/0003_conversation_tenant_scope.py`
- Modify: `app/sessions/base.py`
- Modify: `app/sessions/sqlite_store.py`
- Modify: `app/application/chat_service.py`
- Modify: `tests/db/test_migrations.py`
- Modify: `tests/sessions/test_sqlite_store.py`
- Modify: `tests/application/test_chat_service.py`
- Modify: `app/api/router.py`
- Modify: `main.py`
- Modify: `tests/api/test_router.py`
- Modify: `tests/test_main.py`

**Interfaces:**
- Consumes: `TenantContext`
- Produces: Alembic revision `0003_conversation_tenant_scope`
- Changes: `Conversation` adds `organization_id: str`
- Changes: all `SessionStore` methods add `organization_id: str`
- Changes: all `ChatService` use cases accept `context: TenantContext`
- Changes: `creat_conversation_router` consumes `get_current_tenant`
- Produces: fully wired tenant-scoped conversation API

- [ ] **Step 1: 写会话迁移失败测试**

在 `tests/db/test_migrations.py` 增加：

```python
def test_legacy_conversation_is_backfilled_to_users_personal_org(tmp_path):
    database_path = tmp_path / "conversation-backfill.db"
    create_legacy_schema(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO users(id, username, password_hash)
            VALUES ('user-1', 'alice', 'hash')
            """
        )
        connection.execute(
            """
            INSERT INTO conversations(id, user_id)
            VALUES ('conversation-1', 'user-1')
            """
        )

    upgrade_database(database_path)

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            """
            SELECT c.organization_id, m.user_id, m.role
            FROM conversations AS c
            JOIN memberships AS m
              ON m.organization_id = c.organization_id
             AND m.user_id = c.user_id
            WHERE c.id = 'conversation-1'
            """
        ).fetchone()

    assert row[0]
    assert row[1:] == ("user-1", "admin")


def test_orphan_legacy_conversation_blocks_tenant_migration(tmp_path):
    database_path = tmp_path / "orphan.db"
    create_legacy_schema(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO conversations(id, user_id)
            VALUES ('conversation-1', 'missing-user')
            """
        )

    with pytest.raises(RuntimeError, match="unowned legacy conversations"):
        upgrade_database(database_path)
```

在 `tests/db/test_migrations.py` 另增最近一次迁移回滚测试，保留 Task 2 的
`0002 → 0001` 回滚测试不变：

```python
def test_conversation_tenant_scope_can_rollback_to_memberships(tmp_path):
    database_path = tmp_path / "conversation-rollback.db"
    upgrade_database(database_path)

    command.downgrade(
        alembic_config(database_path),
        "0002_organization_memberships",
    )

    assert {"organizations", "memberships"} <= table_names(database_path)
    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(conversations)"
            ).fetchall()
        }
    assert "organization_id" not in columns
```

- [ ] **Step 2: 写 SessionStore 三元隔离失败测试**

重写 `tests/sessions/test_sqlite_store.py` 的测试准备部分，使用真实用户和企业：

```python
import pytest

from app.organizations.sqlite_store import SQLiteOrganizationStore
from app.sessions.base import ConversationNotFoundError
from app.sessions.sqlite_store import SQLiteSessionStore
from app.users.sqlite_store import SQLiteUserStore


def build_scope(tmp_path):
    database_path = tmp_path / "chat.db"
    users = SQLiteUserStore(database_path)
    organizations = SQLiteOrganizationStore(database_path)
    sessions = SQLiteSessionStore(database_path)
    alice = users.create_user(username="alice", password_hash="hash")
    bob = users.create_user(username="bob", password_hash="hash")
    org_a = organizations.create_with_admin(
        name="Organization A",
        admin_user_id=alice.user_id,
    )
    org_b = organizations.create_with_admin(
        name="Organization B",
        admin_user_id=bob.user_id,
    )
    return sessions, alice, bob, org_a, org_b
```

将所有 SessionStore 调用增加 `organization_id`。新增两个关键测试：

```python
def test_get_conversation_hides_wrong_organization(tmp_path):
    store, alice, _, org_a, org_b = build_scope(tmp_path)
    conversation = store.create_conversation(
        organization_id=org_a.organization_id,
        user_id=alice.user_id,
    )

    with pytest.raises(ConversationNotFoundError):
        store.get_conversation(
            organization_id=org_b.organization_id,
            user_id=alice.user_id,
            conversation_id=conversation.conversation_id,
        )


def test_get_conversation_still_hides_wrong_user(tmp_path):
    store, alice, bob, org_a, _ = build_scope(tmp_path)
    conversation = store.create_conversation(
        organization_id=org_a.organization_id,
        user_id=alice.user_id,
    )

    with pytest.raises(ConversationNotFoundError):
        store.get_conversation(
            organization_id=org_a.organization_id,
            user_id=bob.user_id,
            conversation_id=conversation.conversation_id,
        )
```

对于 Bob 在 Organization A 内的测试，先通过 `SQLiteOrganizationStore.add_membership`
把 Bob 加入 Organization A；即使是同企业成员，Bob 仍不能读取 Alice 的会话。

- [ ] **Step 3: 运行迁移和 SessionStore 测试并确认失败**

Run:

```powershell
python -m pytest tests/db/test_migrations.py tests/sessions/test_sqlite_store.py -q
```

Expected: FAIL because `organization_id` 列和 Store 参数尚不存在。

- [ ] **Step 4: 创建会话租户范围迁移**

创建 `migrations/versions/0003_conversation_tenant_scope.py`：

```python
from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0003_conversation_tenant_scope"
down_revision: str | None = "0002_organization_memberships"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("organization_id", sa.Text(), nullable=True),
    )
    connection = op.get_bind()
    connection.execute(
        sa.text(
            """
            UPDATE conversations
            SET organization_id = (
                SELECT m.organization_id
                FROM memberships AS m
                WHERE m.user_id = conversations.user_id
                ORDER BY m.created_at, m.organization_id
                LIMIT 1
            )
            WHERE organization_id IS NULL
            """
        )
    )
    unowned_count = connection.execute(
        sa.text(
            """
            SELECT COUNT(*)
            FROM conversations
            WHERE organization_id IS NULL
            """
        )
    ).scalar_one()
    if unowned_count:
        raise RuntimeError(
            f"{unowned_count} unowned legacy conversations require mapping"
        )

    with op.batch_alter_table("conversations") as batch:
        batch.alter_column(
            "organization_id",
            existing_type=sa.Text(),
            nullable=False,
        )
        batch.create_foreign_key(
            "fk_conversations_membership",
            "memberships",
            ["organization_id", "user_id"],
            ["organization_id", "user_id"],
        )
        batch.create_index(
            "idx_conversations_organization_user",
            ["organization_id", "user_id", "updated_at"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("conversations") as batch:
        batch.drop_index("idx_conversations_organization_user")
        batch.drop_constraint(
            "fk_conversations_membership",
            type_="foreignkey",
        )
        batch.drop_column("organization_id")
```

- [ ] **Step 5: 修改 Conversation 和 SessionStore 协议**

在 `app/sessions/base.py` 中把 `Conversation` 改为：

```python
@dataclass(frozen=True)
class Conversation:
    conversation_id: str
    organization_id: str
    user_id: str
    system_prompt: str | None
```

为 `SessionStore` 的五个方法全部增加关键字参数：

```python
organization_id: str
```

最终签名必须为：

```python
create_conversation(
    *,
    organization_id: str,
    user_id: str,
    system_prompt: str | None = None,
) -> Conversation

get_conversation(
    *,
    organization_id: str,
    user_id: str,
    conversation_id: str,
) -> Conversation

update_system_prompt(
    *,
    organization_id: str,
    user_id: str,
    conversation_id: str,
    system_prompt: str,
) -> None

load_messages(
    *,
    organization_id: str,
    user_id: str,
    conversation_id: str,
) -> list[dict]

append_messages(
    *,
    organization_id: str,
    user_id: str,
    conversation_id: str,
    messages: list[dict],
) -> None
```

- [ ] **Step 6: 修改 SQLiteSessionStore 的每条查询**

`_to_conversation()` 必须读取：

```python
organization_id=row["organization_id"]
```

`_get_owned_row()` 的查询固定为：

```sql
SELECT id, organization_id, user_id, system_prompt
FROM conversations
WHERE id = ? AND organization_id = ? AND user_id = ?
```

`create_conversation()` 的 INSERT 固定为：

```sql
INSERT INTO conversations (
    id,
    organization_id,
    user_id,
    system_prompt,
    created_at,
    updated_at
)
VALUES (?, ?, ?, ?, ?, ?)
```

所有 `_get_owned_row()` 调用都传入 `organization_id`。更新会话时间的 SQL 固定为：

```sql
UPDATE conversations
SET updated_at = CURRENT_TIMESTAMP
WHERE id = ? AND organization_id = ? AND user_id = ?
```

不要只在 Router 校验租户；Store 必须保留三元过滤。

- [ ] **Step 7: 修改 ChatService 消费 TenantContext**

在 `app/application/chat_service.py` 导入：

```python
from app.application.organization_service import TenantContext
```

把三个公开方法签名中的 `user_id` 替换为：

```python
context: TenantContext
```

所有 Store 调用统一传：

```python
organization_id=context.organization_id,
user_id=context.user_id,
```

例如创建会话：

```python
def create_conversation(
    self,
    *,
    context: TenantContext,
    system_prompt: str | None = None,
) -> Conversation:
    return self._store.create_conversation(
        organization_id=context.organization_id,
        user_id=context.user_id,
        system_prompt=system_prompt,
    )
```

聊天和修改系统提示词采用同一方式，不修改 Agent Runner 调用和消息提交时机。

- [ ] **Step 8: 更新 ChatService 测试**

在 `tests/application/test_chat_service.py` 创建统一上下文：

```python
from app.application.organization_service import TenantContext
from app.organizations.base import MembershipRole


CONTEXT = TenantContext(
    user_id="user-a",
    organization_id="org-a",
    role=MembershipRole.AGENT,
)
```

测试数据库准备必须先创建 `user-a` 和 `org-a` membership，再创建会话。对 Fake Store
或调用断言，明确断言 `organization_id == "org-a"` 和 `user_id == "user-a"`。
保留原有“Runner 失败时不提交本轮消息”回归测试。

- [ ] **Step 9: 运行 Part A 局部测试**

Run:

```powershell
python -m pytest tests/db/test_migrations.py tests/sessions/test_sqlite_store.py tests/application/test_chat_service.py -q
```

Expected: 全部 PASS。此时不要提交，也不要把中间状态交付给其他分支；继续完成下面的
Part B，使 Router 和生产装配与新接口在同一个原子提交中落地。

#### Part B：接入会话 Router 和生产装配

- [ ] **Step 10: 更新 Router 单元测试为 TenantContext**

在 `tests/api/test_router.py` 用以下对象替换 `CURRENT_USER`：

```python
from app.application.organization_service import TenantContext
from app.organizations.base import MembershipRole


CURRENT_TENANT = TenantContext(
    user_id="authenticated-user",
    organization_id="organization-a",
    role=MembershipRole.AGENT,
)
```

`build_client()` 改为：

```python
def build_client():
    def get_current_tenant():
        return CURRENT_TENANT

    service = FakeChatService()
    app = FastAPI()
    app.include_router(
        creat_conversation_router(
            chat_service=service,
            get_current_tenant=get_current_tenant,
        )
    )
    return TestClient(app), service
```

FakeChatService 的方法签名改为接收 `context`，调用记录必须包含：

```python
context.organization_id
context.user_id
```

例如创建会话的期望记录为：

```python
[
    (
        "create",
        "organization-a",
        "authenticated-user",
        "be helpful",
    )
]
```

- [ ] **Step 11: 更新生产端到端失败测试**

在 `tests/test_main.py` 增加辅助函数：

```python
def create_organization(client, token, name):
    response = client.post(
        "/organizations/",
        json={"name": name},
        headers=bearer(token),
    )
    assert response.status_code == 201
    return response.json()["organization_id"]


def tenant_headers(token, organization_id):
    return {
        "Authorization": f"Bearer {token}",
        "X-Organization-ID": organization_id,
    }
```

把原有会话创建请求改为携带 `tenant_headers`，并新增：

```python
def test_tenant_header_is_verified_and_user_ownership_is_preserved(
    monkeypatch,
    tmp_path,
):
    client = TestClient(load_app(monkeypatch, tmp_path))
    alice_token = register_and_login(client, "alice")
    bob_token = register_and_login(client, "bob")
    org_a = create_organization(client, alice_token, "Company A")
    org_b = create_organization(client, bob_token, "Company B")

    missing_context = client.post(
        "/conversations/",
        json={},
        headers=bearer(alice_token),
    )
    created = client.post(
        "/conversations/",
        json={"system_prompt": "be helpful"},
        headers=tenant_headers(alice_token, org_a),
    )
    conversation_id = created.json()["conversation_id"]
    wrong_tenant = client.post(
        f"/conversations/{conversation_id}/chat/",
        json={"question": "steal history"},
        headers=tenant_headers(bob_token, org_a),
    )
    add_alice_to_org_b = client.post(
        f"/organizations/{org_b}/members/",
        json={"username": "alice", "role": "agent"},
        headers=bearer(bob_token),
    )
    own_other_tenant = client.post(
        f"/conversations/{conversation_id}/chat/",
        json={"question": "wrong company"},
        headers=tenant_headers(alice_token, org_b),
    )

    assert missing_context.status_code == 400
    assert created.status_code == 201
    assert wrong_tenant.status_code == 404
    assert add_alice_to_org_b.status_code == 201
    assert own_other_tenant.status_code == 404
```

- [ ] **Step 12: 运行 Router 和主装配测试并确认失败**

Run:

```powershell
python -m pytest tests/api/test_router.py tests/test_main.py -q
```

Expected: FAIL because Router 和 `main.py` 仍使用 `current_user`。

- [ ] **Step 13: 修改会话 Router**

在 `app/api/router.py`：

1. 删除 `User` 导入。
2. 导入 `TenantContext`。
3. 把工厂参数改为：

```python
get_current_tenant: Callable[..., TenantContext]
```

4. 三个接口依赖统一改为：

```python
context: TenantContext = Depends(get_current_tenant)
```

5. 三个 ChatService 调用统一传：

```python
context=context
```

不再在会话 Router 中读取 `current_user.user_id`。

- [ ] **Step 14: 修改 main.py 生产装配**

增加导入：

```python
from app.api.organization_router import create_organization_router
from app.api.dependencies import create_current_tenant_dependency
from app.application.organization_service import OrganizationService
from app.organizations.sqlite_store import SQLiteOrganizationStore
```

在 `user_store` 创建后增加：

```python
organization_store = SQLiteOrganizationStore(database_path=database_path)
organization_service = OrganizationService(
    organization_store=organization_store,
    user_store=user_store,
)
```

在 `get_current_user` 创建后增加：

```python
get_current_tenant = create_current_tenant_dependency(
    organization_service=organization_service,
    get_current_user=get_current_user,
)
```

会话 Router 装配改为：

```python
app.include_router(
    creat_conversation_router(
        chat_service=chat_service,
        get_current_tenant=get_current_tenant,
    )
)
```

增加企业 Router：

```python
app.include_router(
    create_organization_router(
        organization_service=organization_service,
        get_current_user=get_current_user,
    )
)
```

- [ ] **Step 15: 增加同企业不同用户仍隔离的端到端测试**

在 `tests/test_main.py` 增加：

```python
def test_two_members_share_tenant_but_not_private_conversations(
    monkeypatch,
    tmp_path,
):
    client = TestClient(load_app(monkeypatch, tmp_path))
    alice_token = register_and_login(client, "alice")
    bob_token = register_and_login(client, "bob")
    org_a = create_organization(client, alice_token, "Company A")

    added = client.post(
        f"/organizations/{org_a}/members/",
        json={"username": "bob", "role": "agent"},
        headers=bearer(alice_token),
    )
    alice_conversation = client.post(
        "/conversations/",
        json={},
        headers=tenant_headers(alice_token, org_a),
    )
    bob_conversation = client.post(
        "/conversations/",
        json={},
        headers=tenant_headers(bob_token, org_a),
    )
    cross_user = client.put(
        (
            "/conversations/"
            f"{alice_conversation.json()['conversation_id']}"
            "/system-prompt/"
        ),
        json={"system_prompt": "unauthorized"},
        headers=tenant_headers(bob_token, org_a),
    )

    assert added.status_code == 201
    assert alice_conversation.status_code == 201
    assert bob_conversation.status_code == 201
    assert cross_user.status_code == 404
```

- [ ] **Step 16: 运行局部测试和全量回归**

Run:

```powershell
python -m pytest tests/api/test_router.py tests/test_main.py -q
python -m pytest -q
```

Expected: 两条命令全部 PASS；未带租户头的旧会话请求现在稳定返回 400。

- [ ] **Step 17: 提交**

```powershell
git add migrations/versions/0003_conversation_tenant_scope.py app/sessions app/application/chat_service.py app/api/router.py main.py tests/db tests/sessions tests/application/test_chat_service.py tests/api/test_router.py tests/test_main.py
git commit -m "feat: enforce tenant and owner conversation isolation"
```

---

### Task 8: 编写迁移运行手册并完成安全验收

**独立验收产物：** 开发者能够备份、升级、检查和回滚数据库；自动化验收覆盖空库迁移、旧库升级、RBAC、跨租户攻击、用户级会话隔离和全量回归。

**Files:**
- Create: `docs/database-migrations.md`
- Modify: `.env.example`
- Verify: all files changed by Tasks 1～7

**Interfaces:**
- Documents: local migration command
- Documents: legacy database backup and upgrade
- Documents: one-revision rollback
- Documents: destructive baseline rollback warning

- [ ] **Step 1: 写数据库迁移运行手册**

创建 `docs/database-migrations.md`：

```markdown
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
```

- [ ] **Step 2: 修正环境变量示例中的敏感信息**

将 `.env.example` 第一行当前的具体 Key 值替换为不具备访问能力的占位符：

```dotenv
DEEPSEEK_API_KEY=replace-with-your-deepseek-key
```

保留：

```dotenv
CHAT_DB_PATH=chat_history.db
```

- [ ] **Step 3: 执行迁移专项验收**

Run:

```powershell
python -m pytest tests/db/test_migrations.py -q
```

Expected:

- 空数据库可升级到 `0003_conversation_tenant_scope`。
- 完整旧结构可识别并升级。
- 已有用户获得个人企业和管理员 membership。
- 已有会话得到 `organization_id`。
- 孤儿会话阻止升级。
- 最新迁移可回滚且旧会话不丢失。

- [ ] **Step 4: 执行授权专项验收**

Run:

```powershell
python -m pytest tests/organizations tests/application/test_organization_service.py tests/api/test_organization_router.py tests/api/test_dependencies.py -q
```

Expected:

- admin 可以添加成员。
- agent 添加成员返回 403。
- 非成员选择企业返回 404。
- 缺少租户上下文返回 400。
- 重复 membership 返回 409。

- [ ] **Step 5: 执行隔离专项验收**

Run:

```powershell
python -m pytest tests/sessions tests/application/test_chat_service.py tests/api/test_router.py tests/test_main.py -q
```

Expected:

- 企业 A 用户不能访问企业 B 数据。
- 同企业 Bob 不能访问 Alice 的私有会话。
- 合法成员可以在当前企业创建自己的会话。
- Runner 异常时不提交局部消息的既有行为仍然通过。

- [ ] **Step 6: 执行全量验证**

Run:

```powershell
python -m pytest -q
```

Expected: 退出码为 0，没有失败或错误。

- [ ] **Step 7: 人工检查数据库结构**

使用临时数据库运行：

```powershell
$env:CHAT_DB_PATH = ".\tenant-verification.db"
python -m alembic upgrade head
python -m alembic current
```

Expected: 当前 revision 为 `0003_conversation_tenant_scope`。

检查完成后，通过文件资源管理器删除临时生成的：

```text
tenant-verification.db
tenant-verification.db-wal
tenant-verification.db-shm
```

不要把临时数据库加入 Git。

- [ ] **Step 8: 提交**

```powershell
git add docs/database-migrations.md .env.example
git commit -m "docs: add tenant migration runbook"
```

---

## 5. 最终验收清单

完成全部 Task 后，逐项确认：

- [ ] 新用户登录后可以创建企业，并自动成为该企业 `admin`。
- [ ] 用户可以列出自己加入的企业，列表不包含其他企业。
- [ ] `admin` 可以把已注册用户加入企业并授予 `admin` 或 `agent`。
- [ ] `agent` 不能添加成员。
- [ ] 会话请求缺少 `X-Organization-ID` 时返回 400。
- [ ] 请求头中的企业 ID 必须经过 membership 校验。
- [ ] 企业 A 的成员不能通过篡改请求头读取企业 B 的数据。
- [ ] 同企业不同用户仍不能读取彼此的私有会话。
- [ ] 所有会话 Store 查询同时包含 `organization_id`、`user_id`、`conversation_id`。
- [ ] `organizations`、`memberships` 和 `conversations.organization_id` 由 Alembic 创建。
- [ ] 空数据库可以执行 `upgrade head`。
- [ ] 现有完整三表数据库可以无数据丢失地升级。
- [ ] 孤儿旧会话不会被自动猜测归属。
- [ ] `downgrade -1` 可以回滚最近一次迁移并保留旧会话。
- [ ] Store 中不再存在 `CREATE TABLE IF NOT EXISTS`。
- [ ] 全量 pytest 退出码为 0。

## 6. 明确不属于本计划的工作

以下内容分别进入独立实施计划：

- `refresh-token-and-session-revocation`：刷新、登出和 Token 撤销。
- `customer-support-domain-tools`：客户、订单、物流、工单和 Tool Gateway。
- `tenant-scoped-rag`：企业文档、向量分块和 `organization_id` 检索过滤。
- `approval-and-durable-execution`：退款审批、幂等和暂停恢复。
- `postgresql-and-sqlalchemy-store-migration`：把同步 `sqlite3` Store 替换为 SQLAlchemy/PostgreSQL。
