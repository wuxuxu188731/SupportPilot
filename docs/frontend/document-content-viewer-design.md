# 知识库正文查看与引用跳转 —— 可行性 / 改动范围（讨论稿，尚未改代码）

> 本文只做可行性论证与改动盘点，**不包含任何代码改动**，结论供排期决策使用。
> 事实均标注了源码位置；未在代码中证实的内容一律记为「待确认」。

## 1. 目标

1. 用户上传文档（`.md` / `.markdown` / `.txt` / `.docx` / `.pdf`）后，前端可以查看**文档正文**；
2. Agent 回答引用知识库（结构化 `citations` 中的 `C1..Cn`）时，点击可以**跳转到该引用对应的正文位置**；
3. 前端**不渲染 PDF / Word 原文**，只渲染入库时已经转换好的 Markdown —— 因为切块、向量、
   引用全都基于这份 Markdown，引用跳转天然指向它。

## 2. 可行性结论

**可行，而且不需要重新解析、不需要新增存储。** 三个关键前提在当前代码里已经全部成立：

| 前提 | 现状 | 结论 |
| --- | --- | --- |
| PDF/Word 入库时确实被转成 Markdown | `app/knowledge/document_loader.py:156-184`（`_load_extracted` → LlamaParse → `markdown_normalizer.normalize_extracted_markdown`），且 `_reload_from_stored_text` 重跑时按 MARKDOWN 处理 | 成立 |
| 转换后的 Markdown 已经落库 | `document_versions.raw_text`（同步写：`sqlite_store.py:329-389`；异步回填：`record_version_content`） | 成立 |
| 每个知识块在正文中的**精确字符区间**已经落库 | `document_chunks.start_offset` / `end_offset`（迁移 `0009_knowledge_base.py:137-158`，约束 `start_offset >= 0`、`end_offset > start_offset`），语义是「`raw_text[start:end]` 的绝对字符偏移」（`chunking.py:169-184`） | 成立 |

也就是说：**"查看正文" = 把 `document_versions.raw_text` 交给现有 Markdown 渲染器；
"跳转到对应内容" = 用 citation 的 `start_offset/end_offset` 在这份正文里滚动 + 高亮**。
不需要 PDF.js、不需要把原始二进制再拿回来。

另外三个已核实的事实（省掉一整类返工）：

- **不需要新增数据库迁移**：写入路径所需的列全部已存在，当前 alembic head 是
  `0013_document_versions_async_ingestion`（线性链 0001→0013），本次功能是纯读路径，
  不触碰表结构。
- **偏移是 Python `str` 的字符偏移**，不是字节偏移，也不是行号：`chunking.py` 的稳定性契约
  原文是 `content == document.text[start_offset:end_offset]`（`chunking.py:12-13`），
  切块时就是 `content=text[base_start:end]`（`:169-184`）。
- **正文只做 `\r\n|\r → \n` 归一化，没有任何裁剪**（`document_loader.py:144`、`:176`），
  落库也是原样透传（`sqlite_store.py:87`、`:359-377`、`:565-581`）。
  唯一的尺寸约束是上传字节上限 `MAX_DOCUMENT_BYTES = 2 MiB`（`document_loader.py:42`）。

## 3. 现状盘点（与本次功能直接相关的既有事实）

### 3.1 后端

- 正文**没有任何读取接口**：`GET /knowledge/documents/{id}/` 只返回摘要 + 版本列表 + 最近任务
  （`app/api/knowledge_router.py:222-255`，`app/schemas/knowledge.py`），响应显式不含正文。
  `docs/frontend/api-inventory.md` 已经把这条写成硬结论：4.4.4（第 627-628 行）"响应不含文档
  正文/原文（`DocumentVersionResponse` 无 raw_text），**"查看文档内容"当前无法通过 API 实现**"，
  5.4 第 9 条同样记为缺口。也就是说**前端现在"不允许"出现正文预览，是文档化的产品约束**，
  本次功能必须连带更新这两处文档。
