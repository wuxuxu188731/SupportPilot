# Register/Login Authentication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为现有 FastAPI Agent 聊天后端增加用户名/密码注册、JWT 登录和当前用户鉴权，并让所有会话接口只信任登录凭证中的用户身份。

**Architecture:** 保留现有 Router → Application Service → Store 分层，新增 User Store、AuthService、密码哈希/JWT 安全组件和 FastAPI 鉴权依赖。用户与会话共用当前 SQLite 数据库；认证依赖从 Bearer Token 得到 `User`，会话 Router 再把 `current_user.id` 传给现有 `ChatService`，Agent Runner 不感知认证细节。

**Tech Stack:** Python 3.10、FastAPI 0.115、Pydantic 2、标准库 `sqlite3`、`bcrypt` 5、PyJWT 2、pytest、FastAPI TestClient。

## Global Constraints

- 本计划只实现后端 API，不实现 HTML、Vue 或 React 登录页面；可使用 FastAPI `/docs` 验收。
- 注册标识暂定为唯一 `username`；不引入邮箱验证、手机号或找回密码。
- 密码只保存 bcrypt 哈希，cost factor 固定为 12；明文密码和 Token 不得写入日志或响应。
- Access Token 使用 HS256，默认有效期 30 分钟，必须包含 `sub`、`type`、`iss`、`iat`、`exp`。
- `AUTH_SECRET_KEY` 必须通过环境变量提供且不少于 32 个字符，不能提供代码内默认值。
- 所有会话接口必须从 Bearer Token 得到 `user_id`，请求体中的 `user_id` 字段必须删除并拒绝。
- 错误用户名和错误密码统一返回 `401 invalid username or password`，避免暴露用户名是否存在。
- 本阶段不实现 Refresh Token、Token 主动撤销、登出、RBAC、组织/租户、第三方 OAuth 和密码重置；Token 过期后重新登录。
- 当前本地数据库没有可安全推断的“旧 `user_id` → 新注册用户”映射；旧会话不自动归属新账户。开发数据库可备份后重建，生产迁移必须另写数据映射方案。
- 保持现有同步 FastAPI/同步 `sqlite3` 风格，不在本功能中顺带迁移 async、ORM、Alembic、PostgreSQL 或 Redis。
- 每个 Task 按失败测试 → 最小实现 → 局部测试 → 全量回归 → 提交的顺序完成。

---

## 1. 当前项目调研结论

当前代码已经具备清晰的基础边界：

- `main.py`：创建 FastAPI、LLM Client、SQLiteSessionStore、ChatService 并装配 Router。
- `app/api/router.py`：创建会话、聊天、修改系统提示词三个 HTTP 接口。
- `app/application/chat_service.py`：组织会话加载、Agent 调用和消息持久化。
- `app/sessions/sqlite_store.py`：SQLite 会话和消息存储，以 `user_id + conversation_id` 校验所有权。
- `app/agent/runner.py`：模型/工具调用循环，不应引入认证逻辑。
- `tests/`：已有 Router、Service、Store、并发和 Agent Runner 测试。
- 调研时基线：`31 passed, 1 warning`。

当前的关键安全缺口是：`CreateConversationRequest`、`ChatRequest` 和
`UpdateSystemPromptRequest` 都允许客户端直接提交任意 `user_id`。数据库虽然按
`user_id` 隔离，但攻击者只要知道另一个用户的字符串 ID 就能冒充该用户。因此本次
功能的完成标志不是“多两个注册/登录接口”，而是“所有受保护资源都只相信认证上下文”。

## 2. 本次 API 契约

| 方法 | 路径 | 是否登录 | 成功响应 | 主要错误 |
|---|---|---:|---|---|
| POST | `/auth/register` | 否 | `201 UserResponse` | `409 username already exists`、`422` |
| POST | `/auth/login` | 否 | `200 TokenResponse` | `401 invalid username or password` |
| GET | `/auth/me` | 是 | `200 UserResponse` | `401 invalid or expired access token` |
| POST | `/conversations/` | 是 | `201 ConversationCreated` | `401`、`422` |
| POST | `/conversations/{id}/chat/` | 是 | `200 LLMResponse` | `401`、`404`、`422` |
| PUT | `/conversations/{id}/system-prompt/` | 是 | `200 SystemPromptUpdated` | `401`、`404`、`422` |

注册请求：

```json
{
  "username": "alice_01",
  "password": "correct-horse-42"
}
```

登录响应：

```json
{
  "access_token": "<jwt>",
  "token_type": "bearer",
  "expires_in": 1800
}
```

受保护请求：

```http
Authorization: Bearer <jwt>
```

## 3. 最终文件结构与职责

```text
requirements.txt                         # 可复现的运行和测试依赖
.env.example                             # 必需环境变量示例，不含真实密钥
README.md                                # 启动、注册、登录、携带 Token 的说明
app/
  auth/
    __init__.py
    passwords.py                         # bcrypt 哈希与校验
    tokens.py                            # Access Token 签发与解析
  users/
    __init__.py
    base.py                              # User、UserStore 协议和领域异常
    sqlite_store.py                      # users 表及 SQLite CRUD
  application/
    auth_service.py                      # 注册、登录、Token→User 用例
  schemas/
    auth.py                              # 注册/登录/用户/Token HTTP 模型
    chat.py                              # 删除客户端 user_id，拒绝多余字段
  api/
    auth_router.py                       # /auth/register、/auth/login、/auth/me
    dependencies.py                      # Bearer Token → current_user
    router.py                            # 现有会话路由改为 current_user
  core/
    config.py                            # 认证密钥和 Token TTL 配置
tests/
  auth/
    test_passwords.py
    test_tokens.py
  users/
    test_sqlite_user_store.py
  application/
    test_auth_service.py
  api/
    test_auth_router.py
    test_router.py
  test_auth_flow.py                      # 真实 Router/Store 的完整认证链路
  test_main.py                           # 生产装配验证
```

---

### Task 1: 建立用户领域模型和 SQLite 用户存储

**独立验收产物：** 可以在 SQLite 中创建用户、按用户名/ID 查找用户，并由数据库唯一
约束阻止大小写不同的重复用户名；数据库中不存在明文密码。

**Files:**
- Create: `app/users/__init__.py`
- Create: `app/users/base.py`
- Create: `app/users/sqlite_store.py`
- Create: `tests/users/test_sqlite_user_store.py`

