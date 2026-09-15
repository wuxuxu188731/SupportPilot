# 知识库 document loader 改造：剩余任务

> 状态文档，最后更新 2026-09-15。
> 配套阅读：`docs/evals/llamaparse-document-extraction-eval.md`（解析效果评估与结论依据）。

## 1. 文档目的与当前进度

### 1.1 背景

原 `app/knowledge/document_loader.py` 只能本地解析 Markdown/TXT（UTF-8 解码）；
DOCX 虽然有一条 python-docx 路径，但产出的是一整块**没有标题层级**的正文；
PDF 完全无法处理。本次改造引入 LlamaParse 作为富文本文档的提取层，
让 DOCX/PDF 与原生 Markdown 走同一套 section 划分与 chunk 逻辑。

### 1.2 已完成（阶段 A，commit `e0ae09f`）

| 交付物 | 说明 |
|---|---|
| `app/knowledge/markdown_normalizer.py` | HTML 表格 → Markdown 管道表；token 开销从 +12%~15% 压回原生水平 |
| `app/knowledge/llamaparse_extractor.py` | 外部解析适配层，客户端可注入，失败统一收敛为 `ParsingUnavailableError` |
| `DocumentSourceType.PDF` + `ParsingUnavailableError` | `app/knowledge/base.py` |
| `migrations/versions/0012_documents_source_type_extended.py` | 放开 `documents.source_type` 的 CHECK 约束（原约束连 `word` 都没放行） |
| `DocumentLoader` 改造 | DOCX/PDF 走「提取 → 归一化 → 复用 Markdown 标题栈」；`LOADER_VERSION` → `supportpilot-loader-v2` |
| `tests/knowledge/fixtures/llamaparse/` | 固化的真实 LlamaParse 产物，单测完全离线 |
| 测试 | 993 通过 / 10 跳过 |

### 1.3 剩余任务总览

| # | 任务 | 阶段 | 体量 | 阻塞关系 |
|---|---|---|---|---|
| 1 | 接口层放开 `.docx` / `.pdf` | B | 小 | 无 |
| 2 | 前端放开扩展名与文案 | B | 中 | 依赖 1 |
| 3 | 反转前端「拒绝 docx」断言 | B | 小 | 与 2 同批 |
| 4 | 解析结果按 content_hash 缓存 | C | 小 | 无 |
| 5 | 入库异步化 | D | **大** | 无（可后置） |
| 6 | 端到端检索回归 | E | 小 | 依赖 1、2 |
| 7 | 扫描件与档位对比补测 | F | 中 | 无 |

**阶段 B + C + E（即 1–4、6）约 2~3 天**，做完即可交付「用户能上传 docx/pdf 并检索到」的可用状态；阶段 D 单独排期 2~3 天。

## 2. 任务详情

### 任务 1：接口层放开 `.docx` / `.pdf`

**目标**：让 HTTP 上传接口接受 DOCX 与 PDF。

**工作内容**

1. `app/api/knowledge_router.py:73` 的 `_extract_source_type` 目前只认
   `.md` / `.markdown` / `.txt`，需补 `.docx` → `WORD`、`.pdf` → `PDF`，
   并更新其错误文案与注释（注释里"the user can rename to .md/.markdown/.txt"
   的说法已过时）。
2. 确认 `PARSING_UNAVAILABLE` 的 HTTP 映射。**无需改动**——
   `_STATUS_BY_CODE_DEFAULT = 503` 已经覆盖它，但要在测试里把这个行为钉住，
   避免以后有人误加 422 映射。

**涉及文件**

- `app/api/knowledge_router.py`
- `tests/api/test_knowledge_router.py`

**验收标准**

- 上传 `.docx` / `.pdf` 能走到 ingestion（在未配置解析密钥时以
  `PARSING_UNAVAILABLE` / 503 失败，而**不是** 422 `INVALID_DOCUMENT`）。
- 未知扩展名（如 `.pptx`）仍然 422 `INVALID_DOCUMENT`。
- **注意**：`tests/api/test_knowledge_router.py:485`
  `test_extensions_and_empty_body_mapped_to_invalid_document` 目前把
  `doc.pdf` 与 `no_ext`、`empty`、`bad_utf8` 一起断言为 422。这个用例必须拆开：
  pdf 不再属于"非法扩展名"这一类，而 empty/bad_utf8/no_ext 的语义不变。