- 但 store 层**已经有现成的读取方法**：`SQLiteKnowledgeStore.get_version_by_id(...)` 返回的
  `DocumentVersion` 里就带 `raw_text`（`sqlite_store.py:261-277` → `_to_version`，`base.py:46-65`）。
  也就是说**不需要新增 store 方法、不需要新增迁移**就能拿到正文。
  （补充事实：**没有**按 chunk_id 取单块的 store 方法；也没有"只取版本元数据、不带正文"的投影
  方法——`get_version_by_id`/`list_versions`/`get_version_by_hash` 都会把 `raw_text` 一起读出来，
  所以 HTTP 层必须自己白名单字段。）
- 引用对象 `Citation`（`app/knowledge/results.py:29-47`）当前字段为
  `citation_id / document_id / version_id / chunk_id / title / heading_path / content`，
  **不含偏移**。它由三处构造：
  - `app/knowledge/retrieval.py:377-392`（Baseline）
  - `app/knowledge/service.py:446-459`（Adaptive）
  - `app/agent/runner.py:69-109` 的 `validate_final_citations`：**从工具返回的 dict 重建**
    （`Citation(**allowed[citation_id])`，runner.py:84）——所以给 `Citation` 加字段时，
    这三处的 dict → 对象链路必须一致，否则会 `TypeError`。
- `ChunkWithDocumentTitle`（`base.py:83-102`）目前**没有** offset 字段，但 `_to_chunk_with_title`
  （`sqlite_store.py:111-123`）的数据来源 `resolve_active_citations` 用的是 `SELECT c.*`
  （`sqlite_store.py:1067`），**偏移列其实已经在结果集里**，只是没映射出来。
- 租户与权限：所有知识库读接口都经 `get_current_tenant` 绑定 `organization_id`，
  文档不存在与跨租户一律 404（`knowledge_router.py` 顶部注释）。写操作 403 仅 admin，
  **读操作对成员开放**（`GET /knowledge/documents/`、详情、任务查询都是成员可读）。

### 3.2 前端

- 已有**可复用的安全 Markdown 渲染器**：`frontend/src/utils/markdown.ts` 的 `renderMarkdown()`
  + `frontend/src/components/common/MarkdownContent.vue`（`html:false`、链接协议校验、图片降级为文本）。
  它现在只服务 Agent 回答，但完全可以直接渲染文档正文。
- 引用展示组件：`frontend/src/components/chat/CitationList.vue` —— 当前**只展示**
  `title / heading_path / content` 和三个 id（第 46-49 行），没有任何跳转入口。
- 回答正文里的 `[C1]` 角标已经可点击，但**只滚动到引用卡片**
  （`MarkdownContent.vue:26-34`）：`citation-C1` 元素 → `scrollIntoView`。
  本次新增的"跳到文档正文"是它的下一跳。
- 文档详情页当前明确写着"正文、预览与下载暂不提供"
  （`KnowledgeDetailView.vue:271-274`），本次要改掉这句。
- 路由：`/app/knowledge/:documentId` 已存在（`router/index.ts:83-88`），
  带 query 的"跳转 + 高亮"不需要新增路由（也可以新增一个更明确的 `knowledge-content` 路由）。
- 前端 `Citation` DTO：`frontend/src/api/types.ts:224-240`；知识库 DTO：
  `frontend/src/api/knowledgeTypes.ts`（文件头注释明确写了"详情与版本响应不含正文，前端不得伪造正文预览"）。
- 事实约束：**历史消息接口不保存 citations**（`chat_service.py:128-161` 只返回问答文本；
  `api-inventory.md:900`、`stores/chat.ts` 头部注释都记了这一条）。
  因此"刷新页面后还能从引用跳转"取决于是否把引用持久化，见 4.3。

## 4. 三个关键设计点

### 4.1 正文怎么读（接口形态）

建议新增一个只读接口（成员可读、租户隔离、需要显式版本）：