**Interfaces:**
- Produces: `User(user_id: str, username: str, password_hash: str, created_at: str)`
- Produces: `UserStore.create_user(*, username: str, password_hash: str) -> User`
- Produces: `UserStore.get_by_username(*, username: str) -> User`
- Produces: `UserStore.get_by_id(*, user_id: str) -> User`
- Produces: `UserAlreadyExistsError`、`UserNotFoundError`

- [ ] **Step 1: 写用户存储失败测试**

创建 `tests/users/test_sqlite_user_store.py`：

```python
import sqlite3

import pytest

from app.users.base import UserAlreadyExistsError, UserNotFoundError
from app.users.sqlite_store import SQLiteUserStore


def test_user_round_trip_by_username_and_id(tmp_path):
    database_path = tmp_path / "app.db"
    store = SQLiteUserStore(database_path)

    created = store.create_user(
        username="alice",
        password_hash="$2b$12$test-hash",
    )

    assert store.get_by_username(username="alice") == created
    assert store.get_by_id(user_id=created.user_id) == created


def test_username_is_unique_case_insensitively(tmp_path):
    store = SQLiteUserStore(tmp_path / "app.db")
    store.create_user(username="alice", password_hash="first-hash")

    with pytest.raises(UserAlreadyExistsError):
        store.create_user(username="ALICE", password_hash="second-hash")


def test_missing_user_raises_domain_error(tmp_path):
    store = SQLiteUserStore(tmp_path / "app.db")

    with pytest.raises(UserNotFoundError):
        store.get_by_username(username="missing")

    with pytest.raises(UserNotFoundError):
        store.get_by_id(user_id="missing")


def test_database_never_contains_plain_password(tmp_path):
    database_path = tmp_path / "app.db"
    store = SQLiteUserStore(database_path)
    store.create_user(username="alice", password_hash="stored-hash")

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT username, password_hash FROM users"
        ).fetchone()

    assert row == ("alice", "stored-hash")
    columns = {
        item[1]
        for item in sqlite3.connect(database_path)
        .execute("PRAGMA table_info(users)")
        .fetchall()
    }
    assert "password" not in columns
```

- [ ] **Step 2: 运行测试并确认失败**

Run:

```powershell
python -m pytest tests/users/test_sqlite_user_store.py -q
```

Expected: collection fails with `ModuleNotFoundError: No module named 'app.users'`。

- [ ] **Step 3: 实现用户领域接口**

创建空文件 `app/users/__init__.py`，并创建 `app/users/base.py`：

```python
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class User:
    user_id: str
    username: str
    password_hash: str
    created_at: str


class UserAlreadyExistsError(ValueError):
    pass


class UserNotFoundError(LookupError):
    pass


class UserStore(Protocol):
    def create_user(self, *, username: str, password_hash: str) -> User:
        raise NotImplementedError

    def get_by_username(self, *, username: str) -> User:
        raise NotImplementedError

    def get_by_id(self, *, user_id: str) -> User:
        raise NotImplementedError
```

- [ ] **Step 4: 实现 SQLite 用户存储**

创建 `app/users/sqlite_store.py`：

```python
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from app.users.base import (
    User,
    UserAlreadyExistsError,
    UserNotFoundError,
)


class SQLiteUserStore:
    def __init__(self, database_path: str | Path):
        self._database_path = database_path
        self._initialize()

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

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username
                ON users(username COLLATE NOCASE);
                """
            )

    @staticmethod
    def _to_user(row: sqlite3.Row) -> User:
        return User(
            user_id=row["id"],
            username=row["username"],
            password_hash=row["password_hash"],
            created_at=row["created_at"],
        )

    def create_user(self, *, username: str, password_hash: str) -> User:
        user_id = str(uuid4())
        try:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO users(id, username, password_hash)
                    VALUES (?, ?, ?)
                    """,
                    (user_id, username, password_hash),
                )
                row = connection.execute(
                    """
                    SELECT id, username, password_hash, created_at
                    FROM users
                    WHERE id = ?
                    """,
                    (user_id,),
                ).fetchone()
        except sqlite3.IntegrityError as exc:
            raise UserAlreadyExistsError("username already exists") from exc

        if row is None:
            raise RuntimeError("created user could not be reloaded")
        return self._to_user(row)

    def get_by_username(self, *, username: str) -> User:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT id, username, password_hash, created_at
                FROM users
                WHERE username = ? COLLATE NOCASE
                """,
                (username,),
            ).fetchone()
        if row is None:
            raise UserNotFoundError("user not found")
        return self._to_user(row)

    def get_by_id(self, *, user_id: str) -> User:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT id, username, password_hash, created_at
                FROM users
                WHERE id = ?
                """,
                (user_id,),
            ).fetchone()
        if row is None:
            raise UserNotFoundError("user not found")
        return self._to_user(row)
```

- [ ] **Step 5: 运行局部测试**

Run:

```powershell
python -m pytest tests/users/test_sqlite_user_store.py -q
```

Expected: `4 passed`。

- [ ] **Step 6: 提交**

```powershell
git add app/users tests/users
git commit -m "feat: add sqlite user store"
```

---

### Task 2: 实现密码哈希、JWT 和认证配置

**独立验收产物：** 明文密码可安全哈希/校验，Access Token 可签发/验证，过期、篡改、
错误类型 Token 会被拒绝，应用不会使用弱默认密钥。

**Files:**
- Create: `requirements.txt`
- Create: `.env.example`
- Create: `app/auth/__init__.py`
- Create: `app/auth/passwords.py`
- Create: `app/auth/tokens.py`
- Modify: `app/core/config.py`
- Create: `tests/auth/test_passwords.py`
- Create: `tests/auth/test_tokens.py`
- Create: `tests/core/test_config.py`

**Interfaces:**
- Produces: `hash_password(password: str) -> str`
- Produces: `verify_password(password: str, password_hash: str) -> bool`
- Produces: `PasswordPolicyError`
- Produces: `AccessTokenService.issue(*, user_id: str) -> str`
- Produces: `AccessTokenService.decode(token: str) -> AccessTokenClaims`
- Produces: `AccessTokenClaims(user_id: str)`
- Produces: `InvalidAccessTokenError`
- Produces: `get_auth_secret_key() -> str`
- Produces: `get_access_token_ttl_seconds() -> int`

- [ ] **Step 1: 声明依赖和环境变量**

创建 `requirements.txt`：

```text
fastapi==0.115.0
pydantic==2.11.5
openai>=1.0,<3.0
uvicorn>=0.30,<1.0
bcrypt==5.0.0
PyJWT==2.8.0
httpx==0.28.1
pytest==9.1.1
```