### 任务 2：前端放开扩展名与文案

**目标**：前端允许选择 docx/pdf，并把类型推导与后端对齐。

**工作内容**

1. `frontend/src/utils/knowledgeUpload.ts`
   - `detectSourceTypeByFilename`：补 `.docx` → `word`、`.pdf` → `pdf`
     （当前第 68-69 行只映射 md/txt，其余返回 `null`）。
   - 第 101 行的提示文案"仅支持 .md、.markdown 或 .txt 文件（不支持 Word 文档）"需重写。
2. `frontend/src/components/knowledge/DocumentUploadDialog.vue`
   - `ACCEPT_EXTENSIONS`（第 63-64 行）补 `.docx,.pdf`。
   - 第 208 行的说明文案同步更新（含体积上限说明）。
3. `frontend/src/components/knowledge/VersionUploadDialog.vue`
   - 第 9-10 行注释与第 65 行的 `accept` 计算目前是
     `text ? '.txt' : '.md,.markdown'`，需要为 `word` / `pdf` 增加分支。
     该组件要求"上传版本的文件类型必须与原文档 source_type 一致"，
     新增两种类型时这条规则必须保持。
4. `frontend/README.md` 第 289-290、299 行关于"仅支持 md/txt、不支持 Word"的说明。

**验收标准**

- 选择 `.docx` / `.pdf` 能通过前端校验并真正发起上传请求。
- 类型不匹配（例如给 markdown 文档上传 `.pdf` 版本）仍被拒绝。

### 任务 3：反转前端「拒绝 docx」断言

**目标**：把现有断言从"必须拒绝"改为"必须接受"，并保留对真正非法类型的拒绝。

**必须改动的既有断言**（这些是**有意的行为变更**，不是修 bug）

| 文件 | 位置 | 现状 |
|---|---|---|
| `frontend/src/utils/__tests__/knowledgeUpload.spec.ts` | 约 50-53 行 | `.docx` / `.pdf` 必须映射为 `null` |
| 同上 | 约 68-69 行 | `validateKnowledgeFileBasics` 必须拒绝 `.docx` |
| 同上 | 约 138 行 | 拒绝 `.docx` 的用例 |
| `frontend/src/components/knowledge/__tests__/DocumentUploadDialog.spec.ts` | 约 119 行 | "docx 文件被前端拒绝且不发起上传请求" |

**风险提示**：`knowledgeUpload.ts:9` 的原注释写着"领域枚举虽含 word，但当前
HTTP 上传不可用"——说明当年的拦截是**因为后端不支持**，而不是产品不想要 Word。
放开符合原意，但建议动手前与维护者确认一句。

**验收标准**

- 上述用例改为断言接受 `.docx` / `.pdf` 并推导出正确的 `source_type`。
- 保留一个"未知扩展名（如 `.exe` / `.pptx`）仍被拒绝"的用例，避免放开过头。

### 任务 4：解析结果按 content_hash 缓存

**目标**：同一份文档重复上传或 job 重试时，不重复调用 LlamaParse。

**为什么必须做**：LlamaParse 按页计费，单份 3~4 页文档约 27 秒；而
`KnowledgeIngestionService._reuse_duplicate_version` 在版本未激活时**会重跑整条
pipeline**（含 loader），重试失败 job 也会重新解析。

**工作内容**

1. 缓存键用 `sha256(原始上传字节)` 而非 loader 产出的 `content_hash`
   （后者的定义是 `sha256(normalized_text)`，必须先解析才能算出，救不了这次调用）。
2. 缓存内容：LlamaParse 的原始 markdown（**归一化前**）——归一化是纯函数，
   缓存原始产物可以让归一化规则升级时不必重新解析。
3. 存储位置二选一：本地文件目录（按 hash 分片）或 SQLite 表。倾向文件目录 +
   可配置根路径（`LLAMAPARSE_CACHE_DIR`），避免把大文本堆进业务库。
4. 需要清理策略；解析产物可能很大。

**涉及文件**

- `app/knowledge/llamaparse_extractor.py`（装饰/包装现有 `extract`）
- `app/core/config.py`（缓存开关与路径）

**验收标准**

