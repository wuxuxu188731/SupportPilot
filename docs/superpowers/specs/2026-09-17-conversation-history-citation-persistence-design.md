# 引用随回答持久化（刷新后仍可跳转）—— 改动文档

> 来源：`docs/superpowers/specs/2026-09-09-knowledge-document-content-viewer-design.md`
> §6「后续待完成」第 1 项「**引用在会话历史中持久化**（刷新后仍能跳转）」。
>
> 本文档只做**改动范围与方向的界定**，不含代码实现。
>
> **决策记录（2026-09-17，项目负责人）**：
>
> | # | 决策 | 结论 |
> | --- | --- | --- |
> | 0 | 存储形态 | **独立表** `message_citations`（3.2 方案 B）：一条引用一行、列名与字段一一对应，后续维护者读表即懂 |
> | 1 | 是否连 `retrieval_summary` 一起存 | **不存**：它是 Agentic Search 的产物，当前生产走传统 RAG（3.4），先不管 |
> | 2 | 写入接口形态 | **`append_messages(..., display=[...])` 显式参数**（4.2、9.2 详解） |
> | 3 | 引用片段正文 `content` 是否入库 | **入库**（先按"不入库"评估后改回）：行内自带证据快照，历史响应直接复用 `Citation`，前端组件零改动、刷新后卡片与实时完全一致；不入库会引入两套引用形状与一处于用户可见的行为差异，工作量与长期维护面都更大（9.1 决策 3 有对比） |
> | 4 | 引用锚点作用域修复 | **并入本次**（5.4） |
> | 5 | `(document_id, version_id)` 反查索引 | **暂不建**：本次只用会话读路径，将来做反查时再加 |

| 项 | 内容 |
| --- | --- |
| 状态 | 决策点已由项目负责人拍板（2026-09-17，见第 9.1 节），可按第 8 节拆分实施 |
| 目标 | 刷新页面 / 重新进入会话后，历史回答仍带**结构化引用**，可继续「查看原文位置」 |
| 涉及模块 | `migrations`、`app/sessions`、`app/application`、`app/schemas`、`frontend/src`（api / stores / components） |
| 数据库 | **新增 1 张表 `message_citations`**（一条引用一行）+ `messages` 新增一列 `answer_incomplete`；迁移 `0014` |
| 本次不做 | `events` / 待审批卡片 / 检索摘要的持久化；历史数据回填；引用写 localStorage |
| 前置能力 | 正文按版本读取、引用偏移高亮、历史版本与停用文档提示、偏移为空降级——**均已落地**（阶段 A/B/C 完成） |

## 0. 结论速览（先回答三个问题）

| 问题 | 结论 |
| --- | --- |
| 是否要新增数据库表？ | **要，新增 1 张表 `message_citations`**：一条引用一行，列名与 `Citation` 字段一一对应（含片段正文快照），另加 `conversation_id` / `seq` / `ordinal` / `created_at`。引用是结构化领域对象而非"一坨展示 JSON"，建表后**列即文档**，维护者读表结构就能懂。 |
| 是否需要迁移？ | **需要。** 迁移 `0014_message_citations`：`CREATE TABLE message_citations`（外键 + 唯一约束 + 读路径索引）**并且** `ALTER TABLE messages ADD COLUMN answer_incomplete INTEGER NULL`（回答级属性，不属于任何一条引用，见 3.2 末段）。升级前的历史回答没有任何引用行，读取时按「无引用」处理，**无需回填**。 |
| 改动方向一句话 | 回答生成时，在**消息落库的同一事务**里把该回答的引用（标识 + 定位字段 + 证据片段快照）逐条写入 `message_citations`；历史接口把它们作为新字段返回，前端在历史回答下渲染同一套引用卡片，刷新后的展示与实时完全一致。 |

预期改动规模：后端 **9 个文件**（含 1 个迁移与 4 个测试文件）、前端 **6 个文件**（含 3 个测试文件）、文档 **5 个文件**。完整清单见第 6 节。

---

## 1. 目标与非目标

### 1.1 目标

1. 一条带知识引用的回答，**刷新页面后引用卡片仍在**，卡片里的来源文档、标题路径、
   引用正文片段与实时看到的一致；
2. 历史引用上的「查看原文位置」入口**与实时引用行为完全相同**：带偏移则高亮定位，
   偏移为空则降级为「只打开来源文档」；
3. 刷新后仍能显示「当前回答的证据引用可能不完整，请谨慎采用」这类**安全提示**
   （实时响应里的 `answer_incomplete` 不能丢）；
4. 全过程不扩大数据暴露面：历史接口仍然只返回"该用户当时已经看到过"的内容。

### 1.2 非目标（本次明确不做，理由见第 11 节）

- `events`（处理过程时间线）、`pending_approvals`（待审批卡片）、`retrieval_summary`
  （检索摘要行）的持久化；
- 历史引用"重新对齐到最新版本"；
- 历史数据的引用回填（数据当时就没存，无法回填）；
- 前端用 localStorage 缓存引用（服务端才是唯一可信来源）。

---

## 2. 现状（已核实事实，实施时不得推翻）

### 2.1 引用目前只活在本轮 HTTP 响应里

| 事实 | 依据 |
| --- | --- |
| 引用来自由工具结果解析出的结构化 `citations`，并随 `POST /conversations/{id}/chat/` 一次性返回 | `app/agent/runner.py:227-248`（`validate_final_citations` → `LLMResponse.citations`） |
| 持久化的只有**模型上下文消息**：`chat()` 把本轮新增的消息批量写库 | `app/application/chat_service.py:224-229` |
| 历史接口只做「白名单过滤」，绝不返回结构化展示信息 | `app/application/chat_service.py:128-161`（`_to_visible_message`）、`app/schemas/chat.py:67-76` |
| 前端 store 明确写着「刷新后不会恢复结构化展示信息，不伪造」 | `frontend/src/stores/chat.ts:1-14`、`frontend/src/components/chat/MessageList.vue:181-184` |
| 手工验收把它登记为**已知限制** | `docs/frontend/manual-test-runbook.md:525-531`（E-09）、`:785-791`（F-14-9）、`:1067` |

### 2.2 `messages.payload_json` 是"模型上下文载荷"，不是展示数据

- `messages` 表：`id / conversation_id / seq / role / payload_json / created_at`
  （`migrations/versions/0001_baseline.py:59-87`），本轮只加一列且不涉及重建表。
- `load_messages()` 把 `payload_json` **原样**解析后交给 `ChatService.chat()`，
  再作为 `messages` 传给模型（`app/sessions/sqlite_store.py:262-284`、
  `app/application/chat_service.py:191-222`）。
- 因此**不能**把引用塞进 `payload_json`：那会让引用内容作为"助手自己说过的话"
  回到模型上下文里（污染上下文，且部分 OpenAI 兼容网关会拒绝未知字段）。详见 3.2 的否决理由。
- `load_message_records()` 是历史读路径，返回完整 `payload` 供应用层过滤
  （`app/sessions/sqlite_store.py:153-187`）——这是历史引用唯一需要接入的读路径
  （引用本身另有一条按 `conversation_id` 取行、按 `seq` 分组的查询，见 4.2）。
- `SQLiteSessionStore` 是 `SessionStore` 协议的唯一实现（`app/sessions/legacy_file.py`
  只是两个未被使用的自由函数），所以协议扩展没有第二处实现要跟。

