# SupportPilot 前端 HTTP API 盘点文档（api-inventory）

> 本文档面向后续前端开发，只盘点**真正提供给前端使用的 HTTP API**。
> Agent Tool（`propose_refund` / `search_knowledge` 等）、数据库 Store、
> 内部 Python 方法一律不计入。
>
> - 依据版本：仓库 HEAD `42b83d9`（第三阶段，客服对话闭环）
> - 核对方式：静态阅读 `main.py` / `app/api/*` / `app/schemas/*` /
>   `app/api/dependencies.py` / `tests/api/*`，并在导入阶段用临时环境变量
>   在内存中生成 OpenAPI（`app.openapi()`，未启动服务器、未修改任何配置、
>   DB 指向临时目录）交叉核对；两种口径结果一致。
> - 统计结果：**20 个路径、23 个 HTTP 操作**（不含 FastAPI 自带的
>   `/docs`、`/openapi.json` 等）。

---

## 1. 业务背景与模块分布（速览）

业务问题：面向电商售后客服团队的**多租户工单处理 Agent**——客服（agent）在
对话中查询本企业客户/订单/物流、辅助创建工单、检索企业知识库，并向 Agent
提出退款/补偿提案；管理员（admin）审批后由确定性工作流执行（当前为模拟
业务记录）。

模块与接口数分布：

| 模块 | 前缀 | 操作数 | 说明 |
| --- | --- | --- | --- |
| 认证 authentication | `/auth` | 3 | 注册、登录、查询当前用户（登录态恢复） |
| 组织 organizations | `/organizations` | 3 | 创建企业、列出我的企业、添加成员 |
| 会话与聊天 conversations | `/conversations` | 5 | 会话列表/新建、单轮聊天、历史读取、更新会话系统提示词 |
| 知识库 knowledge | `/knowledge` | 7 | 文档上传/新版本、列表/详情、停用/启用、入库任务查询 |
| 退款/补偿审批与 Run | `/approvals`、`/action-runs` | 5 | 审批列表/详情/决定、Run 状态/恢复 |

角色与权限矩阵（详见各接口小节）：

| 能力 | 未登录 | agent | admin |
| --- | --- | --- | --- |
| 注册 / 登录 | ✅ | — | — |
| 查看/更新自己的登录态（/auth/me） | ❌ | ✅ | ✅ |
| 创建企业、列出自己的企业 | ❌ | ✅ | ✅ |
| 添加企业成员 | ❌ | ❌ | ✅（且必须是该企业成员） |
| 会话列表/创建/聊天/历史读取/改提示词（限自己所属企业+自己创建的会话） | ❌ | ✅ | ✅ |
| 知识库读接口（列表/详情/任务） | ❌ | ✅ | ✅ |
| 知识库写接口（上传/新版本/停用/启用） | ❌ | ❌ | ✅ |
| 审批列表/详情、Run 状态（本企业范围） | ❌ | ✅ | ✅ |
| 审批决定、显式恢复 Run | ❌ | ❌ | ✅ |
| 跨企业访问任何租户资源 | ❌ | ❌ | ❌（一律伪装为 404） |

## 2. 全局通用规则

### 2.1 全局认证规则（依据 `app/api/dependencies.py`、`app/auth/tokens.py`）

- 除 `POST /auth/register`、`POST /auth/login` 外，**所有接口都需要
  Bearer Token**。
- 请求头写法：`Authorization: Bearer <access_token>`（登录接口返回的
  `access_token`）。
- Token 是 HS256 JWT，载荷 `{sub: user_id, type: "access", iss: "supportpilot",
  iat, exp}`；过期时间由服务端 `ACCESS_TOKEN_TTL_SECONDS` 决定（代码默认
  **1800 秒**，仓库 `.env.example` 为 **604800 秒**，以实际部署为准；登录响应
  里的 `expires_in` 会如实返回秒数）。
- 无 Token：`401 {"detail": "authentication required"}`
  + `WWW-Authenticate: Bearer`；
- Token 无效或过期：`401 {"detail": "invalid or expired access token"}`
  + `WWW-Authenticate: Bearer`。
- 服务端**没有 refresh token、没有登出/吊销接口**：Token 过期后只能重新登录。
  前端应缓存登录态并依赖 `/auth/me` 做刷新恢复（该接口注释即为此用途）。
- 用户名规则（注册/登录都会被服务端 `strip + casefold` 归一化）：
  小写字母/数字/下划线 `[a-z0-9_]{3,32}`，比较不区分大小写；
  密码为 **8–72 个 UTF-8 字节**（bcrypt 上限 72 字节），无复杂度要求。

### 2.2 多租户 Header 规则（依据 `dependencies.py` 的 `get_current_tenant`）

- 需要租户上下文的接口必须携带请求头 `X-Organization-ID: <organization_id>`，
  取值来自 `GET /organizations/` 响应中的 `organization_id`。
- 缺失或全空白：`400 {"detail": "organization context required"}`。
- Header 指向的企业中当前用户**不是成员**：一律 `404 {"detail":
  "organization not found"}`（与“企业不存在”不可区分，防枚举）。
- Header 只用于取「组织」，**当前用户在组织里的角色由服务端实时从数据库
  Membership 重新读取**（不信任 Token 或缓存，`actions/service.py` 每次决定
  都会重查），前端传入的角色只是展示用。
- 注意：OpenAPI 里该 Header 被标记为 `required: false`（代码参数默认 None
  后再手检），**不要信 OpenAPI 的 required 标志**——租户接口缺头就是 400。
- 哪些接口不需要该头：`/auth/*` 全部、`/organizations/*` 全部（成员管理接口
  的企业 id 在路径里，服务端自行校验）；其余接口全部需要。

### 2.3 Content-Type

- JSON 接口：请求 `application/json`，响应 `application/json`；所有 JSON
  请求模型均 `extra="forbid"`（多传字段直接 422）。
- 上传接口（知识库）：`multipart/form-data`，见第 5.1 节。

### 2.4 错误响应结构与全局错误处理建议

当前后端存在 **三种错误形态并存**，前端封装 HTTP 客户端时必须统一兼容：

| 形态 | 示例 | 出现位置 |
| --- | --- | --- |
| 字符串 detail | `{"detail": "invalid username or password"}` | auth / organization / 会话路由的手动 HTTPException（401/403/404/409/422 消息） |
| 结构化 detail | `{"detail": {"code": "DOCUMENT_NOT_FOUND", "message": "..."}}` | knowledge 与 action 路由的领域错误 |
| FastAPI 校验 422 | `{"detail": [{"type": "...", "loc": [...], "msg": "...", ...}]}` | 所有带请求体/Query/枚举/Path 约束的请求校验失败 |

建议的全局处理约定：

1. 统一拦截器先看 HTTP 状态码，再解析 `detail`：
   - `detail` 是字符串 → 直接当用户可读消息（部分英文消息建议前端再映射中文）；
   - `detail` 是对象且含 `code` → 优先用 `code` 驱动逻辑分支（如
     `RUN_NOT_RESUMABLE` 提示“不可恢复”、`APPROVAL_ALREADY_DECIDED` 提示刷新列表），
     `message` 兜底展示；
   - `detail` 是数组（422 校验错误）→ 逐条取 `msg`/`loc` 拼接展示，或按字段
     定位到表单控件。
2. 401（除登录接口自身返回的 401）一律视为登录过期：清空本地登录态 → 跳登录页。
3. 429/5xx 归为服务端/上游问题，做“稍后重试”类提示；503 的 action/knowledge
   错误语义为“事实已保存但工作流/入库未完成，可稍后重试或查询”。
4. 租户接口的 404 是“不存在或不属于当前企业”的模糊语义，UI 统一提示
   “资源不存在或已被移除”，不要提示跨企业猜测。

### 2.5 接口命名 / 结构上的注意点（影响前端的既有事实）

- 会话相关路径**带末尾斜杠**（`/conversations/`、`/conversations/{id}/chat/`、
  `/conversations/{id}/messages/`、`/conversations/{id}/system-prompt/`），
  `/auth/login` 等则不带。FastAPI 默认
  `redirect_slashes=True`，路径写错斜杠会返回 307 跳转；前端应**精确按本文档
  路径调用**，避免多一次跳转（无 CORS 时跨域 307 更麻烦）。
- 各 store 的时间格式不一致（见 5.2），前端需要统一解析。
- `AgentEvent.timestamp` 由 `datetime.now()`（**服务器本地时间、无时区**）生成，
  与其它 UTC 字段语义不同，展示事件时间时注意偏差（建议后续后端统一为 UTC，
  属缺口建议，未修改）。
- OpenAPI 中 `X-Organization-ID` 的 required 标志与实际行为不符（见 2.2）。
- 路由工厂名 `creat_conversation_router` 为历史拼写（`app/api/router.py`），
  不影响 HTTP 面。
- 会话/审批/知识库列表接口均**不返回总数**，分页/滚动加载由前端自行推断
  （拉一页小于 limit 即视为到底）。

---

## 3. 接口汇总表

图例：T=需要 Bearer Token；O=需要 X-Organization-ID；G=需为企业成员（角色不限）；
A=仅 admin；✅/❌ 同理。P=公开。