创建 `.env.example`：

```dotenv
DEEPSEEK_API_KEY=replace-with-your-deepseek-key
DEEPSEEK_MODEL=deepseek-v4-flash
CHAT_DB_PATH=chat_history.db
AUTH_SECRET_KEY=replace-with-at-least-32-random-characters
ACCESS_TOKEN_TTL_SECONDS=1800
```

在 `.gitignore` 末尾增加：

```text
.env
```

- [ ] **Step 2: 写密码与 Token 失败测试**

创建空文件 `app/auth/__init__.py`，创建 `tests/auth/test_passwords.py`：

```python
import pytest

from app.auth.passwords import (
    PasswordPolicyError,
    hash_password,
    verify_password,
)


def test_password_hash_is_salted_and_verifiable():
    first = hash_password("correct-horse-42")
    second = hash_password("correct-horse-42")

    assert first != second
    assert "correct-horse-42" not in first
    assert verify_password("correct-horse-42", first) is True
    assert verify_password("wrong-password", first) is False


@pytest.mark.parametrize("password", ["short", "密" * 40])
def test_password_rejects_invalid_utf8_byte_length(password):
    with pytest.raises(PasswordPolicyError):
        hash_password(password)
```

创建 `tests/auth/test_tokens.py`：

```python
from datetime import datetime, timedelta, timezone

import jwt
import pytest

from app.auth.tokens import (
    AccessTokenService,
    InvalidAccessTokenError,
)


SECRET = "test-secret-key-that-is-at-least-32-characters"


def test_access_token_round_trip():
    service = AccessTokenService(
        secret_key=SECRET,
        ttl_seconds=1800,
    )

    token = service.issue(user_id="user-123")

    assert service.decode(token).user_id == "user-123"


@pytest.mark.parametrize(
    "payload",
    [
        {"sub": "user-123", "type": "refresh"},
        {"type": "access"},
    ],
)
def test_rejects_wrong_type_or_missing_subject(payload):
    payload.update(
        {
            "iss": "supportpilot",
            "iat": datetime.now(timezone.utc),
            "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        }
    )
    token = jwt.encode(payload, SECRET, algorithm="HS256")
    service = AccessTokenService(secret_key=SECRET, ttl_seconds=1800)

    with pytest.raises(InvalidAccessTokenError):
        service.decode(token)


def test_rejects_expired_or_tampered_token():
    service = AccessTokenService(secret_key=SECRET, ttl_seconds=1800)
    expired = jwt.encode(
        {
            "sub": "user-123",
            "type": "access",
            "iss": "supportpilot",
            "iat": datetime.now(timezone.utc) - timedelta(minutes=10),
            "exp": datetime.now(timezone.utc) - timedelta(minutes=5),
        },
        SECRET,
        algorithm="HS256",
    )

    with pytest.raises(InvalidAccessTokenError):
        service.decode(expired)

    token = service.issue(user_id="user-123")
    with pytest.raises(InvalidAccessTokenError):
        service.decode(token + "tampered")
```

创建 `tests/core/test_config.py`：

```python
import pytest

from app.core.config import (
    get_access_token_ttl_seconds,
    get_auth_secret_key,
)


def test_auth_secret_is_required_and_long(monkeypatch):
    monkeypatch.delenv("AUTH_SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError):
        get_auth_secret_key()

    monkeypatch.setenv("AUTH_SECRET_KEY", "too-short")
    with pytest.raises(RuntimeError):
        get_auth_secret_key()


def test_token_ttl_must_be_positive_integer(monkeypatch):
    monkeypatch.setenv("ACCESS_TOKEN_TTL_SECONDS", "0")
    with pytest.raises(RuntimeError):
        get_access_token_ttl_seconds()

    monkeypatch.setenv("ACCESS_TOKEN_TTL_SECONDS", "1800")
    assert get_access_token_ttl_seconds() == 1800
```

- [ ] **Step 3: 运行测试并确认失败**

Run:

```powershell
python -m pytest tests/auth tests/core/test_config.py -q
```

Expected: import fails because `app.auth.passwords`、`app.auth.tokens` and new config
functions do not exist。

- [ ] **Step 4: 实现密码安全组件**

创建 `app/auth/passwords.py`：

```python
import bcrypt


MIN_PASSWORD_BYTES = 8
MAX_PASSWORD_BYTES = 72
BCRYPT_ROUNDS = 12


class PasswordPolicyError(ValueError):
    pass


def _password_bytes(password: str) -> bytes:
    encoded = password.encode("utf-8")
    if not MIN_PASSWORD_BYTES <= len(encoded) <= MAX_PASSWORD_BYTES:
        raise PasswordPolicyError(
            "password must contain between 8 and 72 UTF-8 bytes"
        )
    return encoded


def hash_password(password: str) -> str:
    encoded = _password_bytes(password)
    return bcrypt.hashpw(
        encoded,
        bcrypt.gensalt(rounds=BCRYPT_ROUNDS),
    ).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        encoded = _password_bytes(password)
        return bcrypt.checkpw(encoded, password_hash.encode("utf-8"))
    except (PasswordPolicyError, ValueError):
        return False
```

- [ ] **Step 5: 实现 Access Token 组件**

创建 `app/auth/tokens.py`：

```python
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt


ISSUER = "supportpilot"
ALGORITHM = "HS256"


@dataclass(frozen=True)
class AccessTokenClaims:
    user_id: str


class InvalidAccessTokenError(ValueError):
    pass


class AccessTokenService:
    def __init__(self, *, secret_key: str, ttl_seconds: int):
        if len(secret_key) < 32:
            raise ValueError("secret_key must contain at least 32 characters")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._secret_key = secret_key
        self._ttl_seconds = ttl_seconds

    @property
    def ttl_seconds(self) -> int:
        return self._ttl_seconds

    def issue(self, *, user_id: str) -> str:
        now = datetime.now(timezone.utc)
        return jwt.encode(
            {
                "sub": user_id,
                "type": "access",
                "iss": ISSUER,
                "iat": now,
                "exp": now + timedelta(seconds=self._ttl_seconds),
            },
            self._secret_key,
            algorithm=ALGORITHM,
        )

    def decode(self, token: str) -> AccessTokenClaims:
        try:
            payload = jwt.decode(
                token,
                self._secret_key,
                algorithms=[ALGORITHM],
                issuer=ISSUER,
                options={"require": ["sub", "type", "iss", "iat", "exp"]},
            )
        except jwt.InvalidTokenError as exc:
            raise InvalidAccessTokenError("invalid access token") from exc

        if payload["type"] != "access":
            raise InvalidAccessTokenError("invalid access token type")
        user_id = payload["sub"]
        if not isinstance(user_id, str) or not user_id:
            raise InvalidAccessTokenError("invalid access token subject")
        return AccessTokenClaims(user_id=user_id)
```

