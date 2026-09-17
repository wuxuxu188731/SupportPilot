# 知识库正文查看与引用跳转 —— 正式设计稿

> **后续变更（2026-09-16，产品决定）**：查看器内「正文为入库时由 PDF/Word 自动转换的
> Markdown，与原文排版可能不一致」提示已**移除**（客服对话页「查看原文」与知识库详情页
> 「查看正文」共用同一组件）。本文档 §2/§3/§4 中要求该提示上屏的条目以
> `docs/frontend/api-inventory.md` 4.4.8 与 `docs/frontend/manual-test-runbook.md` F-14-4
> 的最新口径为准；接口、`source_type` 字段与正文渲染方式均不变。

| 项 | 内容 |
| --- | --- |
| 状态 | 已评审，待实施（决策点已由项目负责人确认） |
| 范围 | 知识库正文查看（Markdown）+ Agent 引用跳转定位 |
| 涉及模块 | `app/knowledge`、`app/api/knowledge_router.py`、`app/schemas/knowledge.py`、`frontend/src`（chat / knowledge） |
| 数据库 | **无迁移、无表结构变更**（全部为读路径；所需列已存在） |
| 前置文档 | 本目录 `document-content-viewer-design.md`（可行性论证，事实与源码定位均在其中） |
| 待办 | 本次任务清单见 §5；后续 backlog 见 §6 |

---

## 1. 背景与目标

用户上传 `.md` / `.markdown` / `.txt` / `.docx` / `.pdf` 后：

1. 前端可以查看**文档正文**；
2. Agent 回答引用知识库（结构化 `citations` 的 `C1..Cn`）时，可以从引用**跳转到正文中对应位置**；
3. 正文**只渲染入库时已转换好的 Markdown**，不渲染 PDF / Word 原文——因为切块、向量、引用
   全部基于这份 Markdown（`document_versions.raw_text`），引用跳转天然指向它。

非目标（本次不做）：PDF 原文渲染、原文排版还原、原文文件下载。
（原文此处还列了"引用在会话历史中的持久化"——该项已于 **2026-09-17** 单独实施，
见 `2026-09-17-conversation-history-citation-persistence-design.md` 与 §6 第 1 项。）

## 2. 关键前提（已核实，实施时不得推翻）

| 前提 | 事实 | 依据 |
| --- | --- | --- |
| 正文已落库 | `document_versions.raw_text` = 归一化 Markdown，仅做 `\r\n|\r → \n`，**无裁剪** | `document_loader.py:144,176`；`ingestion.py:469-477`；`sqlite_store.py:87,359-377,565-581` |
| 切块位置已落库 | `document_chunks.start_offset/end_offset` 是**字符偏移**，契约 `content == raw_text[start:end]` | `chunking.py:12-13,169-184`；迁移 `0009:137-158` |
| 块之间有重叠 | `CHUNK_OVERLAP_TOKENS = 80`，相邻块正文重叠，因此"定位"是一个**区间**而不是唯一点 | `chunking.py:48,157-165` |
| 读取能力已有 | `SQLiteKnowledgeStore.get_version_by_id` 已返回带 `raw_text` 的 `DocumentVersion`，且按 `(organization_id, document_id, version_id)` 三重限定 | `sqlite_store.py:244-277` |
| 未解析版本可能无正文 | 异步入库下 `raw_text` 可为 `NULL`；`docs/knowledge-loader-remaining-tasks.md` §9 要求所有新读路径处理 `None` | `base.py:56-60`；迁移 `0013` |
| 详情接口的现状约束 | `DocumentVersionResponse` **永不带 `raw_text`**，且已有防泄漏测试断言 | `app/schemas/knowledge.py` 头部注释；`tests/api/test_knowledge_router.py:382,408-409` |

## 3. 已确认的产品决策