| # | 模块 | Method | Path | 用途 | T | O | 权限 | 成功码 | 建议页面/操作 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 认证 | POST | `/auth/register` | 注册账号 | ❌ | ❌ | P | 201 | 注册页 |
| 2 | 认证 | POST | `/auth/login` | 登录获取 Token | ❌ | ❌ | P | 200 | 登录页 |
| 3 | 认证 | GET | `/auth/me` | 当前用户（登录态恢复） | ✅ | ❌ | G(任意登录用户) | 200 | 应用启动/刷新时的会话校验；个人信息页 |
| 4 | 组织 | POST | `/organizations/` | 创建企业（创建者自动成为 admin） | ✅ | ❌ | 登录用户 | 201 | 企业选择/创建页 |
| 5 | 组织 | GET | `/organizations/` | 列出我加入的企业及我在各企业的角色 | ✅ | ❌ | 登录用户 | 200 | 企业选择/切换页（顶栏企业下拉） |
| 6 | 组织 | POST | `/organizations/{organization_id}/members/` | 添加成员（按用户名，指定角色） | ✅ | ❌ | A（且为本企业成员） | 201 | 成员管理页 |
| 7 | 会话 | POST | `/conversations/` | 新建会话（可选自定义 system prompt） | ✅ | ✅ | G | 201 | 客服对话页“新会话” |
| 8 | 会话 | GET | `/conversations/` | 我的会话列表（当前企业，分页） | ✅ | ✅ | G（会话归属人） | 200 | 客服对话页会话侧栏（加载更多） |
| 9 | 会话 | POST | `/conversations/{conversation_id}/chat/` | 发送问题并取整轮 Agent 回答（同步） | ✅ | ✅ | G（会话归属人） | 200 | 客服对话页发送框 |
| 10 | 会话 | GET | `/conversations/{conversation_id}/messages/` | 会话历史读取（只含安全问答文本） | ✅ | ✅ | G（会话归属人） | 200 | 进入会话时加载历史；刷新恢复 |
| 11 | 会话 | PUT | `/conversations/{conversation_id}/system-prompt/` | 更新会话系统提示词 | ✅ | ✅ | G（会话归属人） | 200 | 会话设置 |
| 12 | 知识库 | POST | `/knowledge/documents/` | 上传 Markdown/TXT 文档并同步入库 | ✅ | ✅ | A | 201 | 文档上传 |
| 13 | 知识库 | GET | `/knowledge/documents/` | 文档列表（当前企业） | ✅ | ✅ | G | 200 | 知识库列表页 |
| 14 | 知识库 | GET | `/knowledge/documents/{document_id}/` | 文档详情：版本历史 + 最近入库任务 | ✅ | ✅ | G | 200 | 知识库详情页 |
| 15 | 知识库 | POST | `/knowledge/documents/{document_id}/versions/` | 上传新版本 | ✅ | ✅ | A | 201 | 详情页“上传新版本” |
| 16 | 知识库 | POST | `/knowledge/documents/{document_id}/disable/` | 停用文档 | ✅ | ✅ | A | 200 | 列表/详情停用按钮 |
| 17 | 知识库 | POST | `/knowledge/documents/{document_id}/enable/` | 重新启用文档 | ✅ | ✅ | A | 200 | 列表/详情启用按钮 |
| 18 | 知识库 | GET | `/knowledge/ingestion-jobs/{job_id}/` | 查询入库任务状态/错误 | ✅ | ✅ | G | 200 | 上传失败后的任务详情/重试提示 |
| 19 | 审批 | GET | `/approvals/` | 审批列表（可筛选状态、分页） | ✅ | ✅ | G | 200 | 审批列表页（待办/全部 Tab） |
| 20 | 审批 | GET | `/approvals/{approval_id}/` | 审批详情（版本、Run、决定） | ✅ | ✅ | G | 200 | 审批详情页 |
| 21 | 审批 | POST | `/approvals/{approval_id}/decisions/` | 批准/修改后批准/拒绝 | ✅ | ✅ | A | 201/202/200 | 审批详情页决定表单 |
| 22 | Run | GET | `/action-runs/{run_id}/` | Action Run 状态查询 | ✅ | ✅ | G | 200 | Run 状态面板/失败恢复页 |
| 23 | Run | POST | `/action-runs/{run_id}/resume/` | 显式恢复 Run | ✅ | ✅ | A | 200 | Run 状态面板“恢复”按钮 |

---

## 4. 分模块接口明细

> 请求/响应模型的字段级定义见「附录 A」，错误码表见「附录 B」。
> 未登录用户调用所有 T=✅ 的接口均得 401（见 2.1）。

### 4.1 认证 `/auth`（3 个）

#### 4.1.1 POST `/auth/register` — 注册（公开，201）

- 用途：创建账号。**注册成功后不直接登录**，需要再调 `/auth/login` 拿 Token。
- 鉴权：无需 Token；无需 X-Organization-ID。
- Content-Type：`application/json`；请求体 `RegisterRequest`（extra=forbid）：

| 字段 | 类型 | 必填 | 约束 |
| --- | --- | --- | --- |
| username | string | ✅ | 归一化（strip+小写）后匹配 `^[a-z0-9_]{3,32}$` |
| password | string | ✅ | 8–72 个 UTF-8 字节 |

- 成功：`201`，`UserResponse`：
  `{user_id: string, username: string, created_at: string}`（不含任何密码字段；
  `created_at` 为 UTC `"%Y-%m-%d %H:%M:%S"` 文本）。
- 错误：
  - `409 {"detail": "username already exists"}`（用户名已存在，大小写不敏感）；
  - `422 {"detail": "username must contain 3-32 lowercase letters, digits, or
    underscores"}`（用户名非法）；
  - `422 {"detail": "password must contain between 8 and 72 utf-8 bytes"}`；
  - `422` FastAPI 校验数组（请求体缺字段/超长/多传字段等）。
- 源码：`app/api/auth_router.py`（`register`）、`app/application/auth_service.py`
  （`registry`/`_normalize_username`）、`app/auth/passwords.py`、
  `app/schemas/auth.py`。
- 测试：`tests/api/test_auth_router.py`（`test_register_returns_public_user_
  without_password`、`test_duplicate_registration_returns_409`）、
  `tests/application/test_auth_service.py`。
- 建议页面：注册页。前端应做同规则前置校验（小写字母/数字/下划线、3–32；
  密码 ≥8），但**以服务端 422 为准**。

#### 4.1.2 POST `/auth/login` — 登录（公开，200）

- 用途：校验用户名密码，签发访问 Token。
- 鉴权：无需 Token；无需 X-Organization-ID。
- Content-Type：`application/json`；请求体 `LoginRequest`（extra=forbid）：
  `username`（3–32）、`password`（8–72），约束同上。
- 成功：`200`，`TokenResponse`：
  `{access_token: string, token_type: "bearer", expires_in: int(秒)}`。
- 错误：
  - `401 {"detail": "invalid username or password"}` +
    `WWW-Authenticate: Bearer`（用户名不存在与密码错误**返回完全一致**，防
    枚举；前端统一提示“用户名或密码错误”）；
  - `422` FastAPI 校验数组。
- 源码：`app/api/auth_router.py`（`login`）、`app/application/auth_service.py`
  （`login`，含假哈希防时序枚举）、`app/auth/tokens.py`。
- 测试：`tests/api/test_auth_router.py`（`test_login_and_me_round_trip`、
  `test_login_does_not_reveal_whether_username_exists`）。
- 建议页面：登录页。前端拿到 `access_token` 后本地持久化（localStorage /
  sessionStorage 策略见 5.6），并记录 `expires_in` 做预过期提醒。

#### 4.1.3 GET `/auth/me` — 当前用户（登录态恢复，200）

- 用途：用 Token 换取当前用户信息，判断“是否仍处于登录状态”
  （源码注释明确写给前端刷新页面用）。
- 鉴权：Bearer；无需 X-Organization-ID。
- 成功：`200`，`UserResponse`（同注册响应，含 `user_id/username/created_at`）。
- 错误：`401`（见 2.1 两种 detail）。
- 源码：`app/api/auth_router.py`（`me`）、`app/api/dependencies.py`。
- 测试：`tests/api/test_auth_router.py`（`test_login_and_me_round_trip`、
  `test_me_rejects_missing_and_invalid_bearer_token`）。
- 建议页面：应用启动/路由守卫中调用；个人信息展示（用户名、注册时间）。

### 4.2 组织 `/organizations`（3 个）

> 本模块不校验 X-Organization-ID——企业 id 走路径/请求体，成员关系由服务端
> 校验；但注意「添加成员」同时校验“我是该企业 admin”。

#### 4.2.1 POST `/organizations/` — 创建企业（201）

- 用途：创建企业，创建者自动成为该企业 admin 成员。注册用户尚无企业时可从
  这里开始“多租户旅程”。
- 鉴权：Bearer。
- Content-Type：`application/json`；`CreateOrganizationRequest`（extra=forbid）：
  `name: string`（Pydantic 1–100；服务端 strip 后要求 **2–100** 字符，
  `1 < len < 2` 会得 422）。
- 成功：`201`，`OrganizationResponse {organization_id, name}`。
- 错误：`422 {"detail": "organization name must contain 2-100 characters"}`；
  `422` 校验数组。