- [ ] **Step 6: 增加认证配置**

保留 `app/core/config.py` 现有 LLM 和数据库配置，并增加：

```python
def get_auth_secret_key() -> str:
    secret_key = os.getenv("AUTH_SECRET_KEY", "")
    if len(secret_key) < 32:
        raise RuntimeError(
            "AUTH_SECRET_KEY must contain at least 32 characters"
        )
    return secret_key


def get_access_token_ttl_seconds() -> int:
    raw_value = os.getenv("ACCESS_TOKEN_TTL_SECONDS", "1800")
    try:
        ttl_seconds = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(
            "ACCESS_TOKEN_TTL_SECONDS must be an integer"
        ) from exc
    if ttl_seconds <= 0:
        raise RuntimeError(
            "ACCESS_TOKEN_TTL_SECONDS must be positive"
        )
    return ttl_seconds
```

- [ ] **Step 7: 安装依赖并运行局部测试**

Run:

```powershell
python -m pip install -r requirements.txt
python -m pytest tests/auth tests/core/test_config.py -q
```

Expected: `9 passed`。

- [ ] **Step 8: 提交**

```powershell
git add requirements.txt .env.example .gitignore app/auth app/core/config.py tests/auth tests/core
git commit -m "feat: add password hashing and access tokens"
```

---

### Task 3: 用 AuthService 实现注册、登录和 Token 解析

**独立验收产物：** 不依赖 HTTP 的应用服务可以完成注册、校验密码、签发 Token，并把
Token 解析为数据库中的当前用户。

**Files:**
- Create: `app/application/auth_service.py`
- Create: `tests/application/test_auth_service.py`

**Interfaces:**
- Consumes: `UserStore`、`hash_password()`、`verify_password()`、`AccessTokenService`
- Produces: `AuthService.register(*, username: str, password: str) -> User`
- Produces: `AuthService.login(*, username: str, password: str) -> LoginResult`
- Produces: `AuthService.get_user_from_token(*, token: str) -> User`
- Produces: `LoginResult(access_token: str, token_type: str, expires_in: int)`
- Produces: `InvalidUsernameError`、`InvalidCredentialsError`、`InvalidAuthenticationError`

- [ ] **Step 1: 写 AuthService 失败测试**

创建 `tests/application/test_auth_service.py`：

```python
import pytest

from app.application.auth_service import (
    AuthService,
    InvalidAuthenticationError,
    InvalidCredentialsError,
    InvalidUsernameError,
)
from app.auth.tokens import AccessTokenService
from app.users.sqlite_store import SQLiteUserStore


SECRET = "test-secret-key-that-is-at-least-32-characters"


def build_service(tmp_path):
    store = SQLiteUserStore(tmp_path / "app.db")
    service = AuthService(
        user_store=store,
        token_service=AccessTokenService(
            secret_key=SECRET,
            ttl_seconds=1800,
        ),
    )
    return service, store


def test_register_normalizes_username_and_hashes_password(tmp_path):
    service, store = build_service(tmp_path)

    user = service.register(
        username="  Alice_01  ",
        password="correct-horse-42",
    )

    assert user.username == "alice_01"
    stored = store.get_by_id(user_id=user.user_id)
    assert stored.password_hash != "correct-horse-42"


@pytest.mark.parametrize(
    "username",
    ["ab", "contains-space", "中文用户", "a" * 33],
)
def test_register_rejects_invalid_username(tmp_path, username):
    service, _ = build_service(tmp_path)

    with pytest.raises(InvalidUsernameError):
        service.register(
            username=username,
            password="correct-horse-42",
        )


def test_login_returns_token_for_correct_password(tmp_path):
    service, _ = build_service(tmp_path)
    user = service.register(
        username="alice",
        password="correct-horse-42",
    )

    result = service.login(
        username="ALICE",
        password="correct-horse-42",
    )

    assert result.token_type == "bearer"
    assert result.expires_in == 1800
    assert (
        service.get_user_from_token(token=result.access_token).user_id
        == user.user_id
    )


@pytest.mark.parametrize(
    ("username", "password"),
    [
        ("missing", "correct-horse-42"),
        ("alice", "wrong-password"),
    ],
)
def test_login_uses_one_error_for_unknown_user_and_wrong_password(
    tmp_path,
    username,
    password,
):
    service, _ = build_service(tmp_path)
    service.register(username="alice", password="correct-horse-42")

    with pytest.raises(InvalidCredentialsError):
        service.login(username=username, password=password)


def test_deleted_or_invalid_token_user_is_rejected(tmp_path):
    service, _ = build_service(tmp_path)

    with pytest.raises(InvalidAuthenticationError):
        service.get_user_from_token(token="not-a-jwt")
```

- [ ] **Step 2: 运行测试并确认失败**

Run:

```powershell
python -m pytest tests/application/test_auth_service.py -q
```

Expected: import fails because `app.application.auth_service` does not exist。

- [ ] **Step 3: 实现 AuthService**

创建 `app/application/auth_service.py`：