### 2.3 安全边界现状（回归红线）

- 历史接口的过滤规则：只返回 `role = user` 与「无 `tool_calls` 且内容非空」的
  assistant 最终回答；`system` / `tool` / 中间助手消息 / `reasoning_content` /
  工具参数与结果一律不返回（`app/application/chat_service.py:128-161`）。
- 现有防漂移测试：OpenAPI 声明的字段集合必须与真实响应**完全一致**
  （`tests/api/test_router.py:273-292`），历史响应体还有整体相等断言
  （`tests/api/test_router.py:189-203`）——新增字段会**自动**被这两条覆盖。
- `payload_json` 往返一致性与"模型上下文不变"已有断言：
  `tests/sessions/test_sqlite_store.py:169`、`:457`，
  `tests/application/test_chat_service.py:184-210`。
  这些断言必须继续成立：**引用不得出现在 `load_messages()` 的返回值里**。

### 2.4 正文侧能力已齐备，历史引用不需要新接口

| 历史引用的需求 | 现有能力 |
| --- | --- |
| 按引用所属版本读正文（旧版本不是 active 也能读） | `GET /knowledge/documents/{id}/versions/{version_id}/content/`（4.4.8），返回 `version_no` / `active_version_id` |
| 「该引用来自历史版本 vN」提示 | 查看器已实现（`manual-test-runbook.md` F-14-3） |
| 文档被停用后仍能核对来源 | 正文接口对 `disabled` 文档返回 200（设计稿 4.1 既定口径） |
| 偏移为空 / 越界时降级 | 查看器降级路径已实现（F-14-5） |
| 版本正文是否可能被改写（偏移会不会失效） | `raw_text` 只在解析完成时从 `NULL` 回填一次（`app/knowledge/sqlite_store.py:562-593`），**解析后不可变**，偏移长期有效 |

---

## 3. 关键设计决策

### 3.1 持久化什么

| 数据 | 是否持久化 | 理由 |
| --- | --- | --- |
| 引用的**全部公开字段**：`citation_id` / `document_id` / `version_id` / `chunk_id` / `title` / `heading_path` / `content` / `start_offset` / `end_offset` | **是**（决策 3） | 与 `Citation` 字段一一对应；`content` 是**证据快照**：全部字段当时已经返回给同一用户，不新增数据暴露面（详见下方说明） |
| `answer_incomplete` | **是** | 服务端确定性判定的安全提示。**漏引场景下 `citations` 可能为空而该标记为 `true`**——不持久化就会让刷新后的回答"看起来一切正常" |
| `retrieval_summary` | 否（决策 1） | Agentic Search 的产物，当前生产走传统 RAG：`strategy`/`round_count` 是常量、`latency_ms` 是过程指标（3.4） |
| `events` / `pending_approvals` | 否 | 见第 11 节 |
| `raw_text`、完整 chunk 正文、向量载荷、检索 trace | **绝不** | 现有红线不变 |

存储形态：**一条引用一行**，写入新表 `message_citations`：

| 列 | 类型 | 含义 |
| --- | --- | --- |
| `id` | INTEGER PK AUTOINCREMENT | 行主键，仅作唯一标识，业务逻辑不依赖它 |
| `conversation_id` | TEXT NOT NULL | 所属会话（外键 → `conversations.id`） |
| `seq` | INTEGER NOT NULL | 引用所属消息的 `messages.seq`；只有 assistant 最终回答会有引用行 |
| `ordinal` | INTEGER NOT NULL | 引用在该回答中的展示次序（1..n，决定 C1..Cn 顺序） |
| `citation_id` | TEXT NOT NULL | `C1..Cn` 标签，与回答正文里的 `[C#]` 角标对应 |
| `document_id` | TEXT NOT NULL | 来源文档标识 |
| `version_id` | TEXT NOT NULL | **生成时冻结**的版本标识（读取历史引用时按它取正文，见 3.3） |
| `chunk_id` | TEXT NOT NULL | 命中的知识块标识 |
| `title` | TEXT NOT NULL | 文档标题 |
| `heading_path` | TEXT NULL | 标题路径；无则为 `NULL`（偏移缺失时用于回退到章节） |
| `content` | TEXT NOT NULL | **证据片段快照**：来自知识库的可信片段正文（非模型生成），刷新后卡片直接展示 |
| `start_offset` / `end_offset` | INTEGER NULL | 片段在所属版本文正中的字符区间；两者同生同灭 |
| `created_at` | TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP | 写入时间（UTC 文本） |

写入时机：

- **引用行**：只在 `citations` 非空时插入；纯聊天回答不插入任何引用行。
- **`answer_incomplete`**：落在 `messages.answer_incomplete`（`0` / `1`）。**本轮只要产生了最终
  回答就写**（包括"无检索、无引用"的纯聊天回答，写 `0`），这样 `NULL` 就只剩一个含义——
  「没有记录」：非最终回答行（带 `tool_calls` 的中间助手消息、`system` / `tool`）与升级前的
  历史行。读取时 `NULL` 与 `0` 一律当 `false`。

#### 为什么片段正文要一起入库（决策 3 的结论）

| 场景 | 行为 |
| --- | --- |
| 实时回答 | 本轮响应里的 `citations[].content` 直接渲染（引用卡片里的证据原文） |
| 刷新 / 重新进入会话 | 引用卡片**与实时完全一致**：`[C1]` 标签 + 文档标题 + 章节路径 + 证据原文 + 跳转入口 |
| 用户点「查看原文位置」 | 打开正文面板/整页，按 **`version_id` + `start_offset/end_offset`** 读取正文并高亮对应区间 |
| 偏移为 `null` | 现有降级路径：只打开文档（有 `heading_path` 时滚到对应章节），并提示「该引用未记录精确位置」 |

- **契约最简单**：历史响应直接复用 `app/knowledge/results.py` 的 `Citation`（**不新增第二套引用
  模型**），前端 `CitationList`、`CitationTarget`、`ChatView.onDocument` 全部零改动；
  "契约钉子"测试就是一个干净的等式 `MESSAGE_CITATION_COLUMNS == Citation.public_dict().keys()`。
- **审计保真**：行内保存的是"回答当时那一版正文里的证据快照"。将来即便有人改写或清理
  chunk，历史回答展示的证据也不会变——这正是"回看当时依据"想要的语义。
- **代价：库里多存一份片段文本**。按 chunk 上限 700 token（`app/knowledge/chunking.py:47`）
  估算，中文一条片段约 1~3KB，每条回答 3~5 条引用 → 每答约 5~10KB；历史响应也随之增大
  （25 答的会话约 150~250KB，而历史响应本来就已经返回全部回答正文）。若实测不可接受，
  处理顺序是：**先在展示层做**（长片段折叠/省略，不改契约）→ 再考虑给历史接口加窗口/分页
  → 最后才是"不存正文、改成按需取"（那会引入两套引用形状，见下）。
- **决策留痕**：一度评估过"不入库"。结论是不入库反而更重——需要后端 `ConversationCitation`、
  前端同形 interface 与 `CitationDisplay` 联合类型、`CitationList` 条件渲染与解释文案，
  外加"刷新前后展示不一致"这一长期需要解释的行为差异，买到的只是不重复存一份文本。

### 3.2 存在哪里（本题的"要不要建新表"）