```text
GET /knowledge/documents/{document_id}/versions/{version_id}/content/
→ 200 {
    document_id, version_id, version_no,
    source_type,              # markdown/text/word/pdf：前端据此提示「已由 PDF/Word 转换」
    title,
    loader_version, chunker_version,
    content_hash,             # 与正文/偏移同一份文本的指纹，可用于自检与缓存
    text,                     # 即 document_versions.raw_text（归一化 Markdown，UTF-8、LF）
    text_length,              # 字符数（不是字节数），前端做边界自检
    outline: [ {level, title, heading_path, char_offset} ]   # 可选：标题目录
  }
```

要点与取舍：

- **必须显式带 `version_id`**：chunk 偏移是相对某一个版本的正文，
  版本一换，"第 1200 个字符"就指向别的内容。
- **用 `raw_text` 而不是重新解析**：`ingestion._reload_from_stored_text` 已经证明
  "已归一化 Markdown 重新装载"与首次解析结果同构，偏移语义一致。
- **`outline` 可选但建议做**：标题栈的行号在 loader 里本来就算了
  （`document_loader.py:186-227`），把标题的字符偏移一并返回，前端就能做目录导航 +
  "跳回章节"，也方便没有偏移的历史引用降级到"跳到所属章节"。
- **响应体积是唯一需要实测的点**：`MAX_DOCUMENT_BYTES = 2 MiB`
  （`document_loader.py:42`）是**文件字节**上限；纯中文 2 MiB ≈ 70 万字符，
  转成 JSON 大约 1.5–4 MB。局域网/本机没问题，但这是"一次把整篇塞进浏览器"的方案上限。
  建议：v1 先返回全文 + 前端虚拟滚动/惰性渲染；如果实测卡顿，
  再加一个"按字符窗口取正文"的变体（例如 `?start=&end=`）——接口形态预留即可，
  不必一开始就做分页。
- **只读接口不参与检索**：不写任何状态、不需要 admin；文档被停用（disabled）时**仍应可读**，
  因为它可能正是某条历史引用的来源。
- **未解析 / 不存在的版本**：`raw_text is None`（异步入库预留但尚未解析，`base.py:56-60`）
  统一按 404 处理（与 `DocumentNotFoundError` 同款安全口径，不泄露跨租户信息）。
  `docs/knowledge-loader-remaining-tasks.md` §9 已明确要求「任何读取版本正文或哈希的新代码
  都必须处理 `None`」——这个接口正是第一处必须遵守它的读路径。
- **注意 `DocumentVersion` 是整行对象**：store 没有"只取元数据不取正文"的投影方法
  （`get_version_by_id`/`list_versions`/`get_version_by_hash` 全都带 `raw_text`），
  所以 HTTP 层必须自己白名单字段，不能让 `DocumentVersionResponse` 直接复用——
  这与 `app/schemas/knowledge.py` 头部注释声明的"version 响应永不带 raw_text"一致：
  **正文只允许通过新的 content 接口出现**，详情/版本接口的现状约束不变。

### 4.2 引用怎么定位（偏移要不要暴露给前端）

必须暴露 —— 而且**不能让前端自己去正文里搜**。`chunking.py` 的模块注释（第 9-19 行）
专门写了为什么不能用 `find()` 定位：Markdown 切分器会改写文本、字符重叠会让重复文本
匹配到更早的位置。偏移是唯一稳定、精确的定位依据。

因此建议：

1. `ChunkWithDocumentTitle` 增加 `start_offset` / `end_offset`，`_to_chunk_with_title`
   （`sqlite_store.py:111-123`）把已经在结果集里的两列映射出来；
2. `Citation` 增加 `start_offset` / `end_offset`（`results.py:29-47`）；
3. `retrieval._build_citations`（:377-392）与 `service._citations`（:446-459）写入这两个值；
4. `runner.validate_final_citations`（:84）的 dict → 对象链路自动一致（字段名一致即可）。

