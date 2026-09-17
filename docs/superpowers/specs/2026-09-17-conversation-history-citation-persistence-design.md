# 引用随回答持久化（刷新后仍可跳转）—— 改动文档

> 来源：`docs/superpowers/specs/2026-09-09-knowledge-document-content-viewer-design.md`
> §6「后续待完成」第 1 项「**引用在会话历史中持久化**（刷新后仍能跳转）」。
>
> 本文档只做**改动范围与方向的界定**，不含代码实现；实施前请先确认第 9 节的
> 4 个决策点。

| 项 | 内容 |
| --- | --- |
| 状态 | 改动范围与方向已核实，待拍板（第 9 节）后按第 8 节拆分实施 |
| 目标 | 刷新页面 / 重新进入会话后，历史回答仍带**结构化引用**，可继续「查看原文位置」 |
| 涉及模块 | `migrations`、`app/sessions`、`app/application`、`app/schemas`、`frontend/src`（api / stores / components） |
| 数据库 | **不新增表**；需要一次迁移：`messages` 新增一列 `structured_json`（迁移 `0014`） |
| 本次不做 | `events` / 待审批卡片 / 检索摘要的持久化；历史数据回填；引用写 localStorage |
| 前置能力 | 正文按版本读取、引用偏移高亮、历史版本与停用文档提示、偏移为空降级——**均已落地**（阶段 A/B/C 完成） |

## 0. 结论速览（先回答三个问题）

| 问题 | 结论 |
| --- | --- |
| 是否要新增数据库表？ | **不需要。** 引用是「某条回答的展示快照」，与消息同生同灭；在 `messages` 上新增一列 `structured_json TEXT NULL` 即可。备选方案（独立表 `message_citations`）见 3.2，本次不采用。 |
| 是否需要迁移？ | **需要。** 迁移 `0014_message_structured_payload`：`ALTER TABLE messages ADD COLUMN structured_json TEXT NULL`。历史行保持 `NULL`，读取时按「无引用」处理，**无需回填**。 |
| 改动方向一句话 | 回答生成时把「引用 + 引用完整性标记」作为**展示用结构化数据**随消息一起落库（与 `payload_json` 严格分离，绝不进入模型上下文），历史接口把它作为新字段返回，前端在历史回答下渲染同一套 `CitationList` 与「查看原文位置」入口。 |

预期改动规模：后端 **8 个文件**（含 1 个迁移与 4 个测试文件）、前端 **6 个文件**（含 3 个测试文件）、文档 **5 个文件**。完整清单见第 6 节。

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
  （`migrations/versions/0001_baseline.py:59-87`），本轮新增列不涉及重建表。
- `load_messages()` 把 `payload_json` **原样**解析后交给 `ChatService.chat()`，
  再作为 `messages` 传给模型（`app/sessions/sqlite_store.py:262-284`、
  `app/application/chat_service.py:191-222`）。
- 因此**不能**把引用塞进 `payload_json`：那会让引用内容作为"助手自己说过的话"
  回到模型上下文里（污染上下文，且部分 OpenAI 兼容网关会拒绝未知字段）。详见 3.2 的否决理由。
- `load_message_records()` 是历史读路径，返回完整 `payload` 供应用层过滤
  （`app/sessions/sqlite_store.py:153-187`）——这是**新增列唯一需要接入的读路径**。
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
| `citations`（`citation_id` / `document_id` / `version_id` / `chunk_id` / `title` / `heading_path` / `content` / `start_offset` / `end_offset`） | **是** | 就是"可跳转"所需的最小集合；全部字段当时已经返回给同一用户，不新增数据暴露面 |
| `answer_incomplete` | **是** | 服务端确定性判定的安全提示。**漏引场景下 `citations` 可能为空而该标记为 `true`**——不持久化就会让刷新后的回答"看起来一切正常" |
| `retrieval_summary` / `events` / `pending_approvals` | 否 | 见第 11 节 |
| `raw_text`、完整 chunk 正文、向量载荷、检索 trace | **绝不** | 现有红线不变 |