- 源码：`app/api/organization_router.py`、`app/application/organization_service.py`
  （`MIN_ORGANIZATION_NAME_LENGTH=2`）。
- 测试：`tests/api/test_organization_router.py`
  （`test_create_and_list_organizations`）。
- 建议页面：企业选择/创建页（创建后自动出现在企业列表，前端可直接切换到它）。

#### 4.2.2 GET `/organizations/` — 我的企业列表（200）

- 用途：列出当前用户加入的所有企业，**并给出我在每个企业的角色**
  （`role`），是企业切换与前端权限展示的数据源。
- 鉴权：Bearer。
- 成功：`200`，`OrganizationAccessResponse[]`：
  `[{organization_id, name, role}]`，`role ∈ {admin, agent}`。
  排序为 SQL 层确定性 `ORDER BY o.created_at, o.id`（创建时间升序，老企业在
  前）；前端如需“最近加入在前”可自行倒序。
- 错误：`401`。
- 源码：`app/api/organization_router.py`、`app/organizations/sqlite_store.py`
  （`list_for_user`）。
- 测试：`tests/api/test_organization_router.py`、`tests/organizations/
  test_organization_store.py`、`tests/application/test_organization_service.py`。
- 建议页面：登录后首页/企业选择页/顶栏企业切换器。**前端把「当前企业
  + 角色」保存在全局状态，所有租户请求自动带 `X-Organization-ID`。**

#### 4.2.3 POST `/organizations/{organization_id}/members/` — 添加成员（201）

- 用途：把**已注册用户**（按精确用户名）加入本企业并指定角色。
- 鉴权：Bearer。**不读 X-Organization-ID**，企业 id 在路径。
- Path 参数：`organization_id`（必须是「我」为 admin 的企业，否则按下面错误码）。
- Content-Type：`application/json`；`AddMemberRequest`（extra=forbid）：
  `username: string`（3–32，规则同注册，会被 strip+casefold 后精确查找）、
  `role: "admin" | "agent"`。
- 成功：`201`，`MembershipResponse {organization_id, user_id, role}`。
- 错误（detail 均为字符串）：
  - `404 {"detail": "organization not found"}`：企业不存在或我不是其成员
    （不区分，防枚举）；
  - `403 {"detail": "admin role required"}`：我是成员但角色是 agent；
  - `404 {"detail": "user not found"}`：目标用户名不存在；
  - `409 {"detail": "user is already an organization member"}`；
  - `422` 校验数组（role 非法枚举、username 格式等）。
- 源码：`app/api/organization_router.py`、`app/application/organization_service.py`
  （`add_member` 先查我角色再查目标用户）、`app/users/sqlite_store.py`。
- 测试：`tests/api/test_organization_router.py`（`test_admin_can_add_member_and_
  duplicate_returns_409`、`test_unknown_member_username_returns_404`、
  `test_agent_cannot_add_member`）。
- 建议页面：成员管理页。**缺口提醒**：没有成员列表/移除/改角色接口，见 5.4；
  且“添加”需要输入对方已注册的精确用户名，UI 上要说明规则。

### 4.3 会话与聊天 `/conversations`（5 个）

> 会话归属规则：会话属于「用户 + 企业」二元组——`ChatService`/`SessionStore`
> 所有读取都带 `organization_id + user_id`。跨用户、跨企业访问一律
> `404 {"detail": "conversation not found error"}`（会话对当前登录用户私有，
> 企业内成员之间不可见彼此的会话）。
> 会话标题**不落库**：`GET /conversations/` 响应的 `title` 由首条用户消息
> 实时推导（无消息显示「新会话」），因此不存在会话重命名接口的语义基础。

#### 4.3.1 POST `/conversations/` — 新建会话（201）

- 用途：为「当前企业 + 当前用户」创建新会话，返回服务端会话 id。
- 鉴权：Bearer + X-Organization-ID（成员）。
- Content-Type：`application/json`；`CreateConversationRequest`（extra=forbid）：
  `system_prompt?: string | null`（可选；strip 后为空等同不设置）。
- 成功：`201`，`ConversationCreated {conversation_id: string}`。
- 错误：`401`；`400 {"detail": "organization context required"}`（缺头）；
  `404 {"detail": "organization not found"}`（非成员）；`422` 校验数组。
- 源码：`app/api/router.py`、`app/application/chat_service.py`、
  `app/sessions/sqlite_store.py`。
- 测试：`tests/api/test_router.py`（`test_create_conversation_returns_server_id`）、
  `tests/application/test_chat_service.py`、`tests/sessions/test_sqlite_store.py`。
- 建议页面：客服对话页“新建会话”。**同一个会话只能由创建它的用户使用**，
  若做“企业共享会话/工单流转”属后端缺口（见 5.4）。

#### 4.3.2 GET `/conversations/` — 我的会话列表（200，分页）

- 用途：列出「当前企业 + 当前用户」自己的会话（会话对用户私有，不返回
  同企业其他人的会话；也不返回其它企业的会话）。
- 鉴权：Bearer + X-Organization-ID。
- Query 参数：

| 名称 | 类型 | 必填 | 约束 |
| --- | --- | --- | --- |
| `limit` | int | 否 | 1–100，默认 50 |
| `offset` | int | 否 | ≥0，默认 0 |

- 排序：`updated_at DESC`；同一秒（文本相同）以 `conversation_id` 倒序为
  稳定次级排序，分页顺序可复现。
- 成功：`200`，`ConversationListItem[]`（字段见附录 A.5）：
  `{conversation_id, title, created_at, updated_at}`。`title` 由首条用户
  消息推导（去首尾空白 → 连续空白折叠为单空格 → 截断到 30 字符）；尚无
  消息的空会话显示「新会话」。
- 错误：`401`；`400`（缺头）；`404`（非成员企业）；`422` 校验数组
  （limit/offset 越界、类型错误）。
- 源码：`app/api/router.py`（`list_conversations`）、
  `app/application/chat_service.py`（标题推导 `derive_conversation_title`）、
  `app/sessions/sqlite_store.py`（`list_conversations`）。
- 测试：`tests/api/test_router.py`（分页透传与 422）、
  `tests/application/test_chat_service.py`（标题推导）、
  `tests/sessions/test_sqlite_store.py`（归属/排序/分页/首条消息正文）、
  `tests/test_main.py`（真实 app 认证与隔离）。
- 建议页面：客服对话页会话侧栏。**该接口不返回总数**：分页/“加载更多”
  由前端按“最近一页是否拉满”推断（与审批/知识库列表口径一致，见 2.5）。

#### 4.3.3 POST `/conversations/{conversation_id}/chat/` — 单轮聊天（200，同步）

- 用途：发送一条客服问题，Agent 执行多轮工具调用（查订单/物流/建工单/
  检索知识库/提退款补偿提案）后一次性返回完整结果。**普通同步 HTTP 请求，
  非流式、非 WebSocket**（全局无 StreamingResponse/WebSocket 端点）。
- 鉴权：Bearer + X-Organization-ID。
- Path 参数：`conversation_id`（strip 后为空 → 422）。
- Content-Type：`application/json`；`ChatRequest`（extra=forbid）：
  `question: string`（strip 后非空，长度 ≥1；空白 → 422）。
- 成功：`200`，`LLMResponse`（字段见附录 A.6）：
  - `llm_answer: string|null`——最终自然语言回答；
  - `llm_reasoning_content: string|null`——模型推理内容（默认不展示给用户，
    仅供调试/内部使用）；
  - `events: AgentEvent[]`——每轮工具调用事件（过程可视化、步骤日志）；
  - `citations: Citation[]`——知识库结构化引用（C1..Cn，见 5.3）；
  - `retrieval_summary: {strategy, round_count, evidence_status, latency_ms}`
    ——检索摘要（evidence_status ∈ SUFFICIENT/INSUFFICIENT，大写）；
  - `answer_incomplete: bool`——证据不足/引用异常被确定性标记；
  - `pending_approvals: PendingApproval[]`——本轮提出的待审批提案（**关键
    结构化字段，禁止解析自然语言，见 5.3**）。
- 错误：`401/400/404`（同 4.3.1）；`404 {"detail": "conversation not found
  error"}`；`422 {"detail": "question must not be blank"}` 等字符串 detail。
- 耗时特征（对前端很重要）：一次请求 = LLM 多轮 × 工具调用，可能包含知识
  检索（单次预算 ≤30s）；**没有流式进度**，请求可能持续数十秒甚至更久，
  需要合理超时与等待 UI（见 5.5 的缺口建议）。前端应使用独立长超时且
  **不要自动重试**（客户端超时不代表服务端未执行，见 5.5 与 4.3.4 的刷新
  历史提示）。
- 源码：`app/api/router.py`（`chat`）、`app/application/chat_service.py`、
  `app/agent/support_runner.py`、`app/agent/runner.py`、`app/schemas/chat.py`。
- 测试：`tests/api/test_router.py`（`test_chat_passes_user_and_conversation_
  to_service`、`test_missing_or_unowned_conversation_returns_404`、
  `test_blank_question_and_prompt_return_422`）、`tests/agent/`、
  `tests/application/test_chat_service.py`。