| # | 决策 | 结论 |
| --- | --- | --- |
| 1 | 正文读取权限 | **所有成员可读；必须严格租户隔离**（A 企业成员读不到 B 企业文档，行为与现有 404 口径一致：不区分"不存在"与"跨租户"） |
| 2 | Citation 偏移 | **允许为空**（`int \| None`）。偏移为空时**降级为"只定位到文档"**，不阻断打开 |
| 3 | 历史版本 | **允许读取非 active 版本正文**，界面显著提示"该引用来自历史版本 vN，当前有效版本 vM" |
| 4 | 跳转形态 | **对话区右侧的内嵌正文面板**（对话区与面板同屏并排，类似 Codex 的右侧内容窗），不是全屏遮罩抽屉；窄屏降级为整页跳转 |
| 5 | 正文分页 | 本次**返回全文**；接口预留窗口参数位，实测后决定是否启用（见 §6 backlog） |
| 6 | 刷新后引用 | ~~**本次不管**：聊天历史仍不保存 citations（列为后续项）~~ **已于 2026-09-17 实施**：历史接口现在返回 `citations` 与 `answer_incomplete`（与实时响应同为 `Citation`，含证据片段快照 `content`），且引用按**生成时冻结的 `version_id`** 原样解释、绝不重新对齐到当前有效版本；见 `2026-09-17-conversation-history-citation-persistence-design.md` |
| 7 | 原文下载 | 本次**不提供**（列为后续项） |

## 4. 详细设计

### 4.1 后端契约：正文读取接口

```text
GET /knowledge/documents/{document_id}/versions/{version_id}/content/
权限：企业成员（admin / agent 均可读），无写副作用
```

**成功 200**（`DocumentContentResponse`）：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `document_id` | string | 文档标识 |
| `version_id` | string | 版本标识（本次读取的确切版本） |
| `version_no` | int | 版本序号，用于"历史版本"提示文案 |
| `title` | string | 文档标题（来自 `documents`，服务端可信来源） |
| `source_type` | string | `markdown` / `text` / `word` / `pdf`；前端据此提示"已由 PDF/Word 转换为 Markdown" |
| `status` | string | 文档当前状态（`active` / `disabled` / `processing` / `failed`），前端决定是否显示"该文档已停用" |
| `active_version_id` | string \| null | 文档当前有效版本；与 `version_id` 不同即"历史版本" |
| `loader_version` | string | 解析器版本（可追溯性） |
| `chunker_version` | string | 分块器版本（偏移语义所属版本，可追溯性） |
| `content_hash` | string \| null | 该版本归一化正文的 sha256 |
| `text` | string | 归一化 Markdown 正文（即 `raw_text`） |
| `text_length` | int | 正文字符数，前端做偏移越界自检的基准 |
| `outline` | array | 标题目录：`{level: int, title: string, heading_path: string, char_offset: int}`，可为空数组 |

**错误口径**（沿用 `KnowledgeError` 的稳定 code + safe message，不泄露跨租户信息）：

| 情形 | 状态码 | code |
| --- | --- | --- |
| 文档不存在 / 跨租户 / `version_id` 不属于该文档 | 404 | `DOCUMENT_NOT_FOUND` |
| 版本存在但尚未解析（`raw_text IS NULL`） | 404 | `DOCUMENT_NOT_FOUND`（不暴露"存在但未解析"，也不返回空正文） |
| 文档被停用（`disabled`） | **200**（正常返回正文，引用可能来自停用前后的文档） | — |
| 存储/DB 异常 | 503 | 沿用 `_STATUS_BY_CODE_DEFAULT` |

**实现要点**：

- 复用现有 `knowledge_store.get_version_by_id(...)`，**不新增 store 方法**；它的 SQL 已经是
  `WHERE organization_id = ? AND document_id = ? AND id = ?`，天然满足决策 1 的隔离要求。
- `outline` 由 `KnowledgeChunker` 的伴生逻辑之外单独实现**行级扫描**（复用
  `document_loader._extract_markdown_sections` 的同一套 `_HEADING_RE` 规则，避免两套标题解析），
  `char_offset` = 该标题所在行的起始字符偏移（`text` 中的绝对偏移）。