```python
import re
from dataclasses import dataclass

from app.auth.passwords import hash_password, verify_password
from app.auth.tokens import (
    AccessTokenService,
    InvalidAccessTokenError,
)
from app.users.base import User, UserNotFoundError, UserStore


USERNAME_PATTERN = re.compile(r"^[a-z0-9_]{3,32}$")
DUMMY_PASSWORD_HASH = (
    "$2b$12$C6UzMDM.H6dfI/f/IKcEe."
    "5YhZQxB4rNB/5G9Jd8M7sV3xB8H6G6K"
)


@dataclass(frozen=True)
class LoginResult:
    access_token: str
    token_type: str
    expires_in: int


class InvalidUsernameError(ValueError):
    pass


class InvalidCredentialsError(ValueError):
    pass


class InvalidAuthenticationError(ValueError):
    pass


class AuthService:
    def __init__(
        self,
        *,
        user_store: UserStore,
        token_service: AccessTokenService,
    ):
        self._user_store = user_store
        self._token_service = token_service

    @staticmethod
    def _normalize_username(username: str) -> str:
        normalized = username.strip().casefold()
        if USERNAME_PATTERN.fullmatch(normalized) is None:
            raise InvalidUsernameError(
                "username must contain 3-32 lowercase letters, digits, or underscores"
            )
        return normalized

    def register(self, *, username: str, password: str) -> User:
        normalized = self._normalize_username(username)
        return self._user_store.create_user(
            username=normalized,
            password_hash=hash_password(password),
        )

    def login(self, *, username: str, password: str) -> LoginResult:
        normalized = username.strip().casefold()
        try:
            user = self._user_store.get_by_username(username=normalized)
            password_hash = user.password_hash
        except UserNotFoundError:
            user = None
            password_hash = DUMMY_PASSWORD_HASH

        password_is_valid = verify_password(password, password_hash)
        if user is None or not password_is_valid:
            raise InvalidCredentialsError(
                "invalid username or password"
            )

        return LoginResult(
            access_token=self._token_service.issue(
                user_id=user.user_id
            ),
            token_type="bearer",
            expires_in=self._token_service.ttl_seconds,
        )

    def get_user_from_token(self, *, token: str) -> User:
        try:
            claims = self._token_service.decode(token)
            return self._user_store.get_by_id(
                user_id=claims.user_id
            )
        except (InvalidAccessTokenError, UserNotFoundError) as exc:
            raise InvalidAuthenticationError(
                "invalid or expired access token"
            ) from exc
```

- [ ] **Step 4: 运行 AuthService 和底层测试**

Run:

```powershell
python -m pytest tests/application/test_auth_service.py tests/auth tests/users -q
```

Expected: all selected tests pass。

- [ ] **Step 5: 提交**

```powershell
git add app/application/auth_service.py tests/application/test_auth_service.py
git commit -m "feat: add authentication service"
```

---

### Task 4: 增加注册、登录、当前用户 HTTP API

**独立验收产物：** HTTP 客户端可以注册、登录、携带 Bearer Token 获取当前用户；重复
用户名和认证失败映射为稳定的 HTTP 状态码。

**Files:**
- Create: `app/schemas/auth.py`
- Create: `app/api/dependencies.py`
- Create: `app/api/auth_router.py`
- Create: `tests/api/test_auth_router.py`

**Interfaces:**
- Consumes: Task 3 的 `AuthService`
- Produces: `create_current_user_dependency(auth_service) -> Callable`
- Produces: `create_auth_router(*, auth_service, get_current_user) -> APIRouter`
- Produces: `POST /auth/register`、`POST /auth/login`、`GET /auth/me`

- [ ] **Step 1: 写 Auth Router 失败测试**

创建 `tests/api/test_auth_router.py`：

```python
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.auth_router import create_auth_router
from app.api.dependencies import create_current_user_dependency
from app.application.auth_service import AuthService
from app.auth.tokens import AccessTokenService
from app.users.sqlite_store import SQLiteUserStore


SECRET = "test-secret-key-that-is-at-least-32-characters"


def build_client(tmp_path):
    service = AuthService(
        user_store=SQLiteUserStore(tmp_path / "app.db"),
        token_service=AccessTokenService(
            secret_key=SECRET,
            ttl_seconds=1800,
        ),
    )
    get_current_user = create_current_user_dependency(service)
    app = FastAPI()
    app.include_router(
        create_auth_router(
            auth_service=service,
            get_current_user=get_current_user,
        )
    )
    return TestClient(app)


def register(client, username="alice", password="correct-horse-42"):
    return client.post(
        "/auth/register",
        json={"username": username, "password": password},
    )


def login(client, username="alice", password="correct-horse-42"):
    return client.post(
        "/auth/login",
        json={"username": username, "password": password},
    )


def test_register_returns_public_user_without_password(tmp_path):
    client = build_client(tmp_path)

    response = register(client)

    assert response.status_code == 201
    assert response.json()["username"] == "alice"
    assert "user_id" in response.json()
    assert "password" not in response.json()
    assert "password_hash" not in response.json()


def test_duplicate_registration_returns_409(tmp_path):
    client = build_client(tmp_path)
    register(client)

    response = register(client, username="ALICE")

    assert response.status_code == 409
    assert response.json() == {"detail": "username already exists"}


def test_login_and_me_round_trip(tmp_path):
    client = build_client(tmp_path)
    registered = register(client).json()

    login_response = login(client)
    token_body = login_response.json()
    me_response = client.get(
        "/auth/me",
        headers={
            "Authorization": f"Bearer {token_body['access_token']}"
        },
    )

    assert login_response.status_code == 200
    assert token_body["token_type"] == "bearer"
    assert token_body["expires_in"] == 1800
    assert me_response.status_code == 200
    assert me_response.json() == registered


def test_login_does_not_reveal_whether_username_exists(tmp_path):
    client = build_client(tmp_path)
    register(client)

    wrong_password = login(client, password="wrong-password")
    missing_user = login(
        client,
        username="missing",
        password="wrong-password",
    )

    assert wrong_password.status_code == 401
    assert missing_user.status_code == 401
    assert wrong_password.json() == missing_user.json()
    assert wrong_password.json() == {
        "detail": "invalid username or password"
    }


def test_me_rejects_missing_and_invalid_bearer_token(tmp_path):
    client = build_client(tmp_path)

    missing = client.get("/auth/me")
    invalid = client.get(
        "/auth/me",
        headers={"Authorization": "Bearer invalid-token"},
    )

    assert missing.status_code == 401
    assert invalid.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"
```

- [ ] **Step 2: 运行测试并确认失败**

Run:

```powershell
python -m pytest tests/api/test_auth_router.py -q
```

Expected: import fails because auth schemas, dependency and Router do not exist。

- [ ] **Step 3: 实现认证 HTTP Schema**

创建 `app/schemas/auth.py`：

```python
from pydantic import BaseModel, ConfigDict, Field

from app.application.auth_service import LoginResult
from app.users.base import User


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=8, max_length=72)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=72)


class UserResponse(BaseModel):
    user_id: str
    username: str
    created_at: str

    @classmethod
    def from_user(cls, user: User) -> "UserResponse":
        return cls(
            user_id=user.user_id,
            username=user.username,
            created_at=user.created_at,
        )


class TokenResponse(BaseModel):
    access_token: str
    token_type: str
    expires_in: int

    @classmethod
    def from_result(cls, result: LoginResult) -> "TokenResponse":
        return cls(
            access_token=result.access_token,
            token_type=result.token_type,
            expires_in=result.expires_in,
        )
```

- [ ] **Step 4: 实现 Bearer Token 依赖**