**采纳：方案 B —— 新建 `message_citations` 表（一条引用一行）。**
否决：方案 C —— 写进现有 `payload_json`。参考：方案 A —— `messages` 加一个 JSON 列
（曾作为"最小改动"备选，已按负责人意见放弃）。

| 方案 | 做法 | 结论 |
| --- | --- | --- |
| **B（采纳）** | 新表 `message_citations`，一条引用一行；`answer_incomplete` 作 `messages` 新列 | 列名与 `Citation` 字段一一对应（含证据片段快照，决策 3），**读表结构即懂**，维护成本最低；引用与消息**同事务**写入，不会出现"有消息没引用"的半截状态；将来若要按文档/版本反查引用，加条索引即可；字段演进时"加列 + 迁移"是显式、可评审的动作，不会悄悄藏进一个 JSON blob。代价：多一条写路径、多一组读方法与测试 |
| A（放弃） | `messages` 加一列 `structured_json`，存 `{citations, answer_incomplete}` | 改动最小（一条 `ADD COLUMN`），但列里是不透明 JSON：字段演进不进迁移、出问题只能翻 JSON 排查，也无法按文档反查。本轮明确不采用 |
| C（否决） | 写进现有 `payload_json` | **会进入模型上下文**（2.2）：引用片段会成为助手历史消息的一部分被回灌给模型，既污染上下文，又可能被网关拒绝；且破坏 `load_messages` 与 `payload_json` 的一一对等关系。**不做** |

设计要点（每一条都是为了"后来者不用猜"）：

- **用 `(conversation_id, seq)` 关联，不用 `messages.id`**：`seq` 是仓库里既有的稳定消息
  业务标识（`uq_messages_conversation_seq`，迁移 `0001:76-80`），`append_messages` 本来就
  自己计算 `seq`（`app/sessions/sqlite_store.py:304-321`）；改用自增主键则要 `RETURNING` /
  `lastrowid` 回填，批量插入时更易写错。历史读路径本来就按 `seq` 排序，口径统一。
- **`answer_incomplete` 放 `messages`，不放引用表**：它是**回答级**属性（这轮引用校验是否
  判定不完整），而且"漏引"场景下可能**一条引用都没有**却仍为 `true`；硬塞进引用表只能造一条
  空引用行，语义会被扭曲。
- **两道外键**：`conversation_id → conversations.id` 与
  `(conversation_id, seq) → messages(conversation_id, seq)`（复用既有复合唯一约束），
  防止引用行成为孤儿。SQLite 连接已开 `PRAGMA foreign_keys = ON`
  （`app/sessions/sqlite_store.py:27`）。
- **不设 `ON DELETE CASCADE`**：会话/消息删除接口目前不存在（`docs/frontend/api-inventory.md`
  §5.4 第 1 条把"会话删除"列为缺口），本次不预置级联，以免掩盖"删会话时引用怎么办"这个决策；
  将来新增删除能力时再显式决定（加级联或显式清理），不要留下孤儿行。
- **两个唯一约束**：`UNIQUE (conversation_id, seq, citation_id)` 防同一回答重复写同一引用
  （重试幂等），`UNIQUE (conversation_id, seq, ordinal)` 保证展示顺序唯一。
- **分层不反向**：`app/sessions` **不 import** `app/knowledge`（保持现状：sessions 只依赖
  `app.db.migrations`）。写路径由应用层把 `Citation` 投影成 dict（`Citation.public_dict()`，
  含 `content`），读路径由应用层把 dict 转回 `Citation`；两个方向的映射各只有一处，
  并用一条测试把 **`Citation.public_dict()` 的字段与表的业务列钉死**（见 7.1 第 12 条）。

### 3.3 历史引用与"当时版本"的关系（设计稿点名要拍板的问题）

**结论：引用在生成时被"冻结"，读取历史时按 `version_id` 原样解析，绝不重新解析到当前有效版本。**

- 偏移（`start_offset` / `end_offset`）只对**引用所属版本**的 `raw_text` 有效；
  一旦换成当前 active 版本，偏移与引用正文会同时失真，属于红线（见第 10 节）。
- 因此历史引用点开后走的就是现有正文接口的"历史版本"分支，界面已会提示
  「该引用来自历史版本 vN，当前有效版本为 vM」，无需新逻辑。
- 版本正文解析后不可变（2.4），所以历史引用的偏移与片段**长期保持一致**。
- 文档之后被停用：正文接口仍返回 200（既定决策），历史引用仍可核对；
  界面已有的「该文档已停用」提示继续生效。
- 文档/版本目前**没有删除接口**，因此本次**不需要**"引用已失效则过滤"的逻辑；
  真正取不到时由查看器的失败提示兜底（"来源文档不存在或已不可访问"）。
- 顺带明确：不做"把旧引用升级到最新版本"，也不做"新旧版本对照"——那是另一个功能。

### 3.4 `retrieval_summary` 的来源复核（为什么传统 RAG 也有它）

生产链路现在走的是**传统 RAG**（`BaselineKnowledgeSearchService`），所以先澄清一个容易误解的点：
**`retrieval_summary` 不是传统 RAG 算出来的，而是从 Agentic Search 沿用下来的对外契约字段，
由 Gateway 适配层补齐。**

| 字段 | 传统 RAG 链路里的真实来源 | 信息量 |
| --- | --- | --- |
| `strategy` | 常量 `STRATEGY = "baseline"`（`app/knowledge/retrieval.py:55`） | 仅标识"这条回答走的是传统 RAG 管线" |
| `round_count` | 常量 `ROUND_COUNT = 1`（`app/knowledge/retrieval.py:56`） | 恒为 1，无信息量 |
| `evidence_status` | 管线真实结果：`sufficient` / `insufficient` / `failed`（`app/knowledge/retrieval.py:229-247`） | 有真实信息，且是漏引判据 |
| `latency_ms` | `time.perf_counter()` 实测（`app/knowledge/retrieval.py:249`） | 有真实信息（过程指标） |

存在原因（按时间顺序）：

1. **Stage A（2026-08-07）**：`RetrievalSummary` 与
   `BaselineSearchResult(..., retrieval_summary, ...)` 一开始就是传统 RAG 的结果契约
   （`docs/superpowers/plans/2026-08-07-tenant-scoped-rag-stage-a.md:1222-1223`），
   目的是让 baseline 与后来的 adaptive/agentic **结果同构**：Chat 层只写一套引用校验、
   Stage C 用同一张报表做 A/B。
2. **Stage B（2026-08-12）**：Agentic Search 的公开形状定为
   `{result_code, strategy, evidence_status, citations, retrieval_summary}`，
   此时 `strategy ∈ single / multi / none / unplanned` 才真正有决策含义。
3. **2026-09-16 切换（commit `5a023a1`「将智能体知识检索切换为传统RAG」）**：
   `KnowledgeToolGateway` 的注入从 `AdaptiveKnowledgeSearchService` 换成
   `BaselineKnowledgeSearchService`；为了**不动协议**，新增
   `_baseline_tool_payload()`（`app/tools/knowledge_gateway.py:51-72`）
   把 baseline 结果翻译成上面那套形状。`strategy="baseline"` 由此变成前端与评测
   识别"走传统 RAG"的标记。

`retrieval_summary` 在链路里是**承重字段**，不能当装饰删掉：