- `text_length` 用 `len(text)`（Python 字符数），**不是** `utf-8` 字节数；前端一切偏移判断都以字符为单位。
- 响应体大小：上限来自上传字节 `MAX_DOCUMENT_BYTES = 2 MiB`，纯中文时约 70 万字符。
  本次接受该体积（决策 5）；前端必须避免对超长正文做多次全量重排（见 4.4）。

### 4.2 后端契约：把偏移贯通到 Citation

`Citation` 新增两个字段（`app/knowledge/results.py`）：

```python
start_offset: int | None = None   # 引用片段在版本正文中的起始字符偏移；None 表示无法定位
end_offset: int | None = None     # 结束字符偏移（不含），满足 text[start:end] == content
```

数据链路（**三处构造必须同时改**，否则 `runner.py` 的 `Citation(**dict)` 会 `TypeError`）：

```text
document_chunks.start_offset/end_offset
  → ChunkWithDocumentTitle（base.py，新增两字段）
  → resolve_active_citations（sqlite_store.py `SELECT c.*` 已含两列，只需 _to_chunk_with_title 映射）
  → retrieval._build_citations（Baseline） / service._citations（Adaptive）
  → Citation（results.py）→ public_dict() → search_knowledge 工具结果
  → runner.validate_final_citations（从 dict 重建 Citation）
  → LLMResponse.citations（HTTP）
```

**默认值取 `None` 是刻意的**：既保证"偏移缺失 = 正常降级路径"（决策 2），也让现有
手工构造 `Citation` 的测试（`tests/evals/stage_c/*`、`tests/knowledge/test_results.py`）无需改动。

**服务端自检**：构造 citation 时校验 `chunk.content == raw_text[start_offset:end_offset]` 的成本过高
（需要读正文），改为在**前端**做自检（4.5）；后端只保证两者同源。

### 4.3 前端契约：类型与 API

```ts
// src/api/knowledgeTypes.ts
export interface DocumentContentOutlineItem {
  level: number          // 标题层级（1-6）
  title: string          // 标题文本
  heading_path: string   // 完整标题路径（如「3/3.2 退货流程」）
  char_offset: number    // 该标题行在正文中的起始字符偏移
}

export interface DocumentContentResponse {
  document_id: string
  version_id: string
  version_no: number
  title: string
  source_type: DocumentSourceTypeValue
  status: DocumentStatusValue
  active_version_id: string | null
  loader_version: string
  chunker_version: string
  content_hash: string | null
  text: string
  text_length: number
  outline: DocumentContentOutlineItem[]
}

// src/api/knowledge.ts
export async function getDocumentVersionContent(
  documentId: string,
  versionId: string,
): Promise<DocumentContentResponse>   // 使用默认 60s 超时；可读操作允许重试
```

```ts
// src/api/types.ts —— Citation 增加两个可空字段
start_offset: number | null   // 引用片段在正文中的起始字符偏移；null 表示无法精确定位
end_offset: number | null     // 结束字符偏移（不含）
```

**事实约束**：`start_offset`/`end_offset` **不做**向后兼容填充。旧会话中已产生的引用结构（若前端
某处仍持有）不含这两个字段，读出来就是 `undefined`——前端一律用 `== null` 判断降级，
不得用 `!start_offset`（偏移 0 是合法值）。

### 4.4 前端布局：对话区右侧的内嵌正文面板

在 `ChatView.vue` 的 `.chat-page`（已是 `display:flex`，`height: calc(100vh - 60px - 48px)`）中，
把正文面板作为**第三个 flex 兄弟**插入 `.chat-main` 右侧：

```text
┌────────────┬──────────────────────────┬───────────────────────┐
│ 会话列表    │ 主对话区 .chat-main       │ 正文面板 .chat-doc     │
│ .chat-side │ flex:1; min-width:0      │ 宽 420px; flex-shrink:0│
│ 280px      │                          │ 可关闭 / 可折叠        │
└────────────┴──────────────────────────┴───────────────────────┘
```

布局规则：

- 面板**不是** `n-drawer`（`n-drawer` 是覆盖式遮罩，会盖住对话）；用普通 flex 列 +
  自绘头部，避免遮罩与焦点陷阱。