**兼容性取舍（需要你拍板）**：给 `Citation` 的两个偏移字段加 `int | None = None` 默认值，
则手工构造 `Citation` 的测试**不用改**（至少涉及
`tests/evals/stage_c/test_scoring.py`、`test_reporting.py`、`test_answer_ab.py`、
`tests/knowledge/test_results.py`，约 15-20 处构造点）；
代价是"偏移可能为 None"，前端要按可降级处理（正好也覆盖 4.4 的历史引用场景）。
若做成必填，这些测试都要补参数（机械改动）。

⚠️ 一个副作用要知情：`Citation` 会作为 `search_knowledge` 工具结果的一部分进入**模型上下文**
（`service.py` 的 citations payload → 工具结果）。加两个数字字段等于给模型多一点元数据，
token 成本可忽略，但要在提示词层面确认不会诱导模型在回答里复述偏移。

### 4.3 跳转交互（三种链路要统一）

| 链路 | 数据来源 | 需要什么 |
| --- | --- | --- |
| 回答中的 `[C1]` → 引用卡片 | 已有（`MarkdownContent.vue`） | 不用改，保持 |
| 引用卡片 → 文档正文对应位置 | 本次新增 | citation 的 `document_id/version_id/start_offset/end_offset` |
| 知识库页 → 查看正文（可选跳章节/跳块） | 本次新增 | 接口 + 目录（`outline`）即可 |

推荐交互：**侧滑抽屉（drawer）打开，整页路由作为可选**。
聊天场景下整页跳走会丢失上下文（返回后消息列表要重载），抽屉式（drawer）或
"知识库详情页内嵌同一组件"能同时满足"对照回答"和"浏览文档"两种诉求。
实现上建议一个 `DocumentContentViewer.vue` 组件，两处复用：

- 知识库详情页：以卡片/抽屉形式嵌在现有详情里（替换掉现在那句"暂不提供"）；
- 聊天页：引用卡片点击 → 抽屉打开 → 加载 → 滚动并高亮 citation 的偏移区间。

（如果选整页路由，`/app/knowledge/:documentId` 已经存在，加 query 参数即可，
不需要新增路由表项；代价是聊天页返回时要重新拉历史。）

### 4.4 前端高亮的技术方案（唯一有实现风险的地方）

难点不在"渲染 Markdown"（已有），而在"把 `raw_text` 的第 `start..end` 个字符
在**渲染后的 DOM** 里精确标出来"。两个候选：

- **方案 A（推荐，精确）**：渲染后做 DOM 文本→源码偏移的双向映射。
  渲染结果与源码的差异只有实体编码（`&`→`&amp;`）与标记剥离；按文本节点遍历累计偏移，
  得到「字符偏移 → (节点, 节点内偏移, 源码偏移)」索引后，用 `Range` 精确框出 `start..end`，
  外层套一个 `class="chunk-highlight"` 的 `span`（跨元素时按文本节点分段包裹 + 整体高亮），
  再 `scrollIntoView({block:'center'})`。**正好能处理 chunk 起点/终点落在段落中间**的情况
  （chunk 切分点本来就在句子中间）。需要写一个 `wrapRangeByOffsets()` 工具 + 单元测试。
- **方案 B（高亮整块）**：借用 markdown-it 的 `token.map`（行号）给块级元素标
  `data-src-line`，把偏移换算成行号后高亮"覆盖到的块"。
  实现更稳，但精确度只到"块"，而 chunk 的 80 token 重叠会让"命中块"看起来比实际引用宽。

建议：v1 用方案 A（对正确答案的"对齐感"是这一功能的核心价值），
方案 B 作为 Range 构造失败时的兜底（高亮所在块 + 至少滚动到位）。

补充：偏移越界/陈旧的自检 —— 建议前端在渲染前校验
`0 <= start < end <= text.length`，后端也可在返回 citation 时校验
`text[start:end] == chunk.content`（chunk 内容本来就是字面子串），
不一致时把偏移置 `null`，前端降级为"跳到所属章节"。

### 4.5 历史引用（刷新后、旧版本）的降级

- **刷新后引用消失**：聊天历史不保存 citations（无对应接口），所以"点击跳转"只在本轮
  会话内可用。若要求刷新后仍可跳，需要后端在历史接口里返回 citations
  （等于把检索结果纳入安全历史，属于**接口范围扩张**，本次可先不做，但要作为已知限制写清）。