- `app/agent/runner.py:354-361`：**只有** `data.retrieval_summary` 是 dict 时，才把该
  payload 记为 `knowledge_payload`。没有它 → 引用校验与 `LLMResponse.citations` 全部失效
  （前端引用卡片、以及本次要持久化的东西会一起消失）。
- `app/agent/runner.py:91-96`：用它重建响应字段；`data["evidence_status"]` 决定
  `answer_incomplete` 的"漏引"分支。
- 前端 `frontend/src/components/chat/MessageList.vue:94-97`（+`:162-164`）：
  「检索：baseline · 1 轮 · 证据 sufficient · 123 ms」。
- 评测报表：`app/evals/stage_c/reporting.py:409,446-453`。

与"审计"的关系：每次检索**已经**有一条耐久记录写进 `retrieval_events`
（`strategy` / `outcome` / `round_count` / `latency_ms` / `selected_chunk_ids_json` 等，
`app/knowledge/retrieval.py:399-427`）。但工具链路是以 `conversation_id=None` 写入的
（`app/tools/knowledge_gateway.py:41-45`，该列可空：迁移 `0009:230`），
**无法关联回会话/消息**——所以"刷新后想看到当时那条检索摘要"补不回来，只能持久化。

→ 结论：第 9 节决策点 1 的建议不变。传统 RAG 下 `strategy`/`round_count` 是常量，
`latency_ms` 是过程指标，真正有安全意义的 `evidence_status` 已经由 `answer_incomplete`
（加上 `citations` 是否为空）表达；若产品确实要求"刷新后仍显示检索那一行"，
再把这四个字段加进同一个 JSON 即可（纯增量、无需再迁移）。

### 3.5 旧数据怎么处理

- 升级前产生的回答**没有可恢复的引用数据**（当时就没写库），`message_citations` 里
  没有对应行，`messages.answer_incomplete` 为 `NULL`，历史接口按空引用返回，
  前端渲染路径与今天完全一致。
- 因此本改动**只对升级后新产生的回答生效**，且这一事实必须写进验收清单
  （把 F-14-9「刷新后引用消失」改写为「升级前的历史回答仍无引用」），避免被当成 Bug 上报。
- 不做回填脚本：无法从消息正文可靠地反推出 `document_id` / `version_id` / 偏移，
  猜测回填会把"无法定位"变成"错误定位"。

### 3.6 安全边界（本次唯一扩张点，必须逐条守住）

1. **不新增数据类别**：持久化与返回的字段是"本轮响应里同一用户已经看到过的"；
2. **白名单不变**：只有 `_to_visible_message` 判定为可见的 assistant 最终回答才带引用；
   `system` / `tool` / 中间助手消息即使库里有引用行也一律不返回；
3. **模型上下文不变**：引用根本不进 `payload_json`（独立表），结构上不可能污染模型上下文，
   `load_messages()` 的返回值与今天完全一致（现有断言不变，见 2.3）；
4. **租户隔离不变**：历史接口仍只经 `get_current_tenant` 取 `organization_id`
   并按 (企业, 用户) 双重限定；`message_citations` 自身**不带** `organization_id` 列，
   归属完全由所属会话决定——查询一律带 `conversation_id`，且该会话已通过归属校验；
   引用里的 `document_id` 是否可读，由正文接口在点击时再校验一次（跨租户 404）；
5. **坏数据不炸接口**：读到的引用行若字段非法（如 `citation_id` 为空、偏移只有一个），
   只丢弃该行并留诊断日志，不让一条脏数据把整个会话历史变成 500。

---

## 4. 后端改动方向

### 4.1 迁移 `0014_message_citations`

- 文件：`migrations/versions/0014_message_citations.py`，
  `down_revision = "0013_document_versions_async_ingestion"`。
- `upgrade()` 两步（都是轻量操作，**不重建** `messages`）：

  1. `op.create_table("message_citations", ...)` —— 列见 3.1，约束：
     - 外键：`conversation_id → conversations.id`；
       `(conversation_id, seq) → messages(conversation_id, seq)`；
     - `UniqueConstraint("conversation_id", "seq", "citation_id",
       name="uq_message_citations_answer_citation")`；
     - `UniqueConstraint("conversation_id", "seq", "ordinal",
       name="uq_message_citations_answer_ordinal")`；
     - 索引：`idx_message_citations_conversation_seq`（`conversation_id, seq`）——
       本次读路径唯一需要的索引；`(document_id, version_id)` 反查索引**本次不建**（决策 5），
       将来做"哪些回答引用了这份文档"时再加一条 `CREATE INDEX` 即可。
  2. `op.add_column("messages", sa.Column("answer_incomplete", sa.Integer(), nullable=True))`
     （可空、无默认 → SQLite 直接 `ADD COLUMN`，无需 `batch_alter_table`）。
- CHECK 约束（沿用仓库习惯，把"不可能的状态"挡在库外）：

  | 约束名 | 表达式 | 挡住的错误 |
  | --- | --- | --- |
  | `ck_message_citations_citation_id_not_blank` | `length(trim(citation_id)) > 0` | 空标签写进库；正文 `[C#]` 将无法对应 |
  | `ck_message_citations_ordinal_positive` | `ordinal >= 1` | 展示顺序从 0 开始等口径漂移 |
  | `ck_message_citations_offsets_paired` | `(start_offset IS NULL) = (end_offset IS NULL)` | 只写一个偏移（"同生同灭"库级不变量） |
  | `ck_message_citations_offsets_ordered` | `start_offset IS NULL OR end_offset > start_offset` | 反向/零长区间 |
  | `ck_message_citations_offsets_non_negative` | `start_offset IS NULL OR start_offset >= 0` | 负偏移 |
  | `ck_message_citations_content_not_blank` | `length(trim(content)) > 0` | 空证据片段（与 `document_chunks` 的同名约束口径一致，见迁移 `0009:161-164`） |

  `heading_path` 允许 `NULL`（引用可以没有章节路径），不加约束。
- `downgrade()`：`op.drop_column("messages", "answer_incomplete")` →
  `op.drop_index("idx_message_citations_conversation_seq")`（及可选索引）→
  `op.drop_table("message_citations")`。**降级会丢弃已持久化的引用数据**
  （消息本体不受影响），必须在迁移文档与提交信息里写明。
- 迁移文件顶部按仓库惯例写中文说明文档字符串：为什么建这张表而不是加 JSON 列、
  为什么 `answer_incomplete` 留在 `messages`、为什么用 `(conversation_id, seq)` 关联、
  降级会丢什么。
- 本机 SQLite 3.39.4 原生支持 `CREATE TABLE` / `ADD COLUMN` / `DROP COLUMN` / `DROP TABLE`；
  若部署环境 SQLite < 3.35，`drop_column` 改用 `batch_alter_table(recreate="always")`
  （`messages` 没有入向外键，重建安全，但要保留 `uq_messages_conversation_seq` 与
  `idx_messages_conversation_seq`）。

### 4.2 会话存储（`app/sessions`）