- 建议页面：客服对话页发送框。发送中锁定输入，展示“思考中”；对返回的
  `pending_approvals` 渲染成卡片（金额/原因/审批链接），对 `citations`
  渲染为角标引用。

#### 4.3.4 GET `/conversations/{conversation_id}/messages/` — 会话历史读取（200）

- 用途：读取指定会话的**安全历史**：只包含用户问题与 Agent 最终自然语言
  回答，供页面刷新后恢复对话。**服务端不保存**上一轮的 `citations`、
  `events`、`pending_approvals` 等结构化展示信息；history 响应**绝不包含**
  这些字段，也绝不包含 `reasoning_content` / `tool_calls` / 工具原始参数与
  结果 / system / tool 消息 / 幂等键等内部内容（`payload_json` 不会原样暴露）。
- 鉴权：Bearer + X-Organization-ID。
- Path 参数：`conversation_id`（strip 后为空 → 422）。
- 成功：`200`，`ConversationHistoryResponse`（字段见附录 A.5）：
  `{conversation_id, system_prompt, created_at, updated_at, messages[]}`；
  `messages[]` 元素 `{sequence, role, content, created_at}`，按原始
  `seq ASC` 返回；`role ∈ {user, assistant}`（assistant 一律是无
  `tool_calls` 且非空内容的最终回答）；空会话返回空数组。
- 错误：`404 {"detail": "conversation not found error"}`（会话不存在、
  跨用户、跨企业统一 404，不泄露归属）；`401/400` 同前；`422`（空 id）。
- 安全过滤实现：`ChatService._to_visible_message`（应用层过滤）+ Store
  按归属读取；**用于模型上下文的原始消息存储与 `load_messages` 不变**。
- 源码：`app/api/router.py`（`get_conversation_messages`）、
  `app/application/chat_service.py`（`get_history`）、
  `app/sessions/sqlite_store.py`（`get_conversation_record` /
  `load_message_records`）、`app/schemas/chat.py`。
- 测试：`tests/api/test_router.py`（含 OpenAPI schema 与真实响应一致）、
  `tests/application/test_chat_service.py`（过滤与顺序）、
  `tests/sessions/test_sqlite_store.py`（记录读取/归属）、
  `tests/test_main.py`（真实 app 隔离）。
- 建议页面：进入会话时加载历史；发送超时/失败后先刷新本接口确认服务端
  实际状态再决定是否重发；刷新页面后以本接口为准恢复（禁止 localStorage
  伪造完整历史）。

#### 4.3.5 PUT `/conversations/{conversation_id}/system-prompt/` — 更新系统提示词（200）

- 用途：覆盖会话级附加提示词（服务端会拼在基础系统提示之后，且注明
  “不能覆盖服务器规则”）。
- 鉴权：Bearer + X-Organization-ID。
- Path 参数：`conversation_id`（空白 → 422）。
- Content-Type：`application/json`；`UpdateSystemPromptRequest`（extra=forbid）：
  `system_prompt: string`（strip 后非空）。
- 成功：`200`，`SystemPromptUpdated {updated: true}`。
- 错误：`404`（会话不存在/非本人）；`422 {"detail": "prompt must be not blank"}`
  等字符串 detail；`401/400` 同前。
- 源码：`app/api/router.py`、`app/application/chat_service.py`。
- 测试：`tests/api/test_router.py`（`test_update_system_prompt_is_conversation_
  scoped`）。
- 建议页面：会话设置面板。当前 system_prompt 可通过 4.3.4 历史接口的
  `system_prompt` 字段回读（最近一次 PUT 成功后的值以本地最近保存值为准，
  二者一致）；新会话的提示词可在创建时传入。

### 4.4 知识库 `/knowledge`（7 个）

> 错误体统一 `{"code": "...", "message": "..."}`（除 403 外），code→状态码
> 映射见附录 B.1。403 为 `{"detail": "admin role required"}` 字符串形态。
> 写接口（上传/新版本/停用/启用）仅 admin；读接口（列表/详情/任务）成员均可。

#### 4.4.1 POST `/knowledge/documents/` — 上传文档（201，admin，同步入库）

- 用途：上传新文档并**同步**完成“加载→分块→embedding→写入向量库→激活”，
  成功返回时文档已 ACTIVE（happy path）。注意同步意味着该请求在服务端做完
  全部入库工作后才返回，耗时可能较长（见 5.1）。
- 鉴权：Bearer + X-Organization-ID；**仅 admin**（agent → 403）。
- Content-Type：`multipart/form-data`，字段**必须恰好**：
  - `title: string`——文档标题（服务端 strip 后 1–200 字符）；
  - `file: 文件`——扩展名仅接受 `.md` / `.markdown` / `.txt`
    （`.docx` 即使领域层支持 Word，**路由层当前拒绝**）；≤ 2 MiB
    （`MAX_DOCUMENT_BYTES = 2*1024*1024`）；须为合法 UTF-8 且非空。
  - 多传/漏传任何其它字段（例如想塞 `organization_id`）→ 422。
- 成功：`201`，`IngestionReceiptResponse`：
  `{document_id, version_id, job_id, status, deduplicated}`；
  `status ∈ queued/running/succeeded/failed`（当前同步管线成功时通常直接
  succeeded）；`deduplicated: true` 表示内容与已激活版本完全相同（幂等重传，
  没有产生新版本）。
- 错误：
  - `403 {"detail": "admin role required"}`；
  - `422` `INVALID_DOCUMENT`（unsupported file type; use .md/.markdown or
    .txt / document exceeds maximum size / title 超长或为空 / multipart 字段
    不合法 / 非 UTF-8 / 空文件等，message 各异）；
  - `503` 默认（embedding/向量库/管线等基础设施失败，code 为具体稳定码）。
- 源码：`app/api/knowledge_router.py`（`upload_document`）、
  `app/knowledge/ingestion.py`、`app/knowledge/document_loader.py`、
  `app/schemas/knowledge.py`。
- 测试：`tests/api/test_knowledge_router.py`（`test_admin_upload_uses_tenant_
  context`、`test_agent_cannot_upload`、`test_upload_response_shape_and_type_
  mapping`、`test_extensions_and_empty_body_mapped_to_invalid_document`、
  `test_file_over_max_bytes_is_invalid_document`、`test_extra_organization_id_
  field_is_rejected`）。
- 建议页面：知识库列表页/详情页“上传文档”对话框。文件选择器限定上述扩展名
  与 2 MiB；上传期间给不可取消/长等待提示；失败后提供“查看任务错误”入口。

#### 4.4.2 POST `/knowledge/documents/{document_id}/versions/` — 上传新版本（201，admin）

- 用途：为既有文档上传新版本；新版本成功后自动成为 active 版本
  （版本号自增，历史版本只增不改）。
- 鉴权与表单规则：同 4.4.1（admin；title+file 恰好两字段；类型/大小/编码
  校验相同）。**文件扩展名类型必须与文档既有 `source_type` 一致**
  （如原文档是 markdown 就不能传 .txt），否则 422。
- Path 参数：`document_id`。
- 成功：`201`，`IngestionReceiptResponse`；内容与某已激活版本完全相同时返回
  `deduplicated: true`（重复版本本身会触发 409 的唯一约束，见下）。
- 错误：`403`；`404 DOCUMENT_NOT_FOUND`；`409 DUPLICATE_DOCUMENT_VERSION`
  （同内容版本已存在且该版本从未成功激活）；`422 INVALID_DOCUMENT`（类型
  不一致等）；`503` 基础设施失败。
- 源码：`app/api/knowledge_router.py`（`upload_document_version`）、
  `app/knowledge/ingestion.py`（`ingest_new_version`）。
- 测试：`tests/api/test_knowledge_router.py`（`test_admin_uploads_new_version`、
  `test_agent_cannot_upload_new_version`）。
- 建议页面：文档详情页“上传新版本”。

#### 4.4.3 GET `/knowledge/documents/` — 文档列表（200，成员）

- 用途：当前企业全部文档摘要列表；按 `created_at, id` **升序**（老文档在前，
  前端如需最新在前自行倒序）。
- 鉴权：Bearer + X-Organization-ID；成员均可（无角色校验）。
- 成功：`200`，`KnowledgeDocumentSummaryResponse[]`：
  `{document_id, title, source_type, status, active_version_id, created_at,
  updated_at}`；`source_type ∈ markdown/text/word`；`status ∈ processing/
  active/disabled/failed`。
- 错误：`401/400/404`（租户依赖三件套，见 2.1/2.2）。
- 源码：`app/api/knowledge_router.py`（`list_documents`）、
  `app/knowledge/sqlite_store.py`。
- 测试：`tests/api/test_knowledge_router.py`（`test_list_documents_is_tenant_
  scoped`）。
- 建议页面：知识库列表页。**无分页/无搜索参数**（缺口见 5.4），量大时前端
  自行过滤或待后端加分页。

#### 4.4.4 GET `/knowledge/documents/{document_id}/` — 文档详情（200，成员）

- 用途：文档详情 + **全部版本列表**（version_no 升序）+ active 版本最近一次
  入库任务（`latest_job`，可 null）。