存储形态（单条消息一个 JSON 对象，字段名与接口字段同名，便于前端直接复用）：

```json
{
  "citations": [
    {
      "citation_id": "C1",
      "document_id": "doc-1",
      "version_id": "ver-3",
      "chunk_id": "chunk-9",
      "title": "退货与换货政策",
      "heading_path": "2. 退货流程",
      "content": "消费者可在签收后 7 日内申请退货。",
      "start_offset": 120,
      "end_offset": 138
    }
  ],
  "answer_incomplete": false
}
```

写入时机：`citations` 非空**或** `answer_incomplete` 为 `true` 时才写；纯聊天回答
（无检索、无引用）保持 `NULL`，不给库和响应增加噪声。

> 备选（不推荐）：只存引用元数据、不存 `content`，让用户打开正文后从
> `raw_text[start:end]` 现场截取。可省约一半体积，但刷新后引用卡片**看不到证据原文**，
> 与 E-07 的验收口径冲突，收益不抵体验损失。

### 3.2 存在哪里（本题的"要不要建新表"）

**推荐：方案 A —— `messages` 新增一列 `structured_json TEXT NULL`。**

| 方案 | 做法 | 结论 |
| --- | --- | --- |
| **A（推荐）** | `messages.structured_json` 存一个 JSON 对象（3.1） | 改动最小；与消息**同事务、同生同灭**；`payload_json`（模型上下文）完全不受影响；`load_message_records` 已经是历史读路径，接一处即可 |
| B（备选） | 新表 `message_citations`，一条引用一行（9 个业务列 + `conversation_id`/`seq`/`ordinal`） | 规范化、可按 `document_id` 反查"哪些回答引用过这份文档"（文档停用/纠错时的影响面分析）；代价是多一条写路径、多一组读方法与协议、多一张表的测试，且 `Citation` 字段演进要跟着迁移。**当出现"按文档反查引用"的真实需求时再升级**（届时可从 JSON 一次性迁出） |
| C（否决） | 写进现有 `payload_json` | **会进入模型上下文**（2.2）：引用片段会成为助手历史消息的一部分被回灌给模型，既污染上下文，又可能被网关拒绝；且破坏 `load_messages` 与 `payload_json` 的一一对等关系。**不做** |

命名与既有词汇保持一致：仓库里统一把这批数据叫「结构化展示信息」，前端 store 里
也已经叫 `structured`（`frontend/src/stores/chat.ts:8-9`），因此列名取
`structured_json`、写入时承载它的临时键取 `_structured`。

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

- 升级前产生的回答**没有可恢复的引用数据**（当时就没写库），`structured_json` 为
  `NULL`，历史接口按空引用返回，前端渲染路径与今天完全一致。
- 因此本改动**只对升级后新产生的回答生效**，且这一事实必须写进验收清单
  （把 F-14-9「刷新后引用消失」改写为「升级前的历史回答仍无引用」），避免被当成 Bug 上报。
- 不做回填脚本：无法从消息正文可靠地反推出 `document_id` / `version_id` / 偏移，
  猜测回填会把"无法定位"变成"错误定位"。

### 3.6 安全边界（本次唯一扩张点，必须逐条守住）

1. **不新增数据类别**：持久化与返回的字段是"本轮响应里同一用户已经看到过的"；
2. **白名单不变**：只有 `_to_visible_message` 判定为可见的 assistant 最终回答才带引用；
   `system` / `tool` / 中间助手消息即使有 `_structured` 也一律不返回；
3. **模型上下文不变**：`_structured` 在写入前被剔除，`payload_json` 与 `load_messages()`
   的返回值与今天完全一致（现有断言不变，见 2.3）；