- `.chat-main` 已有 `min-width: 0`，因此面板出现后对话区自动收窄，不需要额外重排逻辑。
- 面板宽度固定 `420px`（`clamp(360px, 32vw, 520px)`）；桌面 ≥ 1280px 才允许同屏并排。
- **窄屏（< 1280px）降级**：不做挤压式并排（会把对话压到不可用），改为路由跳转到
  `/app/knowledge/:documentId?versionId=&chunkId=&start=&end=`（该路由已存在），
  复用同一个查看器组件，并提供"返回对话"按钮。
- 面板关闭不改变会话路由，不触发历史重载；面板状态（开/关、当前文档）保存在
  ChatView 局部状态，**不写入 localStorage**，企业切换时随 `tenantReset` 关闭。
- 打开面板时**不滚动对话区**（保持用户阅读位置）；面板内部自己滚动到高亮区间。

### 4.5 前端：偏移 → DOM 定位与高亮

核心问题：`raw_text` 的第 `start..end` 个字符，在 markdown-it 渲染后的 DOM 中落在哪里。

**主方案（精确）**：渲染完成后建立"源码字符偏移 ↔ DOM 文本节点"索引。

1. 遍历容器内所有文本节点，累计已见字符数，得到 `charOffset → (node, offsetInNode)` 的单调分段；
2. 用 `document.createRange()` 精确框出 `[start, end)`；
3. 高亮：按文本节点分段，对每段单独 `splitText` + 包裹 `<span class="chunk-highlight">`；
   **不整段替换 innerHTML**，避免破坏已有引用标记等结构；
4. 定位：`span.scrollIntoView({ block: 'center', behavior: 'smooth' })`，并给容器补
   `padding-bottom` 余量，避免高亮落在视口最底部；
5. 高亮常驻直到用户关闭面板或切换引用（不自动淡出，避免"跳过去就找不到"）。