- 鉴权：Bearer + X-Organization-ID。
- Path 参数：`document_id`。
- 成功：`200`，`KnowledgeDocumentDetailResponse`（见附录 A.10）。
  注意：**响应不含文档正文/原文**（`DocumentVersionResponse` 无 raw_text），
  “查看文档内容”当前无法通过 API 实现。
- 错误：`404 {"code": "DOCUMENT_NOT_FOUND", "message": "..."}`（跨企业文档与
  不存在文档同响应，不泄露标题）。
- 源码：`app/api/knowledge_router.py`（`get_document_detail`）、
  `app/knowledge/sqlite_store.py`。
- 测试：`tests/api/test_knowledge_router.py`（`test_document_detail_returns_
  versions_and_latest_job`、`test_detail_cross_tenant_is_404_without_title_leak`）。
- 建议页面：知识库详情页（版本历史时间线、任务状态、停用/启用/新版本入口）。

#### 4.4.5 POST `/knowledge/documents/{document_id}/disable/` — 停用（200，admin）

- 用途：停用文档（不再参与检索；版本与任务保留）。
- 鉴权：Bearer + X-Organization-ID；**仅 admin**。
- 成功：`200`，`KnowledgeDocumentSummaryResponse`（status=disabled）。
- 错误：`403`；`404 DOCUMENT_NOT_FOUND`；
  `409 {"code": "DOCUMENT_DISABLED", ...}`——该分支是 HTTP 层为「重复停用
  冲突」预留的映射，但**当前生产 SQLite Store 对重复停用是幂等覆盖（仍返回
  200）**，409 只在 Store 抛出该错误时出现（测试用 Fake 覆盖该路径），前端
  把「已停用」按钮直接置灰即可。
- 源码：`app/api/knowledge_router.py`（`disable_document`）。
- 测试：`tests/api/test_knowledge_router.py`（`test_disable_and_enable_require_
  admin_and_return_200`、`test_disable_requires_admin`）。
- 建议页面：列表/详情停用按钮（需确认弹窗）。

#### 4.4.6 POST `/knowledge/documents/{document_id}/enable/` — 启用（200，admin）

- 用途：把停用文档重新启用为可检索。
- 鉴权：Bearer + X-Organization-ID；**仅 admin**。
- 成功：`200`，`KnowledgeDocumentSummaryResponse`（status=active；
  生产 SQLite Store 直接置 active，对 processing/failed 等其它状态文档
  enable 的深层语义后端未额外校验，前端按场景提示即可）。
- 错误：`403`；`404 DOCUMENT_NOT_FOUND`（启用不存在的文档；启用一个从未
  成功入库的文档不会触发额外错误，展示上仍按接口返回的 status 呈现）。
- 源码：`app/api/knowledge_router.py`（`enable_document`）。
- 测试：同 4.4.5 的两个测试。
- 建议页面：同 4.4.5 的启用按钮。

#### 4.4.7 GET `/knowledge/ingestion-jobs/{job_id}/` — 入库任务查询（200，成员）

- 用途：查询一次入库任务的执行状态、重试次数与稳定错误码/消息
  （上传失败或想确认异步结果时使用）。
- 鉴权：Bearer + X-Organization-ID（任务按企业隔离，跨企业一律 404）。
- Path 参数：`job_id`。
- 成功：`200`，`IngestionJobResponse`（字段见附录 A.9）。
- 错误：`404 {"code": "INGESTION_JOB_NOT_FOUND", "message": "ingestion job
  not found"}`。
- 源码：`app/api/knowledge_router.py`（`get_ingestion_job`）。
- 测试：`tests/api/test_knowledge_router.py`（`test_list_jobs_returns_current_
  org_job`、`test_list_jobs_cross_tenant_is_404`）。
- 建议页面：上传结果提示/任务错误详情抽屉。

### 4.5 退款/补偿审批与 Action Run（5 个）

> 域错误统一 `{"code": "...", "message": "..."}`；跨企业资源一律伪装为
> 404。错误码→状态码总表见附录 B.2。
> 读接口（列表/详情/Run 状态）agent 与 admin 均可；写接口（决定/恢复）
> **仅 admin**，且每次调用都实时重读 Membership。

#### 4.5.1 GET `/approvals/` — 审批列表（200，成员）

- 用途：分页查询**当前企业**全部审批（**不按用户过滤**，agent 也能看到
  企业里其他人提的审批），带 Run 状态与决定摘要。
- 鉴权：Bearer + X-Organization-ID。
- Query 参数：

| 名称 | 类型 | 必填 | 约束 |
| --- | --- | --- | --- |
| `status` | enum/null | 否 | `pending` \| `approved` \| `approved_with_changes` \| `rejected`；非法值 422 |
| `limit` | int | 否 | 1–200，默认 20 |
| `offset` | int | 否 | ≥0，默认 0 |

- 排序：`created_at DESC, rowid DESC`（新审批在前）。
- 成功：`200`，`ApprovalListItemResponse[]`（字段见附录 A.12）：每项含
  `approval_id / proposal_id / order_id / action_type / approval_status /
  run_status / current_version / version_count / decision / created_at`。
  `current_version`/`decision` 可为 null；`order_id` 是企业内订单标识，与
  订单对外编号/详情页展示的对应关系属数据层口径（待确认），前端原样透出
  即可。
- 错误：`401/400/404` 租户三件套；`422` 校验数组（Query 越界/枚举非法）。
- 源码：`app/api/action_router.py`（`list_approvals`）、
  `app/actions/service.py`、`app/actions/sqlite_store.py`、
  `app/schemas/action.py`。
- 测试：`tests/api/test_action_router.py`（`test_agent_can_list_and_read_
  approval_detail`、`test_list_status_filter_and_pagination`）。
- 建议页面：审批列表页（待办=status=pending Tab / 全部 Tab；加载更多分页）。

#### 4.5.2 GET `/approvals/{approval_id}/` — 审批详情（200，成员）

- 用途：单个审批的完整上下文：Run 视图、请求版本、全部历史版本（升序）、
  当前版本、已落库决定与自审标记。
- 鉴权：Bearer + X-Organization-ID。
- Path 参数：`approval_id`（空白 → 422 `{"code": "APPROVAL_NOT_FOUND",
  "message": "审批 标识不能为空"}`）。
- 成功：`200`，`ApprovalDetailResponse`（附录 A.13）：
  - 顶层：`approval_id/proposal_id/order_id/action_type/approval_status/
    run/requested_version/current_version/versions/decision/
    self_approved/created_at`；
  - `self_approved` 只在决定作出后有意义（提案人=决定人，MVP 允许自审，
    前端可展示“自审”标记）。
- 错误：`404 APPROVAL_NOT_FOUND`（跨企业/不存在同码）；`422`（空白 id）。
- 源码：`app/api/action_router.py`、`app/actions/service.py`
  （`get_approval_detail`）。
- 测试：`tests/api/test_action_router.py`（`test_agent_can_list_and_read_
  approval_detail`、`test_acceptance_scenario_f_cross_tenant_access_returns_404`）。
- 建议页面：审批详情页：金额/原因卡片（requested_version）、历史版本折叠表
  （versions）、Run 状态条、决定表单（admin）、决定摘要（decision）。

#### 4.5.3 POST `/approvals/{approval_id}/decisions/` — 作出审批决定（201/202/200，仅 admin）

- 用途：admin 批准 / 修改后批准 / 拒绝提案；决定落库后服务端**自动尝试恢复
  工作流**，恢复失败则要求前端引导“显式恢复”。
- 鉴权：Bearer + X-Organization-ID；**仅 admin**（服务端实时重读角色）。
- Path 参数：`approval_id`（空白 → 422，同 4.5.2）。
- Content-Type：`application/json`；`DecisionRequest`（extra=forbid）：

| 字段 | 类型 | 必填 | 约束 |
| --- | --- | --- | --- |
| `decision` | enum | ✅ | `approved` \| `approved_with_changes` \| `rejected` |
| `changes` | 对象/null | 条件 | 仅 `approved_with_changes` 时必填；`approved`/`rejected` 时必须为 null（否则 422 `APPROVAL_INVALID_CHANGES`） |
| `comment` | string/null | 否 | strip 后 ≤1000 字符 |

`changes`（`DecisionChanges`，extra=forbid）字段（未填的沿用请求版本）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `amount_cents` | int/null | 修改后金额（**分**），严格正整数（拒绝字符串强转，`strict=True`） |
| `reason_code` | string/null | 修改后原因码，必须属于动作类型对应固定枚举（退款 11 个/补偿 4 个，见附录 B.3） |
| `reason_text` | string/null | 1–2000 字符 |
| `refund_scope` | enum/null | `full` \| `partial`；**仅退款提案**允许，补偿提案带此字段 → 422 |

- 成功状态码语义（三个都返回 `DecisionResponse`，前端必须区分）：
  - `201`：首次决定且自动恢复成功；
  - `202`：决定已保存但工作流恢复失败（`resume_required: true` +
    `resume_error_code`）→ 应提示“决定已生效，执行未完成，可稍后恢复”；
  - `200`：相同决定重复提交（幂等重放，返回首次结果，前端可忽略重复点击）。
- 成功响应 `DecisionResponse`：`{decision_id, decision, approval_id,
  proposal_id, run_id, run_status, decided_version_id, decided_by_user_id,
  comment, self_approved, resume_required, resume_error_code, created_at}`。