| 位置 | 改动 |
| --- | --- |
| `base.py` 新增 `MessageDisplay` | 冻结 dataclass：`citations : tuple[dict, ...] = ()`（每项键 = `MESSAGE_CITATION_COLUMNS`，即 `Citation.public_dict()` 全集）、`answer_incomplete : bool = False`；每个属性带中文注释，并写明「仅供展示与历史读取使用，**绝不进入模型上下文**」 |
| `base.py` `MessageRecord` | 新增 `citations : tuple[dict, ...] = ()` 与 `answer_incomplete : bool = False`（带中文注释）。带默认值 → 唯一构造点在 `sqlite_store.py:180`，安全 |
| `base.py` `SessionStore` 协议 | `append_messages` 增可选 `display` 形参，并写明「与 `messages` 等长、与消息同事务写入、不进入 `payload_json`」 |
| `sqlite_store.py` `append_messages` | 新增关键字参数 `display : list[MessageDisplay \| None] \| None = None`；长度必须与 `messages` 相等，否则 `ValueError`。在**同一事务**内：插消息 → 按本地算出的 `seq` 插引用行 → 写入 `messages.answer_incomplete`（并入消息 `INSERT` 的列值即可） |
| `sqlite_store.py` `load_message_records` | 先查消息行，再 `SELECT ... FROM message_citations WHERE conversation_id = ? ORDER BY seq ASC, ordinal ASC`，按 `seq` 分组填进各 `MessageRecord`（升级前/无引用的消息得到空元组） |
| `sqlite_store.py` `load_messages` | **不改**（模型上下文路径一行不动） |
| `sqlite_store.py` 模块常量 | 新增 `MESSAGE_CITATION_COLUMNS`（业务列白名单，与 `Citation.public_dict()` 的键**完全相等**）：写入前用它校验 dict 的键（**缺列 → `ValueError`**，多余键忽略，避免静默写入半截数据）；7.1 第 12 条的"列 ↔ 字段"钉子测试也复用它 |

写入接口形态（第 9 节决策点 2，已拍板为**显式可选参数** `display`）：

```python
new_messages = messages[new_messages_start:]
display: list[MessageDisplay | None] = [None] * len(new_messages)
index = _final_answer_index(new_messages)          # 末尾向前找无 tool_calls 的 assistant
if index is not None:
    display[index] = MessageDisplay(
        citations=tuple(item.public_dict() for item in response.citations),  # 键与 message_citations 列一一对应
        answer_incomplete=response.answer_incomplete,
    )
self._store.append_messages(..., messages=new_messages, display=display)
```

备选（不推荐）：在消息 dict 里塞一个保留键（如 `_citations`），写入前剔除。
缺点：会把展示数据混进"模型上下文载荷"那份 dict 里，任何后续误用都可能把它带进模型请求；
显式参数让"消息"与"展示数据"从一开始就是两件事。

无论采用哪种，测试都要断言 `payload_json` 里**没有** `citations` / `_citations`
（见 7.1 第 3 条）。

### 4.3 应用层（`app/application/chat_service.py`）

1. `chat()`：构造上面的 `display` 列表并传给 `append_messages`。识别"最终 assistant 消息"的
   方式：从 `messages[new_messages_start:]` 末尾向前找第一条 `role == "assistant"` 且
   无 `tool_calls` 的消息（`runner.py:219-248` 保证它是本轮最后一条）。找到时**无论有没有引用
   都填** `MessageDisplay(citations=..., answer_incomplete=response.answer_incomplete)`
   （`citations` 为空就只写 `answer_incomplete`，不插引用行）；找不到（理论上不会发生）
   时全部传 `None`，**不报错**。
2. `_to_visible_message()`：新增 `citations` / `answer_incomplete` 两个返回字段。
   - `user` 消息恒为 `[]` / `False`；
   - assistant 最终回答把 `stored.citations` 里的 dict **逐条**转成 `Citation`
     （`Citation(**item)`），非法/缺字段的条目丢弃并留诊断日志（3.6 第 5 条）；
   - 升级前的历史记录没有引用行 → 与今天的输出完全一致。
3. 两个方向的映射各只写一遍：`Citation → dict` 直接用现成的 `Citation.public_dict()`；
   `dict → Citation` 用一个模块级小函数（中文 docstring）。两个方向都要有测试。
4. 不改动：`get_history()` 的归属校验与顺序、`load_messages` 的调用、锁与幂等逻辑。

### 4.4 响应契约（`app/schemas/chat.py`）

```python
class ConversationHistoryMessage(BaseModel):
  # ……现有字段不变……
  citations : list[Citation] = Field(default_factory=list)  # 该回答的知识引用（C1..Cn 原序，字段与实时响应完全一致）；用户消息与无引用回答为空数组
  answer_incomplete : bool = False  # 该回答生成时的引用完整性标记；为 True 时界面须给出「谨慎采用」提示
```

- **复用 `app/knowledge/results.py` 的 `Citation`，不新增第二套引用模型**：决策 3 决定片段正文
  一起入库，历史引用与实时引用字段**完全同形**（含 `content`），前端 `CitationList`、
  `CitationTarget`、`ChatView.onDocument` 全部零改动，也没有"历史引用少一个字段"这种长期解释成本。
- 新增字段放在 `ConversationHistoryMessage` 上（而不是新开一个响应模型），
  Pydantic v2 默认忽略未知字段，将来 `Citation` 增字段时**旧数据不会被判为非法**
  （缺失字段取默认值；`Citation.content` 等必填字段在库内一定有值）。
- 默认值保证：历史接口对任何"没有引用行"的消息都返回 `[]` / `false`，前端无需处理字段缺失。
- 契约钉子：`MESSAGE_CITATION_COLUMNS == Citation.public_dict().keys()`（7.1 第 12 条）——
  `Citation` 将来加字段却忘了加列时，这条测试会立刻失败。

### 4.5 明确不改的文件（防止顺手扩大改动面）

`app/agent/runner.py`（不改消息结构，避免污染模型上下文与大量断言精确相等的 runner 测试）、
`app/knowledge/**`（正文接口、检索、`Citation` 均已满足）、
`app/api/router.py`（路由与错误口径不变）、
`frontend/src/views/ChatView.vue`（`open-document` 链路已存在，历史引用复用同一 `CitationTarget`）。

---

## 5. 前端改动方向

### 5.1 类型与 API（`frontend/src/api/types.ts`）

```ts
export interface ConversationHistoryMessage {
  // ……现有字段不变……
  /** 该回答的知识引用（结构化字段，禁止从回答文本解析）；无引用时为空数组。
   *  历史引用与实时引用**同形**（都含 content，见后端约定）。 */
  citations: Citation[]
  /** 该回答生成时的引用完整性标记；true 时回答上方显示「谨慎采用」提示 */
  answer_incomplete: boolean
}
```

- **不引入任何新类型**：决策 3 决定片段正文一起入库，历史引用就是 `Citation[]`，
  与实时响应完全一致；`CitationList`、`CitationTarget`、`ChatView.onDocument` 全都零改动。
- 唯一需要注意的兼容性事实（既有约定，不是本次新增）：`start_offset` / `end_offset` 在
  `Citation` 上是**可选可空**（旧载荷可能缺失），前端一律用 `== null` 判断降级，
  仍**禁止**用 `!start_offset`（偏移 0 是合法值，见 `frontend/src/api/types.ts:240-252`）。

### 5.2 状态层（`frontend/src/stores/chat.ts`）