- 同一份字节第二次调用 `extract` 不发网络请求（用假客户端断言调用次数）。
- 缓存未命中时行为与现在完全一致。

### 任务 5：入库异步化

**目标**：把解析与入库从 HTTP 请求线程里搬出去。

**为什么必须做**：`app/api/knowledge_router.py` 的 `upload_document`（第 137 行）
是 `async def`，却直接同步调用 `ingest_new_document`，**阻塞事件循环**。
前端上传超时是 180 秒（`frontend/src/api/knowledge.ts`，可用
`VITE_KNOWLEDGE_UPLOAD_TIMEOUT_MS` 覆盖），27 秒的解析勉强塞得下，
但更大的 PDF 必然超时。

**注意**：这是**既有技术债**，不是 LlamaParse 引入的——embedding 与 Qdrant
写入本来就在请求里同步跑。可以独立排期，不阻塞阶段 B/C。

**工作内容**

1. 上传接口先创建 document/version 与 job，返回 `queued`（`IngestionStatus`
   这套状态机已经具备），再由 worker 推进 `running → succeeded/failed`。
2. worker 形态二选一：进程内后台任务（简单，但进程重启丢任务）或独立 worker
   进程（需要处理 job 抢占与超时回收）。倾向后者，但先用前者打通。
3. 前端加 job 状态轮询 UI（`GET` job 接口已存在，见
   `docs/knowledge-rag-operations.md` §5.7）。
4. 并发与 SQLite 写锁（`app/db/migrations.py` 已有一个全局 `_upgrade_lock`
   可参考其思路）。
5. 进程重启后遗留的 `queued` / `running` job 要有恢复或标失败策略。

**验收标准**

- 上传接口在解析完成前就返回，且返回体的 job 状态可被轮询推进到终态。
- 进程重启不会留下永远 `running` 的僵尸 job。

### 任务 6：端到端检索回归

**目标**：证明 docx/pdf 入库后，检索结果与原生 md 入库等价。

**工作内容**

用 `evals/knowledge/stage_c/cases.jsonl` 中 3 个 `general_service` 用例：

- `simple-general-service-response-01`
- `multi-vip-free-shipping-promotion-01`
- `multi-priority-doc-conflict-01`

把 `01-售后服务总则` 分别以 `.md`、`.docx`、`.pdf` 三种形式入库（不同
document），跑同一批用例，比较 `required_evidence_groups` 的命中情况与
citation 的 `heading_path`。

**前置条件**：需要 DashScope（embedding + rerank）与 Qdrant 起来，
即"未跑端到端检索"这一条局限的收口（见评估报告 §6）。

**验收标准**

- 三种形式入库的文档，3 个用例的 `required_evidence_groups` 命中数一致。
- citation 的 `heading_path` 精确等于用例期望值
  （`app/evals/stage_c/scoring.py:244` 的 `heading_matches` 要求精确匹配或
  路径以 `/<expected>` 结尾，`None` 永远匹配不上）。

### 任务 7：扫描件与档位对比补测

**目标**：补上评估报告 §6 明确列为未覆盖的两块。

**工作内容**

1. **扫描件 / 复杂版式**：本次样本是 pandoc 生成的**数字化原生**文件（文字层完整）。
   需补测扫描件 OCR、图片、多栏排版、复杂嵌套表格、跨页表格。
   这既是 LlamaParse 最有价值的场景，也是它最可能翻车的地方。
2. **档位对比**：本次只测了 `agentic`。应对同一份文档跑
   `fast` / `cost_effective` / `agentic` / `agentic_plus`，用
   `scripts/evaluate_llamaparse_extraction.py` 的同一套指标对比质量与单价，
   直接决定 `LLAMA_CLOUD_TIER` 的默认值。
   （脚本已支持 `--tier`，档位也已做成环境变量。）

**验收标准**

- 扫描件样本上，提取内容能否支撑关键事实检索有明确结论。
- 给出档位选择建议与依据（质量指标 + 单价）。

## 3. 待决决策