- 错误：
  - `403 APPROVAL_ADMIN_REQUIRED`（agent 决定）；
  - `404 APPROVAL_NOT_FOUND`；
  - `409 APPROVAL_ALREADY_DECIDED`（已决定且新决定不同）→ 前端刷新列表提示
    “该审批已被处理”；
  - `422 APPROVAL_INVALID_CHANGES`（批准/拒绝带 changes、修改字段非法、
    补偿带 refund_scope、金额非正整数、未实际改变任何字段等）；
  - `422` 校验数组（枚举/长度/多字段）。
- 源码：`app/api/action_router.py`（`decide_approval`，动态设置 201/202/200）、
  `app/actions/service.py`（`decide_approval`）、`app/schemas/action.py`。
- 测试：`tests/api/test_action_router.py`（`test_admin_approval_returns_201_
  and_records_self_approved`、`test_decision_saved_but_resume_failed_returns_
  202_and_retryable`、`test_repeat_same_decision_returns_200_with_first_result`、
  `test_different_decision_after_decided_returns_409`、`test_approved_with_
  changes_creates_new_version`、`test_approved_or_rejected_with_changes_returns_
  422`、`test_agent_cannot_decide_or_resume_returns_403`、并发测试 `test_
  concurrent_*`）。
- 建议页面：审批详情页决定表单（三选一：批准/修改后批准[展开金额、原因码、
  说明、退款范围编辑]/拒绝 + 备注）。提交后按状态码分流提示；202 时详情页
  常驻“恢复 Run”入口。

#### 4.5.4 GET `/action-runs/{run_id}/` — Run 状态查询（200，成员）

- 用途：查询一次 Action Run 的完整快照：Run 视图 + 提案 + 审批 + 决定 +
  当前版本 + 执行记录 + 解析后的业务结果。聊天响应里的 `PendingApproval.run_id`
  与审批详情里的 `run.run_id` 都能用来调用本接口。
- 鉴权：Bearer + X-Organization-ID（成员可读）。
- Path 参数：`run_id`（空白 → 422 `RUN_NOT_FOUND`“Run 标识不能为空”）。
- 成功：`200`，`RunStatusResponse`（附录 A.15）：
  `{run, proposal, approval, decision, current_version, execution, result}`；
  `result` 为执行成功后的解析结果 `{business_record_id: string,
  order_marked_refunded: bool}`（其它内部字段不承诺，以这两个键为准）。
- 错误：`404 RUN_NOT_FOUND`（跨企业/不存在同码）。
- 源码：`app/api/action_router.py`、`app/actions/service.py`（`get_run_status`）。
- 测试：`tests/api/test_action_router.py`（`test_run_status_query_returns_view_
  without_internal_ids`）。
- 建议页面：审批详情里的“Run 状态”面板；聊天 pending_approval 卡片的
  “查看状态”。

#### 4.5.5 POST `/action-runs/{run_id}/resume/` — 显式恢复 Run（200，仅 admin）

- 用途：管理员手动恢复：决定已保存但自动恢复失败（202）、或执行遇到可重试
  失败（`run.last_error_retryable`）。**恢复本身幂等**：已成功 Run 再恢复会
  返回现有稳定结果（200）；恢复调用本身失败返回 503 且业务事实不变，可重试。
- 鉴权：Bearer + X-Organization-ID；**仅 admin**。
- Path 参数：`run_id`（空白 → 422，同 4.5.4）。
- 成功：`200`，`ResumeResponse`：`{run: RunView, resume_ok: true,
  error_code: null, result: {...}|null}`。
- 错误：
  - `403 APPROVAL_ADMIN_REQUIRED`（agent）；
  - `404 RUN_NOT_FOUND`；
  - `409 RUN_NOT_RESUMABLE`（已终态不可恢复 / 未批准的执行分支不允许恢复）；
  - `503`：`resume_ok=false` 时路由直接抛 503，detail `{"code": "<稳定码>",
    "message": "工作流恢复失败，决定仍然有效，可稍后重试"}`；
  - `422`（空白 run_id）。
- 源码：`app/api/action_router.py`（`resume_run`）、`app/actions/service.py`
  （`resume_run`、`_require_resumable`）。
- 测试：`tests/api/test_action_router.py`（`test_resume_missing_run_returns_404`、
  `test_real_runner_approval_executes_and_repeat_resume_returns_result`、
  `test_real_runner_rejected_run_resume_returns_409`）。
- 建议页面：Run 状态面板/审批详情页“恢复执行”按钮（仅 admin 可见）；
  点击后刷新详情，503 时保留按钮允许重试。

---

## 5. 专项说明

### 5.1 文件上传方式

- 接口：`POST /knowledge/documents/`、`POST /knowledge/documents/{id}/versions/`。
- 请求头自动为 `Content-Type: multipart/form-data`，两个字段 `title`（表单
  字符串）与 `file`（文件），**不允许任何其它字段**（含客户端想带的
  organization_id——服务端明确 422 拒绝）。
- 约束（前端选文件前就应校验，服务端仍会兜底校验）：
  - 扩展名仅 `.md` / `.markdown` / `.txt`；注意领域枚举里的 `word` 目前
    HTTP 路由不接受（`DocumentSourceType.WORD` 定义了但上传被拒）；
  - 文件 ≤ 2 MiB（`2 * 1024 * 1024` 字节，服务端按字节截断读校验）；
  - 必须是合法 UTF-8、非空文本；
  - 上传新版本时类型必须与文档原 `source_type` 一致。
- 服务端**同步完成全部入库**后才返回（embedding + Qdrant 写入），因此响应
  可能很慢；上传过程中任务状态先以本地 UI 为准，返回后可用
  `GET /knowledge/ingestion-jobs/{job_id}/` 复核失败原因。
- `deduplicated=true` 表示服务端判定“内容与已激活版本完全相同”并直接复用
  （不产生新版本、不重复入库）——前端可提示“内容未变化”。

### 5.2 金额、时间、枚举状态的前端展示建议

**金额**

- 所有金额字段为 `amount_cents: int`（**分**），响应里**不存在小数金额字段**。
- 前端展示：用整数运算换算（如 `cents/100`、`cents%100` 补齐两位小数），
  避免浮点误差；金额输入控件建议以“分”提交、按整分校验。
- 币种字段 `currency`：等于订单币种，样例数据为 `CNY`（demo_data 与测试
  均为 CNY；其余币种值以订单数据为准，前端不要硬编码唯一币种）。
- 展示约定建议：`¥1,234.56` 风格；审批详情里金额与 `reason_code/reason_text`
  组合展示。

**时间（重要：三种文本格式并存，必须统一解析）**

| 来源模块 | 实际格式 | 示例 | 时区语义 |
| --- | --- | --- | --- |
| users / sessions（注册时间、会话创建/更新时间、历史消息时间） | `%Y-%m-%d %H:%M:%S`（无时区后缀） | `2026-09-06 12:34:56` | UTC（代码 `datetime.now(timezone.utc)`） |
| organizations / knowledge / tickets | `isoformat()`（含 `+00:00`） | `2026-09-06T12:34:56.789012+00:00` | UTC |
| actions（Run/提案/审批/决定/执行） | `%Y-%m-%dT%H:%M:%SZ` | `2026-09-06T12:34:56Z` | UTC |
| `AgentEvent.timestamp` | Pydantic datetime ISO | `2026-09-06T20:34:56.123456`（**无时区后缀**） | **服务器本地时间**（`datetime.now()`，语义不一致，见 2.5） |

- 建议：写一个统一的 `parseUtc(text)`（先识别有无 `Z`/`+hh:mm` 后缀再决定
  是否按 UTC 解释，事件时间除外），统一转本地时间展示；“几分钟前/昨天”
  等相对时间可选。

**枚举状态**（全部是字符串枚举，直接比较字符串即可；完整取值与含义见附录 B）

- 建议建立「枚举值 → 中文标签 + 颜色/图标」映射表集中管理（角色、文档状态、
  入库状态、Run 状态、审批状态、执行状态、退款范围、原因码等）。
- 不确定的枚举值不要假定：后端枚举只增不改，前端映射未命中时显示原始值
  兜底。

### 5.3 前端必须读取结构化字段、禁止从自然语言解析的数据

- **待审批提案**：聊天响应 `LLMResponse.pending_approvals[]` 是唯一可信来源
  （`run_id / proposal_id / approval_id / action_type / amount_cents /
  currency / status / resume_required / error_code`）。**严禁**从
  `llm_answer` 文本里抓取审批 id、金额或状态——模型文本可能错误或过时。
  schema 注释明确写着这一点（`app/schemas/chat.py` PendingApproval）。
- **知识引用**：`citations[]`（`citation_id` 即模型回答中引用的 C1..Cn 标签）
  与 `retrieval_summary.evidence_status`、`answer_incomplete`。回答中的
  “[C1]”样式角标应以结构化数组为准，不要在文本里正则提取正文。
- **工具过程**：`events[]`（type/tool_call_name/参数/结果/耗时）用于步骤
  展示与失败定位，不要在答案文本里解析。