- `historyToView()`：把历史消息的 `citations` / `answer_incomplete` 带进视图模型。
- `ChatMessageView` 新增**回答级**结构化字段（历史与实时都填，渲染时统一读取）：

  ```ts
  /** 回答随附的结构化展示信息：实时来自本轮响应，历史来自服务端持久化结果 */
  structuredAnswer: { citations: Citation[]; answerIncomplete: boolean } | null
  ```

  - 历史消息：由历史响应填充（无引用时也填 `{ citations: [], answerIncomplete: false }`，
    便于模板分支判断）；
  - 实时消息：在 `sendMessage()` 成功分支由 `response.citations` / `response.answer_incomplete` 填充。
- **不要**给历史消息伪造 `structured: LLMResponse`：那会让 `MessageList` 走去实时分支，
  凭空渲染出处理过程与审批卡片，违反 store 头部注释里"不伪造"的既有约定。
  只把"引用与完整性标记"从"不伪造"改成"来自服务端持久化"。
- 同步更新 store 头部注释（`frontend/src/stores/chat.ts:1-14`）与
  `ChatMessageView.structured` 的注释：历史消息现在**有**引用，但仍然没有 events / 审批。

### 5.3 渲染层（`frontend/src/components/chat/MessageList.vue`）

- 历史分支（`v-else` 的 `MarkdownContent`，当前实现见 `:181-184`）：
  - `AssistantAnswer` 传 `:answer-incomplete="message.structuredAnswer?.answerIncomplete ?? false"`
    与 `:citation-links="(message.structuredAnswer?.citations.length ?? 0) > 0"`；
    `AssistantAnswer` 的 props 需要新增 `citationAnchorPrefix`（见 5.4）；
  - 非空时在其后渲染 `CitationList`，沿用**已有的** `@open-document` 上抛（`:165-169`）；
  - 建议把「回答正文 + 引用列表」抽成一个小组件或模板片段，避免实时/历史两个分支各写一遍
    导致后续再次漂移。
- **`CitationList` 与 `AssistantAnswer` 的引用渲染逻辑零改动**（决策 3：历史引用同样带
  `content`）：历史回答下的引用卡片与实时完全一致——标签、标题、章节路径、证据原文、
  「查看原文位置」按钮；唯一新增的是 5.4 的锚点前缀 prop。
- 实时分支保持不变（`structured` 仍负责处理过程、检索摘要、待审批）。

### 5.4 配套修复：引用锚点必须按回答作用域（**必须与本次一起做**）

现状缺陷（今天已存在，但本次改动会把它从"偶发"变成"必然"）：

- `CitationList` 给卡片写的是**全局 id**：`:id="`citation-${citation.citation_id}`"`
  （`frontend/src/components/chat/CitationList.vue:70`）；
- `MarkdownContent` 点击 `[C1]` 时用 `document.getElementById('citation-C1')`
  全局查找（`frontend/src/components/common/MarkdownContent.vue:31`）；
- 一次会话里**多条带引用的回答**都在同一页面时，每个回答都有 `C1` → **重复 id**，
  点第二条回答的 `[C1]` 会跳到第一条回答的卡片（HTML id 必须唯一，`getElementById`
  只返回第一个）。
- 实时多轮今天就能复现；引用持久化后，刷新出来的历史回答全都带引用，问题必然暴露。

修复方向（最小改动、双向对齐）：

1. `CitationList` 新增 `anchorPrefix` prop（如 `answer-12`），卡片 id 变成
   `${anchorPrefix}-citation-${citation_id}`；
2. `MarkdownContent`（以及 `AssistantAnswer` 的透传）新增同名 `citationAnchorPrefix`，
   查找时拼同一个前缀；
3. `MessageList` 用消息自身的稳定标识（如 `answer-${message.id}`）作为前缀；
4. 补测试：**同一页面两条回答都含 `C1` 时，点击各自正文里的 `[C1]` 只滚动到自己的卡片**。
   受影响测试：`MarkdownContent.spec.ts:57-64`（手工造 `id = 'citation-C1'` 的卡片）、
   `CitationList.spec.ts`（如需断言 id）、`AssistantAnswer.spec.ts`（props 透传）。

---

## 6. 文件清单

### 6.1 后端（9 个文件）

| 文件 | 改动 |
| --- | --- |
| `migrations/versions/0014_message_citations.py` | **新增**：建表 `message_citations`（外键/唯一约束/CHECK/索引）+ `messages.answer_incomplete` 列（含中文迁移说明与降级） |
| `app/sessions/base.py` | 新增 `MessageDisplay` dataclass；`MessageRecord` 增 `citations` / `answer_incomplete`；`append_messages` 协议增 `display` 形参（属性与形参均带中文注释） |
| `app/sessions/sqlite_store.py` | 写入侧：同事务插引用行 + 写 `answer_incomplete`；读取侧 `load_message_records` 增查引用表并按 `seq` 分组；新增 `MESSAGE_CITATION_COLUMNS` |
| `app/application/chat_service.py` | 写入侧：构造 `display`（`Citation.public_dict()`）；读取侧：`_to_visible_message` 输出引用（`dict → Citation`）与完整性标记 |
| `app/schemas/chat.py` | `ConversationHistoryMessage` 增 `citations`（复用 `Citation`）/ `answer_incomplete` |
| `tests/db/test_migrations.py` | **新增** 0014 迁移用例（表/列/索引存在、约束生效、降级删表删列、往返升级、旧数据不受影响） |
| `tests/sessions/test_sqlite_store.py` | 引用行写入与读回（含顺序）、`payload_json` 不含引用、`load_messages` 零变化、无引用为默认值、`display` 长度不符报错 |
| `tests/application/test_chat_service.py` | 历史带引用（真实 SQLite 走一遍 chat → 历史）、用户/中间消息不带引用、坏数据丢弃 |
| `tests/api/test_router.py` | 真实响应含新字段、防泄漏回归、跨租户 404 不回归 |

> `app/agent/runner.py`、`app/knowledge/**`、`app/api/router.py` **不改**（见 4.5）。

### 6.2 前端（6 个文件）

| 文件 | 改动 |
| --- | --- |
| `src/api/types.ts` | `ConversationHistoryMessage` 增 `citations: Citation[]` / `answer_incomplete`（**无新类型**） |
| `src/stores/chat.ts` | `ChatMessageView` 增 `structuredAnswer`（`Citation[]`）；`historyToView` / `sendMessage` 填充；更新头部注释 |
| `src/components/chat/MessageList.vue` | 历史回答渲染引用卡片与完整性提示；传入引用锚点前缀 |
| `src/components/chat/CitationList.vue` | 新增 `anchorPrefix` prop，卡片 id 加作用域前缀（引用渲染逻辑不变） |
| `src/components/common/MarkdownContent.vue` + `src/components/chat/AssistantAnswer.vue` | `[C1]` 定位使用同一作用域前缀（透传 prop） |
| `src/stores/__tests__/chat.spec.ts`、`src/components/chat/__tests__/MessageList.spec.ts`、`src/components/chat/__tests__/CitationList.spec.ts`、`src/components/common/__tests__/MarkdownContent.spec.ts` | 历史引用映射与卡片渲染、旧响应兜底、"两条回答的 [C1] 各自归位" |

### 6.3 文档（5 个文件）