| # | 决策点 | 现状 | 需要什么才能定 |
|---|---|---|---|
| D1 | 是否规整中英文之间的插入空格（`双 11` → `双11`、`《VIP 会员权益》`） | 归一化层**刻意未做** | 取决于 DashScope sparse 向量是否归一化。若归一化，标签/空格会稀释内容词权重；若是朴素点积（Qdrant 当前**未配 modifier**），query 里不出现的 term 贡献恰为 0，可不动。需要一个十行实验：两版本文本分别 embed，用金标 query 算 sparse 点积 |
| D2 | 表格跨 chunk 被切开 | **未处理**。本次样本两个表格都完整，但更长表格会被 `RecursiveCharacterTextSplitter` 切断，出现"有 `<td>` 没 `<table>`"的孤儿片段 | 这是"表格跨 chunk"的通用问题（Markdown 管道表被切开一样丢表头），正确解法是让 chunker 视表格为不可分割单元。对现有 `.md` 知识库同样有收益，建议独立立项 |
| D3 | `WordDocumentExtractor` 的去留 | **已保留但不被调用**（按明确指示）。它是一条不依赖网络的降级实现 | 若要启用为 DOCX 降级路径，需明确：两条路径产出的 chunk 不同，`content_hash`/`LOADER_VERSION` 语义要写清楚 |
| D4 | 幻影空 section | **发现但未修**。标题后**只跟一个空行**时，`DocumentLoader._extract_markdown_sections` 的 `flush()` 会产出一个 `content=""` 的 section——`"".isspace()` 为 `False`，逃过了空内容检查（跟两个以上空行行为还不一致） | 一行修复：`content.isspace()` → `not content.strip()`。chunker 会跳过空 section，所以 chunk 输出不变，只影响 `loaded.sections` 的可读性；但会改变所有 Markdown 文档的 sections 列表，需确认无消费者依赖 |

## 4. 已知坑与注意事项

1. **沙箱环境下有一个用例会被拒**：
   `tests/evals/stage_c/test_stage_c_cli.py::test_documented_source_tree_entrypoint_can_import_app`
   用 `subprocess.run(capture_output=True)`，在受限文件沙箱里因管道限制报
   `PermissionError: [WinError 5]`。与代码无关，跑全量测试时可 deselect。
2. **迁移 0012 的降级保护**：`downgrade()` 会先检查有无 `word`/`pdf` 行，
   有则 `RuntimeError` 拒绝降级。SQLite 改 CHECK 走的是
   `batch_alter_table(recreate="always")` 整表重建，`documents` 有入向外键
   （`document_versions` / `document_chunks` / `ingestion_jobs`），
   加新约束时保持同样写法即可。
3. **密钥位置**：`LLAMA_CLOUD_API_KEY` 放在被 gitignore 的 `.env` 里，
   `.env.example` 只有占位符。**不要**把密钥写进任何入库文件。
   另外该密钥曾在 `无关代码文件/TestLlamaParse.py`（同样被 gitignore）里以明文出现过，
   若担心泄露可考虑轮换。
4. **密钥缺失不是致命错误**：`create_knowledge_services` 在无密钥时照常装配，
   只有真正加载 DOCX/PDF 才以 `PARSING_UNAVAILABLE` 失败。
   新增代码不要把它改成启动期强校验。
5. **`MAX_DOCUMENT_BYTES = 2MB`** 现在只是上传体积闸门，不再是解析瓶颈；
   调整它等于直接影响 LlamaParse 计费，需谨慎。
6. **测试不得联网**：所有 LlamaParse 相关单测必须用
   `tests/knowledge/fixtures/llamaparse/` 下的固化产物或假客户端。
   CI 不应该消耗解析额度，也不应该依赖 27 秒的网络往返。

## 5. 整体验收口径

这批改造可视为完成，当且仅当：

- [ ] 用户能从界面选择 `.docx` / `.pdf` 并成功入库（任务 1、2、3）
- [ ] 入库的 docx/pdf 文档，其 chunk 的 `heading_path` 与同内容 `.md` 入库一致（任务 6）
- [ ] 解析服务不可用时，失败语义是 `PARSING_UNAVAILABLE`（5xx）而非
      `INVALID_DOCUMENT`（422），且**不会**降级成空文档
- [ ] 重复上传同一份文档不产生重复的解析计费（任务 4）
- [ ] 大文档上传不会因请求超时而失败（任务 5）
- [ ] 扫描件场景有明确结论，`LLAMA_CLOUD_TIER` 默认值有依据（任务 7）