4. **租户隔离不变**：历史接口仍只经 `get_current_tenant` 取 `organization_id`
   并按 (企业, 用户) 双重限定；引用里的 `document_id` 是否可读，由正文接口在点击时
   再校验一次（跨租户 404）；
5. **坏数据不炸接口**：JSON 解析/校验失败时**逐条丢弃**该引用（记录诊断日志），
   不让一条脏数据把整个会话历史变成 500。

---

## 4. 后端改动方向

### 4.1 迁移 `0014_message_structured_payload`

- 文件：`migrations/versions/0014_message_structured_payload.py`，
  `down_revision = "0013_document_versions_async_ingestion"`。
- `upgrade()`：`ALTER TABLE messages ADD COLUMN structured_json TEXT NULL`
  （可空、无默认值 → SQLite 支持直接 `ADD COLUMN`，**不需要** `batch_alter_table` 重建表；
  本机 SQLite 3.39.4，`DROP COLUMN` 亦可用）。
- `downgrade()`：`op.drop_column("messages", "structured_json")`
  （若部署环境 SQLite < 3.35，改用已有的 `batch_alter_table(recreate="always")` 写法；
  `messages` 没有入向外键，重建安全，但要保留 `uq_messages_conversation_seq` 与
  `idx_messages_conversation_seq`）。
- 列注释必须写清中文语义，例如：
  `structured_json` = 「该消息的**展示用**结构化数据（JSON）：当前含 citations 与 answer_incomplete；
  与 payload_json（模型上下文载荷）严格分离，永不进入模型上下文」。
- 迁移文件顶部按仓库惯例写中文说明文档字符串（为什么加这一列、为什么与 `payload_json` 分开、
  为什么不建新表）。

### 4.2 会话存储（`app/sessions`）

| 位置 | 改动 |
| --- | --- |
| `base.py` `MessageRecord` | 新增字段 `structured : dict \| None = None`（带中文注释：展示用结构化数据，仅供历史过滤使用，禁止进入模型上下文）。带默认值 → 现有构造点只有 `sqlite_store.py:180` 一处，安全 |
| `base.py` `SessionStore` 协议 | 新增写路径形参（见下）与文档说明 |
| `sqlite_store.py` `load_message_records` | `SELECT` 增加 `structured_json`，`json.loads` 后填入 `structured`（为 `NULL` 时填 `None`；解析失败按 `None` 处理并留诊断日志） |
| `sqlite_store.py` `append_messages` | 新增可选形参 `structured : list[dict \| None] \| None = None`，长度必须与 `messages` 一致（不一致直接 `ValueError`，不要静默错位）；`INSERT` 语句增加该列 |
| `sqlite_store.py` `load_messages` | **不改**（模型上下文路径一行不动） |

写入接口的两种候选（第 9 节决策点 2）：

- **推荐**：消息字典用保留键 `_structured` 承载展示数据；`append_messages` 在
  `json.dumps` 之前**剔除所有下划线开头的键**（保证 `payload_json` 只装模型上下文载荷），
  同时把 `_structured` 写进新列。优点：不需要两个平行数组对齐，不会错位；
- 备选：`append_messages(..., structured=[None, None, {...}])` 平行数组。缺点：长度校验靠约定，
  错位风险高。

无论选哪种，都要有测试断言：**`payload_json` 里不出现 `_structured` / `citations`**。

### 4.3 应用层（`app/application/chat_service.py`）

1. `chat()`：在 `append_messages` 之前，把本轮的展示数据挂到**最终 assistant 消息**上。
   识别方式：从 `messages[new_messages_start:]` 末尾向前找第一条
   `role == "assistant"` 且无 `tool_calls` 的消息（`runner.py:219-248` 保证它是本轮最后一条）；
   找不到（理论上不会发生）时不挂、不报错。
   ```python
   response = self._run_agent(messages=messages, context=invocation)
   structured = _build_structured(response)   # citations + answer_incomplete；两者皆空返回 None
   if structured is not None:
       _attach_structured(messages[new_messages_start:], structured)
   ```