创建 `app/api/dependencies.py`：

```python
from collections.abc import Callable

from fastapi import Depends, HTTPException
from fastapi.security import (
    HTTPAuthorizationCredentials,
    HTTPBearer,
)

from app.application.auth_service import (
    AuthService,
    InvalidAuthenticationError,
)
from app.users.base import User


bearer_scheme = HTTPBearer(auto_error=False)


def create_current_user_dependency(
    auth_service: AuthService,
) -> Callable[..., User]:
    def get_current_user(
        credentials: HTTPAuthorizationCredentials | None = Depends(
            bearer_scheme
        ),
    ) -> User:
        if credentials is None:
            raise HTTPException(
                status_code=401,
                detail="authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        try:
            return auth_service.get_user_from_token(
                token=credentials.credentials
            )
        except InvalidAuthenticationError as exc:
            raise HTTPException(
                status_code=401,
                detail="invalid or expired access token",
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc

    return get_current_user
```

- [ ] **Step 5: 实现 Auth Router**

创建 `app/api/auth_router.py`：

```python
from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException

from app.application.auth_service import (
    AuthService,
    InvalidCredentialsError,
    InvalidUsernameError,
)
from app.auth.passwords import PasswordPolicyError
from app.schemas.auth import (
    LoginRequest,
    RegisterRequest,
    TokenResponse,
    UserResponse,
)
from app.users.base import User, UserAlreadyExistsError


def create_auth_router(
    *,
    auth_service: AuthService,
    get_current_user: Callable[..., User],
) -> APIRouter:
    router = APIRouter(prefix="/auth", tags=["authentication"])

    @router.post(
        "/register",
        response_model=UserResponse,
        status_code=201,
    )
    def register(request: RegisterRequest) -> UserResponse:
        try:
            user = auth_service.register(
                username=request.username,
                password=request.password,
            )
        except UserAlreadyExistsError as exc:
            raise HTTPException(
                status_code=409,
                detail="username already exists",
            ) from exc
        except (InvalidUsernameError, PasswordPolicyError) as exc:
            raise HTTPException(
                status_code=422,
                detail=str(exc),
            ) from exc
        return UserResponse.from_user(user)

    @router.post(
        "/login",
        response_model=TokenResponse,
    )
    def login(request: LoginRequest) -> TokenResponse:
        try:
            result = auth_service.login(
                username=request.username,
                password=request.password,
            )
        except InvalidCredentialsError as exc:
            raise HTTPException(
                status_code=401,
                detail="invalid username or password",
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc
        return TokenResponse.from_result(result)

    @router.get(
        "/me",
        response_model=UserResponse,
    )
    def me(
        current_user: User = Depends(get_current_user),
    ) -> UserResponse:
        return UserResponse.from_user(current_user)

    return router
```

- [ ] **Step 6: 运行 Router 测试**

Run:

```powershell
python -m pytest tests/api/test_auth_router.py -q
```

Expected: `5 passed`。

- [ ] **Step 7: 提交**

```powershell
git add app/schemas/auth.py app/api/auth_router.py app/api/dependencies.py tests/api/test_auth_router.py
git commit -m "feat: expose register and login api"
```

---

### Task 5: 将现有会话 API 绑定到当前登录用户

**独立验收产物：** 未登录不能创建/读取/修改会话；登录用户无需也不能提交
`user_id`；用户 B 的 Token 不能访问用户 A 的会话。

**Files:**
- Modify: `app/schemas/chat.py:11-28`
- Modify: `app/api/router.py:13-78`
- Modify: `app/sessions/sqlite_store.py:130-150`
- Modify: `tests/api/test_router.py`
- Modify: `tests/sessions/test_sqlite_store.py`

**Interfaces:**
- Consumes: `get_current_user: Callable[..., User]`
- Produces: `create_conversation_router(*, chat_service, get_current_user) -> APIRouter`
- Ownership source: only `current_user.user_id`
- Error rule: missing/foreign conversation is always `404`

- [ ] **Step 1: 先修改 Router 测试表达新的安全契约**

在 `tests/api/test_router.py` 中让 `build_client()` 注入固定用户：

```python
from app.users.base import User


CURRENT_USER = User(
    user_id="authenticated-user",
    username="alice",
    password_hash="not-exposed",
    created_at="2026-07-24 00:00:00",
)


def build_client():
    service = FakeChatService()
    app = FastAPI()

    def get_current_user():
        return CURRENT_USER

    app.include_router(
        create_conversation_router(
            chat_service=service,
            get_current_user=get_current_user,
        )
    )
    return TestClient(app), service
```

把创建会话、聊天和修改提示词的请求体分别改成：

```python
json={"system_prompt": "be helpful"}
json={"question": "Hello"}
json={"system_prompt": "new prompt"}
```

并把三个 service 调用断言里的 `"user-a"` 改成
`"authenticated-user"`。新增以下测试：

```python
def test_client_cannot_override_authenticated_user():
    client, service = build_client()

    response = client.post(
        "/conversations/",
        json={"user_id": "attacker", "system_prompt": "be helpful"},
    )

    assert response.status_code == 422
    assert service.calls == []


def test_conversation_router_requires_authentication():
    service = FakeChatService()
    app = FastAPI()

    def reject_unauthenticated():
        raise HTTPException(
            status_code=401,
            detail="authentication required",
        )

    app.include_router(
        create_conversation_router(
            chat_service=service,
            get_current_user=reject_unauthenticated,
        )
    )
    client = TestClient(app)

    assert client.post("/conversations/", json={}).status_code == 401
```

同时把 import 从 `creat_router` 改为 `create_conversation_router`，并从
FastAPI import 中增加 `HTTPException`。

- [ ] **Step 2: 增加错误所有者不能更新提示词的 Store 回归测试**

在 `tests/sessions/test_sqlite_store.py` 增加：

```python
def test_update_system_prompt_hides_wrong_owner(tmp_path):
    store = SQLiteSessionStore(tmp_path / "chat.db")
    conversation = store.create_conversation(user_id="user-a")

    with pytest.raises(ConversationNotFoundError):
        store.update_system_prompt(
            user_id="user-b",
            conversation_id=conversation.conversation_id,
            system_prompt="not allowed",
        )
```

- [ ] **Step 3: 运行测试并确认失败**

Run:

```powershell
python -m pytest tests/api/test_router.py tests/sessions/test_sqlite_store.py -q
```

Expected: Router import/signature、旧 `user_id` schema 和错误所有者更新行为导致失败。