- **决定与恢复结果**：`DecisionResponse.resume_required`、
  `ResumeResponse.resume_ok`、`RunStatusResponse.run.status /
  run.last_error_retryable / result`——用布尔/枚举驱动 UI 分支，不猜文案。
- 聊天接口**只返回本轮结果**；会话列表（4.3.2）与安全历史（4.3.4）是
  刷新后恢复对话的**唯一可信来源**——聊天响应里的 citations/events/
  pending_approvals 是“本页新收到”的展示数据，**历史接口不会保存它们**，
  刷新后只恢复安全的问答文本（前端不得伪造历史引用或审批卡片）。

### 5.4 当前后端缺少、但前端可能需要（或会受影响的）接口

> 均为“现状缺失”，不在本阶段修改后端；列出来供后续排期决策。

1. **会话删除 / 重命名 / 归档**：目前会话只能创建、读取与聊天，无删除或
   重命名接口（标题由首条消息自动推导，见 4.3）；会话列表接口本身已实现
   （4.3.2 GET /conversations/）。
2. **成员管理**：只有「添加成员」（4.2.3）；**没有成员列表、移除成员、
   改角色、查看企业成员数**接口。
3. **企业设置**：无修改企业名、退出企业、转让/删除企业接口。
4. **退出登录/吊销 Token**：无服务端登出；Token 只有自然过期。前端“退出登录”
   只能是清本地 Token（可接受，因为 JWT 无状态）；若要求“踢下线”需后端
   加吊销机制。
5. **个人信息**：只有 /auth/me 的只读展示；无改密码、改用户名接口。
6. **“我的提案/我发起的审批”过滤**：审批列表只按企业+status+分页，不按
   `requested_by_user_id` 过滤；前端只能拿到全企业列表后本地过滤。
7. **用户→用户名展示**：响应里的 `*_by_user_id` 都是 user_id，没有按 id
   查用户名的接口（/auth/me 只返回自己），审批/提案人只能显示 id。
8. **Run 列表接口**：没有 `/action-runs` 列表；Run 只能从聊天
   `pending_approvals`、审批详情（`run.run_id`）进入查询。审批详情与决定
   响应都带 run_id，够用但入口有限。
9. **知识库**：无文档删除（只能 disable）、无正文预览（detail 不含
   raw_text）、无列表分页/搜索、无文档下载；“立即重新入库”只能靠重传同
   内容文件。
10. **待办角标/汇总**：无“待我审批数量”汇总接口（可本地用
    `GET /approvals/?status=pending&limit=1` 近似，但拿不到总数）。
11. **角色/成员异动通知**：无推送；只能靠用户手动刷新或进入页面时重新拉取。

### 5.5 前后端联调与缺口（现状 → 建议，未修改代码）

- **CORS：后端完全未配置**（全仓库无 `CORSMiddleware`）。本地开发两种
  方案（二选一，需要你拍板，暂不改动）：
  - 推荐：前端 dev server 配代理（如 Vite `/api` → `http://127.0.0.1:8000`，
    同源请求彻底绕开 CORS）；或
  - 后端加 CORS 白名单（若最终前端与后端分离部署，这一步迟早要做）。
- **聊天接口形态**：普通同步 POST（非流式、非 SSE、非 WebSocket），单轮可能
  耗时数十秒以上。前端可接受“等待转圈”，但建议后续后端提供
  流式/事件接口（runner.py 已预留 SSE/WebSocket 监听扩展点注释）以改善
  体验；同时建议后端给 ChatRequest 增加请求级超时/幂等键，避免用户重试
  造成重复消息（当前服务端在锁内执行、消息按轮 append，客户端取消后服务端
  是否仍写入存在不确定性——待确认项）。前端当前已按此约束落地：聊天请求
  使用独立长超时（`VITE_CHAT_TIMEOUT_MS`，默认 180 秒）、**不自动重试**；
  超时/断网后提示「结果状态可能不确定」，引导用户调用 4.3.4 刷新历史确认
  后再决定是否重发。
- **需要轮询的状态**：
  - Run 恢复/执行：`POST resume` 返回 200 后建议再拉一次 `GET /action-runs/
    {id}/` 或审批详情刷新终态；503/可重试失败场景，UI 保留按钮由用户手动
    重试即可，不强制自动轮询；
  - 入库任务：同步上传接口本身等结果；若显示失败/不确定，可用
    `GET /knowledge/ingestion-jobs/{job_id}/` 复核（现阶段任务基本在请求内
    完成，轮询不是主路径，README 也说明 Worker/定时任务尚未实现）；
  - 无 WebSocket/SSE 推送，任何“别人改了状态”都需要刷新或页面轮询
    （如审批待办，建议 10–30s 低频轮询或下拉刷新）。
- **Token 过期处理**：任何请求 401（登录接口除外）→ 清本地登录态 → 跳登录
  页并提示“登录已过期”；无 refresh 机制。可用 `expires_in` 做本地倒计时
  提前提示。
- **角色权限是“前端隐藏 + 后端强制”双保险**：写操作（成员添加、知识库写、
  决定、恢复）后端全部强校验 admin（agent 得 403；非成员得 404），所以前端
  按 `GET /organizations/` 返回的 role 隐藏按钮**只是体验优化，不是安全
  边界**；后端已兜底，不担心绕过。

---

## 6. 附录 A：请求/响应模型字段字典

> 类型列中 enum 的取值见附录 B。时间格式见 5.2。所有响应均无密码/哈希/
> Token 类字段。

### A.1 RegisterRequest / LoginRequest（请求）

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| username | string | ✅ | 3–32；归一化后须匹配 `^[a-z0-9_]{3,32}$` |
| password | string | ✅ | 8–72 UTF-8 字节 |

### A.2 UserResponse

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| user_id | string | 用户唯一标识（uuid4 字符串） |
| username | string | 归一化后的用户名 |
| created_at | string | 注册时间（UTC `%Y-%m-%d %H:%M:%S`） |

### A.3 TokenResponse

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| access_token | string | Bearer Token（JWT） |
| token_type | string | 固定 `"bearer"` |
| expires_in | int | 有效秒数（部署 TTL，代码默认 1800，env 样例 604800） |

### A.4 OrganizationResponse / OrganizationAccessResponse / MembershipResponse

- `OrganizationResponse`：`organization_id, name`
- `OrganizationAccessResponse`：`organization_id, name, role`（我在该企业角色）
- `MembershipResponse`：`organization_id, user_id, role`

### A.5 会话模块请求/响应模型

- `ConversationCreated`：`conversation_id`
- `SystemPromptUpdated`：`updated: true`
- `CreateConversationRequest`：`system_prompt: string|null`（可选）
- `UpdateSystemPromptRequest`：`system_prompt: string`（strip 后非空）
- `ConversationListItem`（GET /conversations/ 列表元素）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| conversation_id | string | 会话唯一标识 |
| title | string | 由首条用户消息推导（空白折叠、截断 30 字符）；空会话为「新会话」 |
| created_at | string | 创建时间（UTC 文本 `%Y-%m-%d %H:%M:%S`） |
| updated_at | string | 最近活动时间（UTC 文本，倒序排序依据） |

- `ConversationHistoryResponse`（GET /conversations/{id}/messages/）：
  `conversation_id, system_prompt: string|null, created_at, updated_at,
  messages: ConversationHistoryMessage[]`
- `ConversationHistoryMessage`：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| sequence | int | 消息在会话中的原始序号（seq），按升序返回 |
| role | enum | `user` / `assistant`（仅这两种；assistant 为无工具调用的最终回答） |
| content | string | 消息正文（原文保留，保证非空；不含内部字段） |
| created_at | string | 消息写入时间（UTC 文本） |

### A.6 LLMResponse（聊天成功响应）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| llm_answer | string/null | 最终回答文本 |
| llm_reasoning_content | string/null | 模型思考过程（可折叠） |
| events | AgentEvent[] | 工具调用事件列表 |
| citations | Citation[] | 结构化知识引用 C1..Cn |
| retrieval_summary | RetrievalSummary/null | 检索摘要 |
| answer_incomplete | bool | 证据不足/漏引时服务端确定性置 true |
| pending_approvals | PendingApproval[] | 本轮提出的退款/补偿待审批提案 |

### A.7 AgentEvent

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| type | enum | `tool_call.requested` \| `tool_call.started` \| `tool_call.completed` \| `tool_call.failed` \| `citation.invalid` |
| timestamp | datetime(ISO) | 服务器本地时间（见 5.2 警示） |
| tool_call_id | string | 工具调用 id |
| tool_call_name | string | 工具名（如 search_knowledge / propose_refund） |
| tool_call_arguments | dict | 工具参数 |
| result | any/null | 结果（completed 时） |
| error | string/null | 错误（failed 时） |
| duration_ms | number/null | 耗时 |

### A.8 Citation / RetrievalSummary / PendingApproval

- `Citation`：`citation_id`(C1..Cn 标签), `document_id`, `version_id`,
  `chunk_id`, `title`, `heading_path|null`, `content`(可信片段正文)
- `RetrievalSummary`：`strategy`(string), `round_count`(int),
  `evidence_status`("SUFFICIENT"/"INSUFFICIENT", **大写**), `latency_ms`(int)