- **旧消息引用非当前有效版本**：`resolve_active_citations` 只返回 active 版本
  （`sqlite_store.py:1064-1078`），因此**新产生的引用一定是当时 active 的版本**；
  但如果之后上传了新版本，旧引用指向的 version 已不是 active。
  建议接口允许按 `version_id` 读取历史版本正文，并在界面上明确提示
  「该引用来自历史版本 vN，当前有效版本为 vM」，而不是报错或悄悄显示新版本正文。
- **引用是 `word/pdf` 来源**：界面上必须写清「正文为入库时由 PDF/Word 转换的 Markdown，
  与原文排版可能不一致」，这是你方案里最关键的一条产品事实，不能省略。

## 5. 改动范围清单

### 5.1 后端（预计 8-10 个文件，无迁移、无表结构变更）

| 文件 | 改动 |
| --- | --- |
| `app/knowledge/base.py` | `ChunkWithDocumentTitle` 增加 `start_offset` / `end_offset` |
| `app/knowledge/sqlite_store.py` | `_to_chunk_with_title`（:111-123）映射两列 |
| `app/knowledge/results.py` | `Citation` 增加 `start_offset` / `end_offset`（默认值与否见 4.2） |
| `app/knowledge/retrieval.py` | `_build_citations`（:377-392）带偏移 |
| `app/knowledge/service.py` | `_citations`（:446-459）带偏移 |
| `app/schemas/knowledge.py` | 新增 `DocumentContentResponse`（+ outline 元素模型） |
| `app/api/knowledge_router.py` | 新增 `GET .../versions/{version_id}/content/`，成员可读，404/409 口径统一 |
| `app/agent/runner.py` | 无需改（字段同名自动兼容），但要加一条断言/测试防回归 |
| `docs/frontend/api-inventory.md` | 新增 4.4.x 接口条目、更新 A.8/A.10 字段字典、划掉 5.4 第 9 条的"无正文预览"，并修正 4.4.4（第 627-628 行"查看文档内容当前无法通过 API 实现"）与 5.3/A.8 的引用契约 |
| `docs/frontend/manual-test-runbook.md` | 第 8 节 F 知识库（约 560-686 行）与 E-07（约 494-509 行）是当前的事实验收契约：F-09 明确要求界面注明「文档正文、预览与下载暂不提供」，本次要一起改写 |

### 5.2 前端（预计 7-9 个文件）

| 文件 | 改动 |
| --- | --- |
| `src/api/knowledgeTypes.ts` | 新增正文响应类型（含 `outline`） |
| `src/api/knowledge.ts` | 新增 `getDocumentVersionContent()` |
| `src/api/types.ts` | `Citation` 增加 `start_offset/end_offset`（可空） |
| `src/utils/markdown.ts` | 新增"按字符区间高亮"能力（或独立 `markdownRange.ts` + 测试） |
| `src/components/knowledge/DocumentContentViewer.vue` | 新组件：加载正文、Markdown 渲染、跳转+高亮、转换提示、目录 |
| `src/components/chat/CitationList.vue` | 引用卡片新增「查看原文位置」入口，emit/路由到查看器 |
| `src/views/KnowledgeDetailView.vue` | 嵌入查看器入口，删掉"暂不提供正文"的说明（:271-274） |
| `src/views/ChatView.vue` | 承载抽屉（或路由）与"引用 → 正文"的联动 |
| `src/router/index.ts` | 仅在选整页路由方案时需要新增一条路由 |

### 5.3 测试与文档