- [ ] **Step 4: 从聊天请求 Schema 删除 user_id 并拒绝多余字段**

在 `app/schemas/chat.py` 中 import `ConfigDict`，并把三个请求模型替换为：

```python
class CreateConversationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    system_prompt: str | None = None


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1)


class UpdateSystemPromptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    system_prompt: str = Field(min_length=1)
```

保留现有响应模型不变。

- [ ] **Step 5: 改造会话 Router 使用 current_user**

在 `app/api/router.py`：

1. 增加 `Callable`、`Depends`、`User` import。
2. 把 `creat_router` 重命名为 `create_conversation_router`。
3. 增加参数 `get_current_user: Callable[..., User]`。
4. 每个 endpoint 增加
   `current_user: User = Depends(get_current_user)`。
5. 删除所有 `request.user_id.strip()` 和 blank user ID 校验。
6. 三处调用统一传 `user_id=current_user.user_id`。

创建会话 endpoint 的完整目标形态：

```python
@router.post(
    "/conversations/",
    response_model=ConversationCreated,
    status_code=201,
    tags=["conversations"],
)
def create_conversation(
    request: CreateConversationRequest,
    current_user: User = Depends(get_current_user),
) -> ConversationCreated:
    system_prompt = (
        request.system_prompt.strip()
        if request.system_prompt
        else None
    )
    conversation = chat_service.create_conversation(
        user_id=current_user.user_id,
        system_prompt=system_prompt,
    )
    return ConversationCreated(
        conversation_id=conversation.conversation_id
    )
```

聊天和提示词 endpoint 继续保留 question/prompt 空白校验及
`ConversationNotFoundError → 404` 映射，只替换身份来源。

- [ ] **Step 6: 修复错误所有者更新零行不报错**

在 `SQLiteSessionStore.update_system_prompt()` 的同一数据库事务中，先调用：

```python
self._get_owned_row(
    connection=connection,
    user_id=user_id,
    conversation_id=conversation_id,
)
```

然后再执行原 UPDATE。这样不存在和非所有者都会抛出
`ConversationNotFoundError`，不会泄露资源是否存在。

- [ ] **Step 7: 运行会话边界测试和全量回归**

Run:

```powershell
python -m pytest tests/api/test_router.py tests/sessions/test_sqlite_store.py -q
python -m pytest -q
```

Expected: selected tests pass；全量测试 pass，且旧测试中不再发送 `user_id`。

- [ ] **Step 8: 提交**

```powershell
git add app/schemas/chat.py app/api/router.py app/sessions/sqlite_store.py tests/api/test_router.py tests/sessions/test_sqlite_store.py
git commit -m "feat: bind conversations to authenticated user"
```

---

### Task 6: 装配生产应用、增加端到端验收和使用文档

**独立验收产物：** `main.py` 暴露完整认证链路；真实 SQLite User Store 和 Session
Store 共用数据库；从注册到登录、创建会话、跨用户拒绝均通过端到端测试；初学者能按
README 独立运行和手工验收。

**Files:**
- Modify: `main.py:1-40`
- Modify: `tests/test_main.py`
- Create: `tests/test_auth_flow.py`
- Create: `README.md`

**Interfaces:**
- Consumes: `get_chat_db_path()`、`get_auth_secret_key()`、`get_access_token_ttl_seconds()`
- Produces: 生产 `app` 同时包含 Auth Router 和受保护的 Conversation Router

- [ ] **Step 1: 写生产装配和完整流程失败测试**

在 `tests/test_main.py` 的环境准备中增加：

```python
monkeypatch.setenv(
    "AUTH_SECRET_KEY",
    "test-secret-key-that-is-at-least-32-characters",
)
```

并增加路径和装配断言：

```python
assert "/auth/register" in paths
assert "/auth/login" in paths
assert "/auth/me" in paths
assert hasattr(main, "auth_service")
assert hasattr(main, "user_store")
```

创建 `tests/test_auth_flow.py`：

```python
import importlib
import sys

from fastapi.testclient import TestClient


def load_app(monkeypatch, tmp_path):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-key")
    monkeypatch.setenv(
        "AUTH_SECRET_KEY",
        "test-secret-key-that-is-at-least-32-characters",
    )
    monkeypatch.setenv(
        "CHAT_DB_PATH",
        str(tmp_path / "app.db"),
    )
    sys.modules.pop("main", None)
    return importlib.import_module("main").app


def register_and_login(client, username):
    register_response = client.post(
        "/auth/register",
        json={
            "username": username,
            "password": "correct-horse-42",
        },
    )
    assert register_response.status_code == 201

    login_response = client.post(
        "/auth/login",
        json={
            "username": username,
            "password": "correct-horse-42",
        },
    )
    assert login_response.status_code == 200
    return login_response.json()["access_token"]


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_register_login_and_conversation_ownership(
    monkeypatch,
    tmp_path,
):
    client = TestClient(load_app(monkeypatch, tmp_path))
    alice_token = register_and_login(client, "alice")
    bob_token = register_and_login(client, "bob")

    unauthenticated = client.post("/conversations/", json={})
    created = client.post(
        "/conversations/",
        json={"system_prompt": "be helpful"},
        headers=bearer(alice_token),
    )
    conversation_id = created.json()["conversation_id"]
    cross_user = client.post(
        f"/conversations/{conversation_id}/chat/",
        json={"question": "steal history"},
        headers=bearer(bob_token),
    )

    assert unauthenticated.status_code == 401
    assert created.status_code == 201
    assert cross_user.status_code == 404
```

- [ ] **Step 2: 运行测试并确认失败**

Run:

```powershell
python -m pytest tests/test_main.py tests/test_auth_flow.py -q
```

Expected: `main.py` 尚未装配认证服务，测试失败。

- [ ] **Step 3: 完成 main.py 生产装配**

`main.py` 的目标装配顺序如下：