2. `_to_visible_message()`：新增 `citations` / `answer_incomplete` 两个返回字段。
   - `user` 消息恒为 `[]` / `False`；
   - assistant 最终回答从 `stored.structured` 里取，**逐条校验**：合法条目转成
     `Citation`，非法条目丢弃（不抛异常）；
   - `structured` 为 `None`（升级前的历史消息）时与今天输出一致。
3. 不改动：`get_history()` 的归属校验与顺序、`_store.load_messages` 的调用、锁与幂等逻辑。

### 4.4 响应契约（`app/schemas/chat.py`）

```python
class ConversationHistoryMessage(BaseModel):
  # ……现有字段不变……
  citations : list[Citation] = Field(default_factory=list)  # 该回答的知识引用（保持 C1..Cn 原序）；用户消息与无引用回答为空数组
  answer_incomplete : bool = False  # 该回答生成时的引用完整性标记；为 True 时界面须给出「谨慎采用」提示
```

- 复用 `app/knowledge/results.py` 的 `Citation`（不再定义第二套引用模型），
  字段含义与附录 A.8 完全一致；Pydantic v2 默认忽略未知字段，
  未来 `Citation` 增字段时旧数据不会被判为非法。
- 默认值保证：历史接口对任何"没有展示数据"的消息都返回 `[]` / `false`，
  前端无需处理字段缺失。

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
  /** 该回答的知识引用（结构化字段，禁止从回答文本解析）；无引用时为空数组 */
  citations: Citation[]
  /** 该回答生成时的引用完整性标记；true 时回答上方显示「谨慎采用」提示 */
  answer_incomplete: boolean
}
```

`fetchDocumentContent`（正文接口）与 `Citation` 类型**无需改动**：历史引用复用的就是
`CitationTarget` 跳转载荷（`frontend/src/api/types.ts:267-284`）。

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
- 实时分支保持不变（`structured` 仍负责处理过程、检索摘要、待审批）。
- `CitationList` 与 `ChatView.onOpenDocument` 无需逻辑改动。

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

### 6.1 后端（8 个文件）

| 文件 | 改动 |
| --- | --- |
| `migrations/versions/0014_message_structured_payload.py` | **新增**：`messages.structured_json` 列（含中文迁移说明与降级） |
| `app/sessions/base.py` | `MessageRecord` 增 `structured` 字段；`append_messages` 协议增写入形参（均带中文注释） |
| `app/sessions/sqlite_store.py` | 写入侧：剔除 `_structured` 后写 `payload_json`，并写新列；读取侧 `load_message_records` 带出新列 |
| `app/application/chat_service.py` | 写入侧：把引用挂到最终 assistant 消息；读取侧：`_to_visible_message` 输出引用与完整性标记 |
| `app/schemas/chat.py` | `ConversationHistoryMessage` 增 `citations` / `answer_incomplete` |
| `tests/db/test_migrations.py` | **新增** 0014 迁移用例（列存在、旧行为 NULL、降级删列、往返升级） |
| `tests/sessions/test_sqlite_store.py` | 写入/读取/剔除 `_structured`、`load_messages` 不含展示数据、无展示数据为 NULL |
| `tests/application/test_chat_service.py`、`tests/api/test_router.py` | 历史带引用（真实 SQLite 走一遍 chat → 历史）、空引用与中间消息不带引用、防泄漏回归 |

> `app/agent/runner.py`、`app/knowledge/**`、`app/api/router.py` **不改**（见 4.5）。

### 6.2 前端（6 个文件）

| 文件 | 改动 |
| --- | --- |
| `src/api/types.ts` | `ConversationHistoryMessage` 增 `citations` / `answer_incomplete` |
| `src/stores/chat.ts` | `ChatMessageView` 增 `structuredAnswer`；`historyToView` / `sendMessage` 填充；更新头部注释 |
| `src/components/chat/MessageList.vue` | 历史回答渲染引用卡片与完整性提示；传入引用锚点前缀 |
| `src/components/chat/CitationList.vue` | 新增 `anchorPrefix` prop，卡片 id 加作用域前缀 |
| `src/components/common/MarkdownContent.vue` + `src/components/chat/AssistantAnswer.vue` | `[C1]` 定位使用同一作用域前缀（透传 prop） |
| `src/stores/__tests__/chat.spec.ts`、`src/components/chat/__tests__/MessageList.spec.ts`、`src/components/common/__tests__/MarkdownContent.spec.ts`、`src/components/chat/__tests__/CitationList.spec.ts` | 历史引用映射与渲染、旧响应兜底、"两条回答的 [C1] 各自归位" |

### 6.3 文档（5 个文件）

| 文件 | 改动 |
| --- | --- |
| `docs/frontend/api-inventory.md` | 4.3.4（`:505-533`）改写：历史接口现在返回 `citations` 与 `answer_incomplete`，并说明 events/审批仍不保存；附录 A.5（`:1119-1130`）字段表补两行；§5.3（`:965-983`）"刷新后只恢复问答文本"的口径更新；头部增量记录加一条 |
| `docs/frontend/manual-test-runbook.md` | E-09（`:525-531`）改为"刷新后引用卡片仍在、可继续跳转"；F-14-9（`:785-791`）改写为"升级前的历史回答仍无引用"；第 15 条已知限制（`:1067`）同步；新增"历史引用跳转 + 停用文档 + 跨租户"验收项 |
| `README.md` | 能力清单（`:60-64`）补"引用随回答持久化，刷新后仍可核对来源"；已知限制（`:77-78`）移除该项 |
| `docs/superpowers/specs/2026-09-09-knowledge-document-content-viewer-design.md` | §6 第 1 项标记"已实施（见本文档）"；§3 决策 6 的口径更新 |
| `docs/database-migrations.md` | 第 7 节验收命令补 `tests/db/test_migrations.py` 相关说明（如无变化则仅确认） |

---

## 7. 测试与验收

### 7.1 后端验收（每条一个测试，测试方法必须有**中文注释**）

| # | 断言 | 位置 |
| --- | --- | --- |
| 1 | 迁移后 `messages` 有 `structured_json` 列；旧行该列为 `NULL`；降级后列消失且消息数据不丢 | `tests/db/test_migrations.py` |
| 2 | `append_messages` 带展示数据时：`structured_json` 有值，且 `payload_json` **不含** `_structured` | `tests/sessions/test_sqlite_store.py` |
| 3 | `load_messages()` 返回值与调用方传入的模型载荷**逐字相等**（模型上下文零变化） | `tests/sessions/test_sqlite_store.py` |
| 4 | `load_message_records()` 把展示数据带出；无展示数据时为 `None` | `tests/sessions/test_sqlite_store.py` |
| 5 | 走完 `chat()`（真实 SQLite + 假 runner 返回带引用回答）后，`get_history()` 的对应 assistant 消息带**完整引用**与偏移 | `tests/application/test_chat_service.py` |
| 6 | 用户消息恒为空引用；带 `tool_calls` 的中间助手消息、`system`/`tool` 消息即使有展示数据也**不返回** | `tests/application/test_chat_service.py` |
| 7 | 展示数据损坏（非法 JSON / 缺字段）时：历史接口仍 200，坏条目被丢弃（不炸接口） | `tests/sessions/test_sqlite_store.py` 或 `tests/application/test_chat_service.py` |
| 8 | 真实 SQLite 迷你应用：`GET /conversations/{id}/messages/` 响应含新字段且值与落库一致 | `tests/api/test_router.py` |
| 9 | **防泄漏回归**：历史响应不得出现 `reasoning_content` / `tool_calls` / 工具参数与结果 / `raw_text` | `tests/api/test_router.py` |
| 10 | OpenAPI 字段集合与真实响应一致（现有用例自动覆盖新字段，不得放宽） | `tests/api/test_router.py:273-292` |
| 11 | 跨租户/跨用户读历史仍 404（不回归） | `tests/api/test_router.py:206-212` |

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
| 4 | 历史回答 `answer_incomplete=true` → 显示「谨慎采用」提示；`false` → 不显示 |
| 5 | 同一页面两条回答都含 `C1`：点击各自正文里的 `[C1]` 只滚动到**自己**的引用卡片（锚点作用域） |
| 6 | 现有历史消息安全用例按新语义更新（"没有结构化数据就不渲染"，而不是"永不渲染"），**不得**留下跳过或删除的用例 |

命令：

```powershell
cd frontend
npm run typecheck
npm test
```

### 7.3 手工验收（写进 `manual-test-runbook.md`）

1. 拿一条带引用的回答 → **F5 刷新** → 引用卡片与「查看原文位置」入口仍在，点击可高亮定位；
2. 用旧版本引用刷新 → 仍提示「该引用来自历史版本 vN，当前有效版本为 vM」；
3. 停用来源文档后刷新 → 引用仍可核对来源，界面提示文档已停用；
4. 用企业 B 的账号读企业 A 的会话 → 404（会话不可见）；即便手工拿到引用，正文接口仍 404；
5. 升级**之前**产生的历史回答 → 仍无引用卡片（预期行为，不是 Bug）；
6. 断网/后端不可用时点历史引用 → 面板内报错并可重试，对话区不受影响。

---

## 8. 任务拆分与顺序

> 通用验收要求（沿用设计稿 §5）：只改本任务列出的文件（跨任务改动在本文档登记）；
> 自带测试且新增/修改的测试方法有中文注释；`pytest` / `vitest` 全绿、`npm run typecheck` 通过；
> **每个任务单独提交，commit message 用中文并写明"任务编号 + 验收命令"**（`AGENTS.md` §2/§3）。
> 动手前先 `git status` 确认工作区干净。

| 阶段 | 任务 | 内容 | 依赖 |
| --- | --- | --- | --- |
| A | A1 迁移与会话存储 | 迁移 0014 + `sessions` 读写 + 存储层测试 | 无 |
| A | A2 应用层与响应契约 | `chat_service` 写入/读取 + `schemas.chat` + 服务层与接口测试（含防泄漏） | A1 |
| A | A3 接口文档同步 | `api-inventory.md` 4.3.4 / A.5 / §5.3 + 头部增量 | A2（契约冻结） |
| B | B1 类型与 store | `api/types.ts`、`stores/chat.ts` + store 测试 | A2 |
| B | B2 渲染与锚点修复 | `MessageList`、`CitationList`、`MarkdownContent`、`AssistantAnswer` + 组件测试 | B1 |
| C | C1 验收与文档收口 | runbook、README、2026-09-09 设计稿 §6、迁移文档 | B2 |

建议先做 A 阶段并冻结契约，再做前端：A 完成后即可用 curl/手工构造历史响应验证前端渲染。

---

## 9. 待拍板决策点

| # | 决策 | 备选 | 建议 |
| --- | --- | --- | --- |
| 1 | 是否只持久化引用，还是连检索摘要一起 | 只存 `citations + answer_incomplete` / 再加 `retrieval_summary` | **只存前两项**（传统 RAG 下的摘要来源与信息量见 3.4：`strategy`/`round_count` 是常量、`latency_ms` 是过程指标，安全信号已由 `answer_incomplete` 承载；若产品要求刷新后仍显示检索那一行，再往同一 JSON 加字段，纯增量、不用再迁移） |
| 2 | 写入接口形态 | 消息字典保留键 `_structured`（写入时剔除） / `append_messages` 平行数组参数 | **保留键**：不存在两条列表错位的可能，且"下划线键不进 `payload_json`"是一条可测试的硬规则 |
| 3 | 引用正文片段是否入库 | 存 `content` / 只存元数据 | **存 `content`**：刷新后仍要能直接核对证据原文（与 E-07 口径一致）；体积影响见第 10 节第 2 条 |
| 4 | 引用锚点作用域修复是否并入本次 | 并入 / 另开任务 | **并入**：不做就会产生"点 [C1] 跳到别的回答"的可复现错误，属于本改动的直接后果 |

---

## 10. 风险与回归边界

1. **模型上下文污染（最高优先级）**：引用只能进新列，绝不能进 `payload_json`。
   回归证据：`load_messages()` 相等断言（`tests/sessions/test_sqlite_store.py:169`、
   `:457`）与 `tests/application/test_chat_service.py:184-210` 必须原样通过。
2. **历史响应体积**：每条引用含 `content`（chunk 上限 `MAX_CHUNK_TOKENS = 700`，
   `app/knowledge/chunking.py:47`）。一条回答 3–5 条引用 ≈ 2–4 千字；25 轮问答的会话
   约增加 5–10 万字符。本次接受（历史本来就一次返回全部消息正文），
   但实施后要**实测一次**；若不可接受，备选是历史只返回引用元数据、正文按需再取
   （属降级方案，需重新评估 E-07 口径）。
3. **接口范围扩张**：历史接口从"纯文本"变成"文本 + 结构化引用"。守住 3.6 的五条；
   特别禁止把 `payload_json` 原样暴露（今天不是，未来也不能是）。
4. **旧数据行为不一致**：升级前的回答刷新后仍无引用。必须写进 runbook 与已知限制，
   否则会被当作缺陷上报。
5. **偏移失真风险**：历史引用必须按 `version_id` 读取。任何"顺手对齐到最新版本"的实现
   都会让偏移与正文错位——这是红线，checklist 要单独列一条。
6. **重复 DOM id**：见 5.4，必须与本次一起修，并补"两条回答各自归位"的测试。
7. **迁移风险**：纯 `ADD COLUMN`（可空、无默认）为轻量操作；降级删列会丢展示数据
   （消息本体不受影响），需在迁移文档写明。老 SQLite（< 3.35）改用 `batch_alter_table`
   重建表，注意保留唯一约束与索引。
8. **前端"不伪造"约定不能被改坏**：本次只放开"引用"，`events` / 审批卡片仍必须为
   `null` / 不渲染；`MessageList.spec.ts:154-165` 的用例要按新语义改写而不是删除。

---

## 11. 明确不做（本次排除，附理由）

| 事项 | 为什么不做 |
| --- | --- |
| `events` 持久化 | 事件里含工具参数与工具结果（内部载荷），回放等于把内部数据纳入安全历史，需单独的安全评审 |
| `pending_approvals` 持久化 | 审批有独立耐久记录与专门接口（审批中心）；刷新后审批卡片消失是既有已知限制，改动它属于另一个产品决策 |
| `retrieval_summary` 持久化 | 过程指标，刷新后缺失不影响"核对来源"这一目标；同一 JSON 可增量扩展；另注：它的字段来源与承重关系见 3.4，`retrieval_events` 因 `conversation_id=None` 无法回补 |
| 历史引用回填 | 无法可靠反推 `document_id` / `version_id` / 偏移，猜测回填会产生错误定位 |
| 引用写 localStorage | 服务端是唯一可信来源；本地缓存会引入跨企业/跨用户泄漏与陈旧数据风险 |
| "旧引用对齐到最新版本" | 偏移只对生成时的版本文本有效，对齐必然失真；"政策现在怎么说"是版本对照功能 |
| 引用统计 / 反查（哪些回答引用了某文档） | 需要方案 B（独立表），等真实需求出现再升级 |