- `PendingApproval`：`run_id`, `proposal_id`, `approval_id`, `action_type`
  (refund/compensation), `status`(等待审批时为 "awaiting_approval"),
  `amount_cents`(int 分), `currency`, `resume_required`(bool=false),
  `error_code|null`

### A.9 IngestionReceiptResponse / IngestionJobResponse / DocumentVersionResponse

- `IngestionReceiptResponse`：`document_id, version_id, job_id, status,
  deduplicated`
- `IngestionJobResponse`：`job_id, document_id, version_id, status
  (queued/running/succeeded/failed), attempt_count, error_code|null,
  error_message|null, started_at|null, finished_at|null, created_at`
- `DocumentVersionResponse`：`version_id, version_no, content_hash,
  loader_version, chunker_version, embedding_model, embedding_dimensions,
  created_at`（无正文）

### A.10 KnowledgeDocumentSummaryResponse / KnowledgeDocumentDetailResponse

- Summary：`document_id, title, source_type, status, active_version_id|null,
  created_at, updated_at`
- Detail = Summary 字段 + `versions: DocumentVersionResponse[]` +
  `latest_job: IngestionJobResponse|null`

### A.11 DecisionRequest / DecisionChanges

见 4.5.3 表；`changes` 仅在 `approved_with_changes` 时出现且必填。

### A.12 ApprovalListItemResponse

`approval_id, proposal_id, order_id, action_type, approval_status,
run_status, current_version: VersionView|null, version_count: int,
decision: DecisionView|null, created_at`

### A.13 ApprovalDetailResponse

`approval_id, proposal_id, order_id, action_type, approval_status,
run: RunView, requested_version: VersionView, current_version|null,
versions: VersionView[], decision|null, self_approved: bool, created_at`

### A.14 RunView / ProposalView / ApprovalView / DecisionView / VersionView / ExecutionView

- `RunView`：`run_id, workflow_type(refund/compensation), status, created_by_
  user_id, last_error_code|null, last_error_retryable, created_at, updated_at,
  completed_at|null`（不含 thread_id 等内部标识）
- `ProposalView`：`proposal_id, order_id, action_type, status, created_by_
  user_id, created_at, updated_at`
- `ApprovalView`：`approval_id, proposal_id, requested_version_id, requested_
  by_user_id, status, created_at, decided_at|null`
- `DecisionView`：`decision_id, decision, decided_version_id, decided_by_user_
  id, comment|null, created_at`（不可变）
- `VersionView`：`version_id, version_no, amount_cents(分), currency,
  reason_code, reason_text, refund_scope|null(full/partial),
  coupon_valid_days|null(补偿固定 30), created_by_user_id, created_at`
- `ExecutionView`：`execution_id, proposal_id, proposal_version_id,
  action_type, status(claimed/running/succeeded/failed_retryable/
  failed_terminal), attempt_count, error_code|null, error_retryable,
  claimed_at, updated_at, completed_at|null`

### A.15 DecisionResponse / RunStatusResponse / ResumeResponse

- `DecisionResponse`：见 4.5.3（含 `resume_required`、`resume_error_code|null`）
- `RunStatusResponse`：`run: RunView, proposal|null, approval|null, decision|
  null, current_version|null, execution|null, result: dict|null`
- `ResumeResponse`：`run: RunView, resume_ok: bool, error_code|null,
  result|null`

---

## 7. 附录 B：错误码与枚举总表

### B.1 知识库错误码（code → HTTP）

| code | HTTP | 含义 |
| --- | --- | --- |
| DOCUMENT_NOT_FOUND | 404 | 文档不存在或不属于当前企业（不泄露） |
| INVALID_DOCUMENT | 422 | 类型/大小/编码/标题/multipart 结构非法 |
| DOCUMENT_DISABLED | 409 | 重复停用等状态冲突 |
| DUPLICATE_DOCUMENT_VERSION | 409 | 同内容版本冲突 |
| INGESTION_JOB_NOT_FOUND | 404 | 任务不存在或跨企业 |
| 其它 KnowledgeError（INGESTION_FAILED / EMBEDDING_UNAVAILABLE / VECTOR_STORE_UNAVAILABLE / RERANK_UNAVAILABLE 等） | 503 | 入库基础设施失败（决定/事实不变，可重试） |

### B.2 Action 错误码（code → HTTP，依据 `actions/service.py`）

| code | HTTP | 典型场景 |
| --- | --- | --- |
| ACTION_ORDER_NOT_FOUND | 404 | 订单不存在/跨企业 |
| ACTION_INVALID_AMOUNT / ACTION_CURRENCY_MISMATCH / APPROVAL_INVALID_CHANGES | 422 | 输入非法（工具/API 通用） |
| ACTION_ACTIVE_PROPOSAL_EXISTS / ACTION_REFUND_BALANCE_EXCEEDED / ACTION_COMPENSATION_DUPLICATE / ACTION_COMPENSATION_CAP_EXCEEDED | 409 | 业务冲突（主要出现在 Agent 工具面） |
| APPROVAL_NOT_FOUND / PROPOSAL_NOT_FOUND / RUN_NOT_FOUND | 404 | 不存在或跨企业 |
| APPROVAL_ADMIN_REQUIRED | 403 | 非 admin 执行决定/恢复 |
| APPROVAL_ALREADY_DECIDED / RUN_NOT_RESUMABLE / RUN_STATE_CONFLICT / EXECUTION_NOT_APPROVED | 409 | 状态冲突/重复决定/不可恢复 |
| EXECUTION_RETRYABLE_FAILURE / CHECKPOINT_UNAVAILABLE | 503 | 基础设施失败，事实不变可重试 |
| EXECUTION_DATA_INTEGRITY_ERROR | 500 | 数据完整性损坏（不应发生） |
| ACTION_ERROR（其它） | 422 | 兜底输入错误 |

### B.3 前端展示用枚举值清单

| 枚举 | 取值 | 展示建议 |
| --- | --- | --- |
| MembershipRole | `admin` / `agent` | “管理员”/“客服” |
| DocumentSourceType | `markdown` / `text` / `word` | “Markdown”/“文本”/“Word”（word 当前上传不可用） |
| DocumentStatus | `processing` / `active` / `disabled` / `failed` | 处理中/生效/已停用/失败 |
| IngestionStatus | `queued` / `running` / `succeeded` / `failed` | 排队中/处理中/成功/失败 |
| ActionType | `refund` / `compensation` | 退款/优惠券补偿 |
| ActionRunStatus | `queued`/`running`/`awaiting_approval`/`succeeded`/`failed`/`cancelled` | 排队中/执行中/等待审批/成功/失败/已取消；`failed` 且 `last_error_retryable=true` 时可恢复 |
| ProposalStatus | `awaiting_approval`/`approved`/`executing`/`succeeded`/`rejected`/`failed`/`cancelled` | 等待审批/已批准/执行中/成功/已拒绝/失败/已取消 |
| ApprovalStatus | `pending`/`approved`/`approved_with_changes`/`rejected` | 待决定/已批准/修改后批准/已拒绝 |
| ApprovalDecisionType | `approved`/`approved_with_changes`/`rejected` | 同上一行 |
| RefundScope | `full`/`partial` | 全额退款/部分退款 |
| ToolExecutionStatus | `claimed`/`running`/`succeeded`/`failed_retryable`/`failed_terminal` | 已认领/执行中/成功/可重试失败/终态失败 |
| 退款原因码 RefundReasonCode | `customer_cancellation`/`changed_mind_return`/`quality_issue`/`damaged_item`/`wrong_item`/`missing_item`/`not_as_described`/`out_of_stock`/`delivery_delay`/`lost_in_transit`/`other` | 各含义见 `app/actions/base.py` 注释（客户取消/无理由退货/质量问题/破损/错发/漏发/描述不符/缺货/配送超时/丢件/其他） |
| 补偿原因码 CompensationReasonCode | `delayed_shipment`/`transit_delay`/`customer_dispute`/`other` | 发货延迟/运输延误/客户争议/其他 |
| EvidenceStatus（检索摘要） | `SUFFICIENT` / `INSUFFICIENT`（**大写**） | 证据充分/证据不足 |
| 金额字段 | `amount_cents: int` | 展示 ÷100 保留两位小数；样例币种 CNY |

---

## 8. 待确认事项清单（无法从现有代码/测试证实，勿臆测）

1. 除 demo 数据 CNY 外，订单可能出现的币种集合（取决于数据层订单内容）；
   审批列表里 `order_id` 与“对外订单号”的口径对应（见 4.5.1）。
2. 除 demo 数据 CNY 外，订单可能出现的币种集合（取决于数据层订单内容）。
3. 聊天请求被客户端中断（超时/关页）后，服务端是否会继续完成并追加消息、
   是否造成重复历史——需要与后端确认幂等策略（当前 `ChatRequest` 无幂等键）。
4. 部署环境的 `ACCESS_TOKEN_TTL_SECONDS` 与真实 `AUTH_SECRET_KEY`/模型/向量
   服务地址（决定登录态时长与本地联调前提，前端按 `expires_in` 与 /auth/me
   自适应即可）。
5. `AgentEvent.timestamp` 使用服务器本地时间是否为预期（建议统一 UTC，待确认）。
6. 成员管理、会话删除/重命名等缺口接口是否会补（见 5.4），影响页面规划范围。