```python
from functools import partial

from fastapi import FastAPI

from app.agent.runner import run_one_turn
from app.api.auth_router import create_auth_router
from app.api.dependencies import create_current_user_dependency
from app.api.router import create_conversation_router
from app.application.auth_service import AuthService
from app.application.chat_service import ChatService
from app.auth.tokens import AccessTokenService
from app.concurrency.conversation_locks import ConversationLockRegistry
from app.core.config import (
    create_llm_client,
    get_access_token_ttl_seconds,
    get_auth_secret_key,
    get_chat_db_path,
)
from app.sessions.sqlite_store import SQLiteSessionStore
from app.tools.registry import TOOL_DEFINITIONS, TOOL_FUNCTIONS
from app.users.sqlite_store import SQLiteUserStore


app = FastAPI(title="SupportPilot")
client = create_llm_client()
database_path = get_chat_db_path()

session_store = SQLiteSessionStore(database_path=database_path)
user_store = SQLiteUserStore(database_path=database_path)
token_service = AccessTokenService(
    secret_key=get_auth_secret_key(),
    ttl_seconds=get_access_token_ttl_seconds(),
)
auth_service = AuthService(
    user_store=user_store,
    token_service=token_service,
)
get_current_user = create_current_user_dependency(auth_service)

run_agent = partial(
    run_one_turn,
    client=client,
    tool_definitions=TOOL_DEFINITIONS,
    tool_functions=TOOL_FUNCTIONS,
    on_event=None,
)
chat_service = ChatService(
    store=session_store,
    run_agent=run_agent,
    locks=ConversationLockRegistry(),
)

app.include_router(
    create_auth_router(
        auth_service=auth_service,
        get_current_user=get_current_user,
    )
)
app.include_router(
    create_conversation_router(
        chat_service=chat_service,
        get_current_user=get_current_user,
    )
)
```

- [ ] **Step 4: 编写 README**

创建 `README.md`，至少包含以下可直接执行内容：

````markdown
# SupportPilot

一个使用 FastAPI、SQLite 和大模型工具调用构建的学习型 Agent 后端。

## 安装

```powershell
python -m pip install -r requirements.txt
```

复制 `.env.example` 的变量到当前终端，并替换真实密钥：

```powershell
$env:DEEPSEEK_API_KEY = "your-key"
$env:AUTH_SECRET_KEY = "use-at-least-32-random-characters"
$env:CHAT_DB_PATH = "chat_history.db"
```

## 启动

```powershell
python -m uvicorn main:app --reload
```

打开 `http://127.0.0.1:8000/docs`。

## 注册和登录

```powershell
$registered = Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8000/auth/register" `
  -ContentType "application/json" `
  -Body '{"username":"alice","password":"correct-horse-42"}'

$login = Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8000/auth/login" `
  -ContentType "application/json" `
  -Body '{"username":"alice","password":"correct-horse-42"}'

$headers = @{ Authorization = "Bearer $($login.access_token)" }

Invoke-RestMethod `
  -Method Get `
  -Uri "http://127.0.0.1:8000/auth/me" `
  -Headers $headers
```

## 创建受保护会话

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8000/conversations/" `
  -Headers $headers `
  -ContentType "application/json" `
  -Body '{"system_prompt":"You are a helpful support agent."}'
```

Access Token 默认 30 分钟过期。本阶段没有 Refresh Token；过期后重新登录。

## 测试

```powershell
python -m pytest -q
```
````

- [ ] **Step 5: 运行局部和全量自动化验收**

Run:

```powershell
python -m pytest tests/test_main.py tests/test_auth_flow.py -q
python -m pytest -q
```

Expected:

- 装配/端到端测试全部通过。
- 全量测试全部通过。
- 不需要真实调用 DeepSeek API。
- 现有 Agent Runner、会话持久化和并发测试没有回归。

- [ ] **Step 6: 手工安全验收**

启动服务后依次验证：

1. 注册 `alice` 成功，响应没有 `password` 或 `password_hash`。
2. 再注册 `ALICE` 返回 409。
3. 错误密码和不存在用户均返回完全相同的 401 body。
4. 不携带 Token 调用 `/conversations/` 返回 401。
5. 携带 Alice Token 创建会话成功，请求体不需要 `user_id`。
6. 请求体额外加入 `"user_id": "bob"` 返回 422。
7. 注册 Bob，使用 Bob Token 访问 Alice 会话返回 404。
8. 修改 JWT 任意字符后访问 `/auth/me` 返回 401。
9. SQLite `users.password_hash` 以 `$2b$12$` 开头且不含明文密码。

- [ ] **Step 7: 提交**

```powershell
git add main.py tests/test_main.py tests/test_auth_flow.py README.md
git commit -m "feat: wire authenticated application flow"
```

---

## 4. Task 依赖与独立验收矩阵

| Task | 依赖 | Reviewer 可独立拒绝/接受的产物 | 验收命令 |
|---|---|---|---|
| 1 用户存储 | 无 | users 表、唯一用户名、按 ID/用户名查询 | `python -m pytest tests/users -q` |
| 2 安全组件 | 无 | bcrypt、JWT、强制密钥配置 | `python -m pytest tests/auth tests/core/test_config.py -q` |
| 3 AuthService | Task 1、2 | 注册/登录/Token→User 用例 | `python -m pytest tests/application/test_auth_service.py -q` |
| 4 Auth API | Task 3 | register/login/me HTTP 契约 | `python -m pytest tests/api/test_auth_router.py -q` |
| 5 会话鉴权 | Task 1、4 | 客户端不能伪造 user_id，所有权边界正确 | `python -m pytest tests/api/test_router.py tests/sessions/test_sqlite_store.py -q` |
| 6 生产装配 | Task 1～5 | 完整认证流程、文档和全量回归 | `python -m pytest -q` |

## 5. 完成定义

只有同时满足以下条件，注册登录功能才算完成：

- 用户能注册、登录和查询当前身份。
- 数据库只保存密码哈希。
- 重复用户名大小写不敏感。
- Access Token 过期或被篡改后不能访问资源。
- 所有会话请求 Schema 已删除 `user_id`。
- 所有会话 Router 都使用 `current_user.user_id`。
- 用户 B 不能访问或修改用户 A 的会话。
- 注册/登录错误不会泄露敏感数据或用户名存在性。
- `AUTH_SECRET_KEY` 未配置或过短时应用明确拒绝启动。
- README 中的启动和手工验收命令可执行。
- 全量 pytest 通过。

## 6. 后续单独规划，不混入本次实现

完成本计划后，按优先级另写计划：

1. Refresh Token 轮换、服务端撤销表和真正的登出。
2. 登录失败限流、审计事件和安全告警。
3. 修改密码、忘记密码、邮箱验证。
4. Organization、Membership、`admin/agent/viewer` RBAC。
5. Alembic 迁移和 PostgreSQL 生产数据库。
6. 浏览器前端采用 HttpOnly + Secure + SameSite Cookie 的登录态方案。

这些能力与当前认证 MVP 的边界不同；本次预留 `UserStore`、`AuthService` 和 Token
组件接口，后续可以扩展而不修改 Agent Runner。