| 文件 | 改动 |
| --- | --- |
| `docs/frontend/api-inventory.md` | 4.3.4（`:505-533`）改写：历史接口现在返回 `citations` 与 `answer_incomplete`（**引用模型与实时响应同为 `Citation`，字段见 A.8**），并说明 events/审批仍不保存；附录 A.5（`:1119-1130`）字段表补两行；§5.3（`:965-983`）"刷新后只恢复问答文本"的口径更新；头部增量记录加一条 |
| `docs/frontend/manual-test-runbook.md` | E-09（`:525-531`）改为"刷新后引用卡片仍在、可继续跳转，且与刷新前一致"；F-14-9（`:785-791`）改写为"升级前的历史回答仍无引用"；第 15 条已知限制（`:1067`）同步；新增"历史引用跳转 + 停用文档 + 跨租户"验收项 |
| `README.md` | 能力清单（`:60-64`）补"引用随回答持久化，刷新后仍可核对来源"；已知限制（`:77-78`）移除该项 |
| `docs/superpowers/specs/2026-09-09-knowledge-document-content-viewer-design.md` | §6 第 1 项标记"已实施（见本文档）"；§3 决策 6 的口径更新 |
| `docs/database-migrations.md` | 第 7 节验收命令补 `tests/db/test_migrations.py` 相关说明（如无变化则仅确认） |

---

## 7. 测试与验收

### 7.1 后端验收（每条一个测试，测试方法必须有**中文注释**）

| # | 断言 | 位置 |
| --- | --- | --- |
| 1 | 迁移后 `message_citations` 表存在且列与 3.1 一致；`messages` 有 `answer_incomplete` 列；旧行不受影响（引用表为空、该列为 `NULL`）；降级后表与列消失且消息数据不丢 | `tests/db/test_migrations.py` |
| 2 | 唯一约束与 CHECK 真的生效：同 `(会话, seq, citation_id)` 插两次报错；只写 `start_offset` 报错；`ordinal = 0` 报错 | `tests/db/test_migrations.py` 或 `tests/sessions/test_sqlite_store.py` |
| 3 | `append_messages(messages=[...], display=[...])` 后：引用行按 `ordinal` 落库；`citations` 为空时**不插引用行但仍写 `answer_incomplete=0`**；**且 `payload_json` 里不含任何 `citation` 相关键**（模型上下文零污染） | `tests/sessions/test_sqlite_store.py` |
| 4 | `load_messages()` 返回值与调用方传入的模型载荷**逐字相等**（模型上下文零变化） | `tests/sessions/test_sqlite_store.py` |
| 5 | `load_message_records()` 把引用按 `seq` 分组带回且顺序正确；没有引用的消息得到空元组、`answer_incomplete=false` | `tests/sessions/test_sqlite_store.py` |
| 6 | `display` 长度与 `messages` 不一致 → `ValueError`（不静默错位写入） | `tests/sessions/test_sqlite_store.py` |
| 7 | 走完 `chat()`（真实 SQLite + 假 runner 返回带引用回答）后，`get_history()` 的对应 assistant 消息带**完整引用**（含 `content`）与偏移 | `tests/application/test_chat_service.py` |
| 8 | 用户消息恒为空引用；带 `tool_calls` 的中间助手消息、`system`/`tool` 消息即使库里有引用行也**不返回** | `tests/application/test_chat_service.py` |
| 9 | 引用行字段非法（空 `citation_id`、只有一个偏移）时：历史接口仍 200，坏行被丢弃（不炸接口） | `tests/application/test_chat_service.py` |
| 10 | 真实 SQLite 迷你应用：`GET /conversations/{id}/messages/` 响应含新字段，且引用的每一个字段（含 `content`）都与落库一致 | `tests/api/test_router.py` |
| 11 | **防泄漏回归**：历史响应不得出现 `reasoning_content` / `tool_calls` / 工具参数与结果 / `raw_text` | `tests/api/test_router.py` |
| 12 | **契约钉子**：`set(MESSAGE_CITATION_COLUMNS) == set(Citation.public_dict().keys())`——将来给 `Citation` 加字段却不加列（或反过来）时，这条测试立刻失败 | `tests/knowledge/test_results.py` 或 `tests/sessions/test_sqlite_store.py` |
| 13 | OpenAPI 字段集合与真实响应一致（现有用例自动覆盖新字段，不得放宽） | `tests/api/test_router.py:273-292` |
| 14 | 跨租户/跨用户读历史仍 404（不回归） | `tests/api/test_router.py:206-212` |

命令：

```powershell
python -m pytest tests/db/test_migrations.py tests/sessions tests/application/test_chat_service.py tests/api/test_router.py -q
python -m pytest -q
```

### 7.2 前端验收（vitest，测试方法必须有**中文注释**）

| # | 断言 |
| --- | --- |
| 1 | 历史响应带 `citations` → 视图消息带引用，且 `structured` 仍为 `null`（不伪造 events/审批） |
| 2 | 历史响应**没有** `citations` 字段（老后端/升级前数据）→ 空数组、不报错、不渲染卡片 |
| 3 | 历史回答渲染引用卡片，点击「查看原文位置」emit 结构化载荷（`documentId/versionId/startOffset/endOffset/headingPath/title`） |
| 4 | 历史引用的 `content` 与实时一样渲染成证据引用块（`blockquote` 存在、文本一致）——刷新前后展示无差异 |
| 5 | 历史回答 `answer_incomplete=true` → 显示「谨慎采用」提示；`false` → 不显示 |
| 6 | 同一页面两条回答都含 `C1`：点击各自正文里的 `[C1]` 只滚动到**自己**的引用卡片（锚点作用域） |
| 7 | 现有历史消息安全用例按新语义更新（"没有结构化数据就不渲染"，而不是"永不渲染"），**不得**留下跳过或删除的用例 |

命令：

```powershell
cd frontend
npm run typecheck
npm test
```

### 7.3 手工验收（写进 `manual-test-runbook.md`）

1. 拿一条带引用的回答 → **F5 刷新** → 引用卡片**与刷新前完全一致**（标签 `[C1]` + 文档标题 +
   章节路径 + 证据原文 + 「查看原文位置」按钮），点击后正文面板打开并高亮到对应区间；
2. 用旧版本引用刷新 → 仍提示「该引用来自历史版本 vN，当前有效版本为 vM」；
3. 停用来源文档后刷新 → 引用仍可核对来源，界面提示文档已停用；
4. 用企业 B 的账号读企业 A 的会话 → 404（会话不可见）；即便手工拿到引用，正文接口仍 404；
5. 升级**之前**产生的历史回答 → 仍无引用卡片（预期行为，不是 Bug）；
6. 断网/后端不可用时点历史引用 → 面板内报错并可重试，对话区不受影响；
7. 带偏移缺失的引用刷新后点击 → 降级为"只打开文档"（有章节路径则滚到该章节），
   不上屏错误。

---

## 8. 任务拆分与顺序

> 通用验收要求（沿用设计稿 §5）：只改本任务列出的文件（跨任务改动在本文档登记）；
> 自带测试且新增/修改的测试方法有中文注释；`pytest` / `vitest` 全绿、`npm run typecheck` 通过；
> **每个任务单独提交，commit message 用中文并写明"任务编号 + 验收命令"**（`AGENTS.md` §2/§3）。
> 动手前先 `git status` 确认工作区干净。