**渲染差异处理**：markdown-it 输出与源码的差异只有两类——实体编码（`&`→`&amp;`）与标记剥离
（`**`、`` ` ``、列表符号等）。索引构建按**渲染后文本**累计，映射时以源码偏移为准逐字符对齐；
遇到无法对齐的情形（罕见：偏移落在被剥离的标记内部）转入降级路径。

**降级路径（按顺序尝试）**：

| 条件 | 行为 |
| --- | --- |
| `start_offset`/`end_offset` 为 `null`（决策 2） | 只打开文档，滚动到顶部，提示"该引用未记录精确位置，已为你打开来源文档" |
| 偏移越界（`start < 0`、`end > text_length`、`start >= end`） | 同上一行，并在控制台留诊断日志（不上屏） |
| 主方案定位失败 | 高亮包含该偏移的**块级元素**（借 markdown-it `token.map` 行号），至少滚动到位 |
| 引用的 `heading_path` 可用但偏移不可用 | 优先滚动到 `outline` 中匹配的标题 |

**性能要求**：正文最长约 70 万字符；高亮定位只允许一次 DOM 遍历 + 一次 Range 构造，
不得在滚动/窗口 resize 时重复全量遍历（高亮结果缓存，直到切换引用或重载正文）。

### 4.6 前端组件划分

| 组件 / 模块 | 职责 | 依赖 |
| --- | --- | --- |
| `components/knowledge/DocumentContentViewer.vue` | 纯展示：加载正文（loading/error/empty）、Markdown 渲染、目录、来源转换提示、历史版本提示、偏移高亮与滚动。**不关心自己出现在面板还是整页** | `utils/markdownRange.ts`、`api/knowledge.ts` |
| `utils/markdownRange.ts` | 纯函数：`locateRangeByOffsets(container, start, end)` / `clearHighlight(container)` / `charOffsetToLine`。可脱离 Vue 单测 | 无 |
| `components/chat/DocumentContentPanel.vue` | 面板外壳：宽度、头部（文档标题、版本、关闭按钮）、把 `documentId/versionId/offsets` 透传给查看器 | `DocumentContentViewer.vue` |
| `components/chat/CitationList.vue` | 新增"查看原文位置"入口，emit `openDocument`（携带 `documentId/versionId/start/end/heading_path/title`） | 无 |
| `views/ChatView.vue` | 布局与状态：面板显隐、当前定位目标、窄屏降级跳转 | 上述组件 |
| `views/KnowledgeDetailView.vue` | 文档详情页新增"查看正文"入口（打开同一查看器），替换现有"正文暂不提供"说明（`:271-274`） | `DocumentContentViewer.vue` |

### 4.7 状态、缓存与错误处理

- 面板内同一 `(document_id, version_id)` 只请求一次；切换引用到**同一版本**只重新定位不重新请求；
  切换文档或版本重新请求。缓存范围为**内存 + 会话内**，不落 localStorage。
- 正文请求失败：面板内展示错误 + 重试按钮，**不影响对话区**（引用卡片仍在）。
- 404（文档/版本不可访问）：提示"来源文档不存在或已不可访问"，保留引用正文片段。
- 文档 `processing` 或版本未解析：本次统一表现为 404；前端文案为
  "该文档尚未完成入库，暂时无法查看正文"。
- 企业切换 / 退出登录：面板关闭、缓存清空（挂到现有 `tenantReset` 机制），在途请求按
  `requestEpoch` 丢弃。

### 4.8 文案要求（必须上屏的事实）

1. **来源类型**：`word` / `pdf` 文档必须显示
   「正文为入库时由 PDF/Word 自动转换的 Markdown，与原文排版可能不一致」。
2. **历史版本**：引用版本 ≠ 当前有效版本时显示
   「该引用来自历史版本 vN，当前有效版本为 vM」。
3. **无法精确定位**：偏移为空时显示「该引用未记录精确位置，已为你打开来源文档」。
4. **停用文档**：`status = disabled` 时显示「该文档已停用，不再参与新的知识检索」。

## 5. 本次任务清单（可独立验收）

> 通用验收要求（每个任务都适用）：
> 1. 只改本任务列出的文件，跨任务改动需在设计稿里登记；
> 2. 自带测试（后端 pytest / 前端 vitest），**新增或修改的测试方法必须有中文注释**；
> 3. `pytest`/`vitest` 全绿，`npm run typecheck` 通过；
> 4. 每个任务单独提交，commit message 使用中文（遵循 `AGENTS.md`），
>    并在提交信息里写明"任务编号 + 验收命令"。

### 阶段 A：后端（A1 → A2 → A3 顺序执行；A4 可与 A1-A3 并行；A5 依赖 A3）

#### 任务 A1 —— `ChunkWithDocumentTitle` 承载偏移

- 改动：`app/knowledge/base.py`（新增 `start_offset`/`end_offset` 字段 + 中文注释）、
  `app/knowledge/sqlite_store.py:111-123`（`_to_chunk_with_title` 映射已存在的两列）。
- 验收命令：`pytest tests/knowledge/test_knowledge_store.py -q`
- 验收标准：
  1. `resolve_active_citations` 返回的每个 chunk 都带非空 `start_offset`/`end_offset`，
     且 `content == raw_text[start:end]` 在测试里被断言（用真实临时 SQLite，按现有
     `tmp_path` fixture 风格）；
  2. 跨租户候选仍然被过滤（现有断言不回归）。

#### 任务 A2 —— 偏移贯通到 `Citation`

- 改动：`app/knowledge/results.py`（新增两个 `int | None = None` 字段 + 中文注释）、
  `app/knowledge/retrieval.py:377-392`、`app/knowledge/service.py:446-459`。
- 验收命令：`pytest tests/knowledge/test_retrieval.py tests/knowledge/test_adaptive_service.py tests/knowledge/test_results.py tests/agent -q`
- 验收标准：
  1. Baseline 与 Adaptive 两条路径产出的 citation 都带正确偏移；
  2. `runner.validate_final_citations` 从 dict 重建 `Citation` 不报错，且偏移保留（新增回归测试）；
  3. `public_dict()` 输出包含两个新字段；
  4. **不修改** `tests/evals/stage_c/*`（证明默认值兜住了手工构造）。

#### 任务 A3 —— 正文读取接口

- 改动：`app/schemas/knowledge.py`（新增 `DocumentContentOutlineItem`、`DocumentContentResponse`）、
  `app/api/knowledge_router.py`（新增路由）、必要时在 `app/knowledge/` 下新增一个
  `outline` 提取函数（复用 `document_loader` 的 `_HEADING_RE` 规则）。
- 验收命令：`pytest tests/api/test_knowledge_router.py -q`
- 验收标准（每条一个测试，中文注释）：
  1. 成员（agent）读到 200 + 完整字段，`text`/`text_length`/`outline` 与草稿数据一致；
  2. **跨租户 404**：B 企业的 document_id 或 version_id 一律 `DOCUMENT_NOT_FOUND`，响应体不含
     标题、正文等任何跨租户信息；
  3. `version_id` 属于别的文档 → 404；
  4. 未解析版本（`raw_text IS NULL`）→ 404，不返回空正文；
  5. `status = disabled` 的文档仍可读（200）；
  6. 历史（非 active）版本可读，响应里 `active_version_id != version_id`；
  7. **防泄漏回归**：`GET /knowledge/documents/{id}/` 的详情与版本响应**仍然不含 `raw_text`**
     （保留原有断言，不得为了新接口放宽）。
- 测试实现注意：`FakeKnowledgeStore`（`tests/api/test_knowledge_router.py:94-196`）目前**没有**
  `get_version_by_id`，必须给它补一个**租户感知**的实现（跨租户 id 抛 `DocumentNotFoundError`），
  否则第 2/3 条会变成"假通过"。新增/触碰该方法时，顺手把它周边遗留的英文注释
  改写成中文（`AGENTS.md` 1.3）。

#### 任务 A4 —— 接口文档更新（可与 A3 并行）

- 改动：`docs/frontend/api-inventory.md`：
  4.4 知识库章节新增该接口条目；修正 4.4.4（"查看文档内容当前无法通过 API 实现"）；
  5.4 第 9 条划掉"无正文预览"；附录 A.8 补 `Citation` 两个新字段、新增 A.x 正文响应字段字典；
  接口计数同步。
- 验收标准：文档内每个字段都能在 `app/schemas/` 找到对应；计数与实际路由数一致。

#### 任务 A5 —— 正文接口端到端（集成验证）

- 改动：`tests/integration/` 下新增/扩展用例（真实临时 SQLite + 假 embedding/vector store，
  沿用 `tests/integration/test_knowledge_pipeline.py` 风格）。
- 验收标准：走完"上传 → 入库 → 检索 → citation 带偏移 → 用 citation 的偏移请求正文接口 →
  `text[start:end] == citation.content`"的完整闭环，含一份**中文文档**（覆盖多字节场景）。

### 阶段 B：前端（B1、B2、B3 可并行起步；B4 依赖 B2/B3；B5 依赖 B4；B6 依赖 B3）

> 阶段 A 的 A2（Citation 带偏移）与 A3（正文接口）契约冻结后，B1 即可开工；
> B2 是纯函数，不依赖后端，可与 A 阶段并行。

#### 任务 B1 —— 类型与 API 方法

- 改动：`frontend/src/api/knowledgeTypes.ts`、`frontend/src/api/knowledge.ts`、
  `frontend/src/api/types.ts`（`Citation` 两个可空字段）。
- 验收标准：`npm run typecheck` 通过；`src/api/__tests__/knowledge.spec.ts` 新增用例断言
  URL 拼接（含 `encodeURIComponent`）与响应透传；`Citation` 新字段注释为中文。

#### 任务 B2 —— 偏移定位与高亮工具（纯函数）

- 改动：新增 `frontend/src/utils/markdownRange.ts` + `utils/__tests__/markdownRange.spec.ts`。
- 验收标准（jsdom 单测，中文注释）：
  1. 单段落内偏移 → 高亮文本片段内容正确、位置正确；
  2. 跨段落 / 跨列表项偏移 → 分段高亮，DOM 结构未被破坏（其余文本不变）；
  3. 偏移落在 `**加粗**`、`` `代码` `` 等标记内部 → 不抛异常、走降级；
  4. 越界与 `null` 输入 → 返回"需降级"信号，不抛异常；
  5. 重复文本场景（同一句话出现两次）→ 命中的是正确那一次（**不得用 `find` 定位**）；
  6. 高亮后 `clearHighlight` 能把 DOM 还原到高亮前（用 `outerHTML` 比对）。

#### 任务 B3 —— 正文查看器组件

- 改动：新增 `frontend/src/components/knowledge/DocumentContentViewer.vue` + 测试。
- 验收标准（组件测试 + 手工确认，中文注释）：
  1. loading / error（含重试）/ 空正文 三态齐全；
  2. 渲染正文用现有安全渲染器（`utils/markdown.ts`，`html:false`），禁止新增 `v-html` 直出；
  3. `source_type = word|pdf` 时显示"由 PDF/Word 转换"提示；`text` 时显示对应提示；
  4. `active_version_id !== version_id` 时显示历史版本提示（含两个版本号）；
  5. `status = disabled` 时显示停用提示；
  6. 传入偏移时自动滚动并高亮；偏移为空时显示降级文案且不报错；
  7. `outline` 非空时渲染目录，点击目录项滚动到对应标题；
  8. 同一 `(document_id, version_id)` 重复传入不重复请求（mock 调用次数断言）。

#### 任务 B4 —— 引用卡片入口（引用 → 正文）

- 改动：`frontend/src/components/chat/CitationList.vue` + 其测试；`MessageList.vue` /
  `ChatView.vue` 按需透传 `openDocument` 事件。
- 验收标准：
  1. 每条引用都有"查看原文位置"入口（键盘可达，`button` 而非 `div`）；
  2. emit 的载荷是结构化字段（`documentId`/`versionId`/`startOffset`/`endOffset`/`title`/
     `headingPath`），**不从回答文本或引用正文里解析**；
  3. `start_offset`/`end_offset` 为 `null`/缺失时入口仍可用（降级语义），不显示"定位"字样；
  4. `CitationList.spec.ts` 原有断言（引用默认展开、heading_path 为 null 不渲染空行）不回归。

#### 任务 B5 —— 对话区右侧正文面板

- 改动：新增 `frontend/src/components/chat/DocumentContentPanel.vue`；`ChatView.vue` 布局与状态；
  企业切换时关闭面板（挂 `tenantReset`）。
- 验收标准：
  1. 桌面 ≥1280px：面板与对话区**同屏并排**，对话区收窄但消息不重排错乱、输入框仍可用；
  2. 面板可关闭；关闭后对话区恢复原宽，会话不重载、消息不丢失、滚动位置保持；
  3. 连续点击两条不同引用：同一版本内只重新定位（不重复请求），跨版本/跨文档才重新请求；
  4. 窄屏 <1280px：改为整页跳转 `/app/knowledge/:documentId?versionId=&start=&end=`，
     并能返回对话；
  5. 正文加载失败时对话区不受影响，面板内可重试；
  6. 手工验收（写入 `manual-test-runbook.md`，见 B6）覆盖上述五条。

#### 任务 B6 —— 知识库详情页入口 + 文案修正

- 改动：`frontend/src/views/KnowledgeDetailView.vue`（新增"查看正文"入口，删除 `:271-274`
  "正文、预览与下载暂不提供"）、`KnowledgeDetailView.spec.ts`。
- 验收标准：
  1. 有有效版本时可打开正文（复用 B3 组件）；无有效版本（`processing`/`failed`）时入口禁用
     并说明原因；
  2. 页面上不再出现"暂不提供正文"的表述，且**下载入口依然不提供**（决策 7）；
  3. 原测试中断言该文案的用例一并更新（不得留下永久跳过的用例）。

### 阶段 C：验收与文档收口

#### 任务 C1 —— 端到端手工验收清单

- 改动：`docs/frontend/manual-test-runbook.md`（F 知识库章节 + E-07 引用相关条目）、
  `docs/frontend/document-content-viewer-design.md` 标记为"已由正式设计稿取代"。
- 验收标准：清单含真实可复现步骤与预期结果，覆盖：成员可读 / 跨租户不可读（用两个企业验证）/
  历史版本提示 / PDF 与 Word 转换提示 / 偏移为空降级 / 停用文档可读 / 刷新后引用消失（已知限制）。
- 依赖：B3、B5、B6 全部完成。

#### 任务 C2 —— 能力清单与 README

- 改动：`README.md` 前端能力清单补一条"知识库正文查看与引用跳转"；
  如涉及后端接口计数，`docs/frontend/api-inventory.md` §3 汇总表同步（与 A4 合并亦可）。

## 6. 后续待完成（本次不做，登记备查）

| # | 事项 | 为什么先不做 | 触发条件 / 前置 |
| --- | --- | --- | --- |
| 1 | ~~**引用在会话历史中持久化**（刷新后仍能跳转）~~ | **已实施（2026-09-17，见 `2026-09-17-conversation-history-citation-persistence-design.md`）**：历史接口返回 `citations` 与 `answer_incomplete`，引用与回答同事务落库（迁移 `0014_message_citations`），刷新后引用卡片与跳转行为与实时完全一致；**不重建**"旧引用对齐到最新版本"，历史引用按生成时冻结的 `version_id` 解释 | 已完成；本次**未**持久化 `events` / `pending_approvals` / `retrieval_summary` |
| 2 | **正文分页 / 窗口化** | 2 MiB 上限的纯中文文档约 70 万字符，本次先全量返回 | 实测出现明显卡顿，或上传上限提高时；接口已预留窗口参数位 |
| 3 | **原文文件下载** | 需要新表 + 存储策略（`raw_bytes` 解析成功即清空），且要处理体积与配额 | 用户明确要求"下载我上传的原件"时 |
| 4 | **面板宽度可拖拽 / 最大化 / 记忆用户偏好** | 属体验优化，先固定宽度验证主流程 | 用户反馈面板太窄/太宽时 |
| 5 | **知识库侧选中文档送入对话上下文** | 需要新的工具语义与权限设计，与本次"只读查看"边界不同 | 有明确产品需求时 |
| 6 | **旧文档的偏移回填** | 偏移本来就已经入库（`chunking.py` 一直写 offset），无须回填；仅"历史上是否有过无偏移 chunk"需在 A5 用真实数据确认 | 若 A5 发现旧库存在 NULL 偏移，再评估一次性重算 |
| 7 | **高亮整块而非精确区间** | 仅作为 B2 的降级能力保留，不单独做产品化 | 精确高亮在真实文档上命中率不足时 |
| 8 | **停用/失败文档的独立提示页** | 本次用文案覆盖 | 有运营诉求时 |

## 7. 风险与回归边界

1. **回归红线：详情/版本接口不得带 `raw_text`。** 现有测试有防泄漏断言
   （`tests/api/test_knowledge_router.py:382,408-409`，`:240` 把 `raw_text` 硬编码为
   `"secret body must never appear in a response"`）。新接口返回正文是**唯一**允许的例外，
   任务 A3 必须证明两者同时成立。
2. **租户隔离红线**：新接口只经 `get_current_tenant` 取 `organization_id`，
   **禁止**接受任何来自 query/body 的企业标识；跨租户一律 404。
3. **偏移一致性**：依赖"`raw_text` 就是切块用的那份文本"这一不变量。前端越界自检 +
   降级路径是兜底；如果真实数据上大面积对不上，说明不变量被破坏，应停下来查而不是放宽降级。
4. **chunk 重叠**：相邻块正文重叠 80 token，同一段落可能被两条引用命中并高亮不同区间——
   这是预期行为，不要"去重合并"。
5. **窄屏体验**：并排面板在窄屏不可用，必须走 B5 的整页降级，否则会把对话区压坏。
6. **文案责任**：PDF/Word 转换提示缺失会造成"以为看到原文"的误解，属必须上屏项。