- 后端：`tests/api/test_knowledge_router.py` 增补正文接口用例（成功/跨租户 404/未解析版本/
  停用文档仍可读/admin 与成员都能读）；`tests/knowledge/test_knowledge_store.py` 补
  `ChunkWithDocumentTitle` 偏移断言；`tests/api/test_chat*` 或 runner 测试补 citation 字段回归。
  ⚠️ 该文件已有一组"防泄漏"断言（约 382、408-409 行：响应里不得出现 `raw_text`，
  `make_version` 把 `raw_text` 硬编码成 `"secret body must never appear in a response"`）。
  新增 content 接口后要**精确区分**：`GET /knowledge/documents/{id}/` 的详情/版本响应
  **必须继续不含 raw_text**（断言保留），只有新的 content 接口才允许返回正文——
  这是本次最容易写错的一条回归边界。
- 前端：`CitationList.spec.ts`（新增入口）、`KnowledgeDetailView.spec.ts`（不再断言"不提供正文"）、
  新增查看器组件测试与"按偏移高亮"的纯函数测试（`utils/__tests__/markdown.spec.ts` 已存在，
  可扩展或新建）。
- 文档：`docs/frontend/api-inventory.md`（必改）、`README.md`（前端能力清单加一条）、
  本文档可升级为正式设计稿。

## 6. 需要你拍板的决策点

| # | 决策 | 备选 | 建议 |
| --- | --- | --- | --- |
| 1 | 谁能读正文 | 成员可读 / 仅 admin | **成员可读**（与列表、详情、引用正文同权；agent 角色正是回答客服提问的人） |
| 2 | `Citation` 偏移是否可空 | `int`（必填，改 ~15-20 处 eval 测试）/ `int \| None = None` | `int \| None = None`，把"偏移缺失"当成正常降级路径 |
| 3 | 是否允许读历史版本正文 | 允许 + 顶栏提示 / 只允许 active / 只允许"当前 citation 所属版本" | **允许 + 提示**，否则旧引用会 404 |
| 4 | 跳转形态 | 抽屉/侧滑 / 整页路由 / 新标签 | 抽屉（保留对话上下文），知识库详情页内嵌同一组件 |
| 5 | 正文响应是否分页 | v1 全文 / 一开始就窗口化 | **v1 全文**，接口预留窗口参数；实测后再决定 |
| 6 | 刷新后引用是否仍可跳 | 不做 / 后端历史接口返回 citations | v1 **不做**，作为已知限制写进文档 |
| 7 | PDF/Word 是否额外提供原文下载 | 不提供 / 提供 | 本次不提供（新表 + 存储 + 体积策略），列为后续项 |

## 7. 风险与限制

1. **体积**：2 MiB 上限的纯中文文档 → 数十万字符的 JSON，是 v1 唯一需要实测的性能点。
2. **偏移与正文的一致性**依赖"`raw_text` 就是切块用的那份文本"这一不变量；
   建议加一条服务端自检（`text[start:end] == chunk.content`）把它变成可验证的断言。
3. **前端高亮的精确度**依赖方案 A 的偏移映射，需要覆盖：跨段落 chunk、
   表格内 chunk、代码块内 chunk、重复文本（不能用 `find()`）。
4. **历史引用**（刷新后消失、旧版本引用）是产品体验上的已知缺口，需要先明确"不承诺"。
5. **PDF/Word 正文的"转换事实"**必须在 UI 上说清，否则用户会以为看到了原文并据此追责排版差异。
6. 本文档所有源码位置基于当前工作区快照；若期间有改动，实施前需复核行号与字段。
7. **完全无法跳转的情形要提前想好文案**：文档处于 `processing`（尚未解析，`raw_text` 为 NULL）、
   引用所属版本已被清理、或正文与偏移自检不通过时，入口应给出「该引用暂时无法定位到正文」
   并保留引用卡片里的原文片段，而不是弹一个无解释的错误。

## 8. 建议的落地顺序

1. **阶段 1（后端最小闭环）**：偏移贯通到 `Citation` + 新增正文接口（含 `outline`），补后端测试。
2. **阶段 2（查看器）**：`DocumentContentViewer.vue` + 偏移高亮工具 + 知识库详情页入口。
3. **阶段 3（引用跳转）**：聊天页引用卡片 → 抽屉 → 高亮定位；补前端测试与文档。
4. **阶段 4（可选）**：窗口化正文、引用持久化、原文下载。