| 阶段 | 任务 | 内容 | 依赖 |
| --- | --- | --- | --- |
| A | A1 迁移与会话存储 | 迁移 0014（建 `message_citations` + `messages.answer_incomplete`）+ `sessions` 读写 + 存储层/迁移测试 | 无 |
| A | A2 应用层与响应契约 | `chat_service` 写入（构造 `display`：`Citation.public_dict()`）与读取（`dict → Citation`）+ `schemas.chat` + 服务层与接口测试（含防泄漏、契约钉子） | A1 |
| A | A3 接口文档同步 | `api-inventory.md` 4.3.4 / A.5 / §5.3 + 头部增量 | A2（契约冻结） |
| B | B1 类型与 store | `api/types.ts`、`stores/chat.ts` + store 测试 | A2 |
| B | B2 渲染与锚点修复 | `MessageList`、`CitationList`、`MarkdownContent`、`AssistantAnswer` + 组件测试 | B1 |
| C | C1 验收与文档收口 | runbook、README、2026-09-09 设计稿 §6、迁移文档 | B2 |

建议先做 A 阶段并冻结契约，再做前端：A 完成后即可用 curl/手工构造历史响应验证前端渲染。

---

## 9. 决策点

### 9.1 已拍板（2026-09-17，项目负责人）

| # | 决策 | 结论 |
| --- | --- | --- |
| 0 | 存储形态 | **独立表** `message_citations`（3.2 方案 B） |
| 1 | 是否连检索摘要一起存 | **不存**。`retrieval_summary` 是 Agentic Search 的产物，当前生产走传统 RAG（来源与承重关系见 3.4），暂不处理；将来要"刷新后仍显示检索那一行"时，再显式加列/加表即可 |
| 2 | 写入接口形态 | **`append_messages(..., display=[...])` 显式可选参数**（4.2 有完整代码示例）。对比：消息 dict 塞保留键 `_citations` 的写法把两类数据混进同一个 dict，安全依赖"每个写入点都记得剔除"，且需要额外文档与测试才能让后来者知道这条隐式规则；显式参数让"模型上下文载荷"和"展示数据"从一开始就是两个参数 |
| 3 | 引用片段正文 `content` 是否入库 | **入库**（一度评估"不入库"，2026-09-17 复核后改回）。入库让历史响应直接复用 `Citation`、前端组件零改动、刷新前后展示完全一致；不入库反而要多出后端 `ConversationCitation` + 前端 `CitationDisplay` 两套形状、`CitationList` 条件渲染与解释文案，并留下"刷新前有片段、刷新后没有"这一长期需要解释的行为差异。代价是库内多存一份片段快照（每答约 5~10KB），需要时可在展示层截断或将来做历史分页，不改契约。详见 3.1 末段 |
| 4 | 引用锚点作用域修复 | **并入本次**（5.4） |
| 5 | `(document_id, version_id)` 反查索引 | **暂不建**。本次只用会话读路径；将来做"哪些回答引用了这份文档"时再加索引与查询 |

### 9.2 可选的口径（实施时按下面建议走即可）

| # | 事项 | 备选 | 建议 |
| --- | --- | --- | --- |
| 6 | 超长证据片段是否折叠显示 | 原样展示 / 前端折叠（`max-height` + 展开） | **先原样展示**：保持与实时一致；实测发现长片段影响阅读时再做展示层折叠（不改契约、不动存储） |

---

## 10. 风险与回归边界

1. **模型上下文污染（最高优先级）**：引用只进 `message_citations`，与 `payload_json`
   在结构上就是两条路径（不像"往载荷里加键"那样依赖约定）。回归证据：
   `load_messages()` 相等断言（`tests/sessions/test_sqlite_store.py:169`、`:457`）与
   `tests/application/test_chat_service.py:184-210` 必须原样通过。
2. **历史响应体积**（存 `content` 的代价）：每条引用多一份证据片段快照——chunk 上限
   `MAX_CHUNK_TOKENS = 700`（`app/knowledge/chunking.py:47`），中文一条约 1~3KB，
   每答 3~5 条 → 每答约 5~10KB；25 答的会话约 150~250KB（历史响应本来已含全部回答正文）。
   本次接受，实施后**实测一次**；若不可接受，按 3.1 末段的顺序处理：先在展示层折叠长片段
   （不改契约）→ 再考虑历史接口窗口/分页 → 最后才是改成"不存正文、按需取"（会引入两套引用形状）。
3. **接口范围扩张**：历史接口从"纯文本"变成"文本 + 结构化引用"。守住 3.6 的五条；
   特别禁止把 `payload_json` 原样暴露（今天不是，未来也不能是）。
4. **旧数据行为不一致**：升级前的回答刷新后仍无引用。必须写进 runbook 与已知限制，
   否则会被当作缺陷上报。
5. **偏移失真风险**：历史引用必须按 `version_id` 读取。任何"顺手对齐到最新版本"的实现
   都会让偏移与正文错位——这是红线，checklist 要单独列一条。
6. **重复 DOM id**：见 5.4，必须与本次一起修，并补"两条回答各自归位"的测试。
7. **迁移风险**：`CREATE TABLE` + 一条可空 `ADD COLUMN` 都是轻量操作，不重建 `messages`；
   **降级会丢弃引用行**（消息本体不受影响），需在迁移文档与提交信息写明。
   老 SQLite（< 3.35）删列改用 `batch_alter_table(recreate="always")`，注意保留
   `uq_messages_conversation_seq` 与 `idx_messages_conversation_seq`。
8. **字段漂移**：独立表的代价是"`Citation` 加字段 = 加列 = 一次迁移"。用 7.1 第 12 条的
   契约钉子测试把两者钉死，避免"代码加了字段、库里没列、历史引用悄悄缺字段"。
9. **前端"不伪造"约定不能被改坏**：本次只放开"引用"，`events` / 审批卡片仍必须为
   `null` / 不渲染；`MessageList.spec.ts:154-165` 的用例要按新语义改写而不是删除。

---

## 11. 明确不做（本次排除，附理由）

| 事项 | 为什么不做 |
| --- | --- |
| `events` 持久化 | 事件里含工具参数与工具结果（内部载荷），回放等于把内部数据纳入安全历史，需单独的安全评审 |
| `pending_approvals` 持久化 | 审批有独立耐久记录与专门接口（审批中心）；刷新后审批卡片消失是既有已知限制，改动它属于另一个产品决策 |
| `retrieval_summary` 持久化 | Agentic Search 的产物，当前生产走传统 RAG；字段来源与承重关系见 3.4（`strategy`/`round_count` 是常量、`latency_ms` 是过程指标，安全信号已由 `answer_incomplete` 承载）。`retrieval_events` 因 `conversation_id=None` 无法回补 |
| 引用片段的**按需加载/瘦身**（历史只返回元数据，正文点击后再取） | 决策 3：片段快照随行入库，历史卡片与实时一致；只有实测体积不可接受时才考虑，且要先在展示层与分页上想办法（见 3.1 末段） |
| 历史引用回填 | 无法可靠反推 `document_id` / `version_id` / 偏移，猜测回填会产生错误定位 |
| 引用写 localStorage | 服务端是唯一可信来源；本地缓存会引入跨企业/跨用户泄漏与陈旧数据风险 |
| "旧引用对齐到最新版本" | 偏移只对生成时的版本文本有效，对齐必然失真；"政策现在怎么说"是版本对照功能 |
| 引用反查的**查询接口/页面**（哪些回答引用了某文档） | 本次只建表与读路径，不做界面与接口；表已具备条件，将来加一条按 `document_id` 的查询（必要时补索引）即可 |
