# 知识库 document loader 改造：剩余任务

> 状态文档，最后更新 2026-09-15。
> 配套阅读：`docs/evals/llamaparse-document-extraction-eval.md`（解析效果评估与结论依据）。

## 1. 文档目的与当前进度

### 1.1 背景

原 `app/knowledge/document_loader.py` 只能本地解析 Markdown/TXT（UTF-8 解码）；
DOCX 虽然有一条 python-docx 路径，但产出的是一整块**没有标题层级**的正文；
PDF 完全无法处理。本次改造引入 LlamaParse 作为富文本文档的提取层，
让 DOCX/PDF 与原生 Markdown 走同一套 section 划分与 chunk 逻辑。

### 1.2 已完成

**阶段 A（commit `e0ae09f`）：解析能力接入**

| 交付物 | 说明 |
|---|---|
| `app/knowledge/markdown_normalizer.py` | HTML 表格 → Markdown 管道表；token 开销从 +12%~15% 压回原生水平 |
| `app/knowledge/llamaparse_extractor.py` | 外部解析适配层，客户端可注入，失败统一收敛为 `ParsingUnavailableError` |
| `DocumentSourceType.PDF` + `ParsingUnavailableError` | `app/knowledge/base.py` |
| `migrations/versions/0012_documents_source_type_extended.py` | 放开 `documents.source_type` 的 CHECK 约束（原约束连 `word` 都没放行） |
| `DocumentLoader` 改造 | DOCX/PDF 走「提取 → 归一化 → 复用 Markdown 标题栈」；`LOADER_VERSION` → `supportpilot-loader-v2` |
| `tests/knowledge/fixtures/llamaparse/` | 固化的真实 LlamaParse 产物，单测完全离线 |

**阶段 B + C（commit `a1c0a8e`）：用户可用**

| 交付物 | 说明 |
|---|---|
| 接口层放开扩展名 | `_extract_source_type` 接受 `.docx` / `.pdf`；未知扩展名（含老式 `.doc`、`.pptx`）继续 422 |
| 前端放开扩展名 | `knowledgeUpload.ts` / `DocumentUploadDialog.vue` / `VersionUploadDialog.vue` / `KnowledgeDetailView.vue`（版本入口不再按类型屏蔽） |
| **前端跳过二进制编码校验** | 真实 docx（zip 包）与 pdf 的字节都不是合法 UTF-8，原先会被 `TextDecoder` 严格模式全部误判为「编码非法」。现按来源类型分支，仅对 markdown/text 做 UTF-8 校验 |
| 解析缓存 | `app/knowledge/llamaparse_cache.py`：按 **原始字节 + 档位 + 版本** 做键，原子写入；重复上传与失败重试不二次计费 |
| 测试 | Python 1010 通过；前端 462 通过；`vue-tsc` 与 `eslint` 均无告警 |

### 1.3 剩余任务总览

| # | 任务 | 阶段 | 体量 | 阻塞关系 |
|---|---|---|---|---|
| 1 | 入库异步化 | D | **大** | 无（可独立排期） |
| 2 | 端到端检索回归 | E | 小 | 无（接口与前端已放开） |
| 3 | 扫描件与档位对比补测 | F | 中 | 无 |

阶段 D 是唯一的大块，约 2~3 天；阶段 E、F 合计约 1~2 天。
**做完 1~3 即达到本文档 §4 的全部验收口径。**

## 2. 任务详情

### 任务 1：入库异步化

**目标**：把解析与入库从 HTTP 请求线程里搬出去。

**为什么必须做**：`app/api/knowledge_router.py` 的 `upload_document`（第 137 行）
是 `async def`，却直接同步调用 `ingest_new_document`，**阻塞事件循环**。
前端上传超时是 180 秒（`frontend/src/api/knowledge.ts`，可用
`VITE_KNOWLEDGE_UPLOAD_TIMEOUT_MS` 覆盖），小文档的 27 秒解析勉强塞得下，
但更大的 PDF 必然超时。

**注意**：这是**既有技术债**，不是 LlamaParse 引入的——embedding 与 Qdrant
写入本来就在请求里同步跑。

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

### 任务 2：端到端检索回归

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

### 任务 3：扫描件与档位对比补测

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
| D1 | 是否规整中英文之间的插入空格（`双 11` → `双11`、`《VIP 会员权益》`） | **暂不处理**（归一化层刻意未做） | 取决于 DashScope sparse 向量是否归一化。若归一化，标签/空格会稀释内容词权重；若是朴素点积（Qdrant 当前**未配 modifier**），query 里不出现的 term 贡献恰为 0，可不动。需要一个十行实验：两版本文本分别 embed，用金标 query 算 sparse 点积 |
| D2 | 表格跨 chunk 被切开 | **暂不修改，已纳入后续考虑**。本次样本两个表格都完整，但更长表格会被 `RecursiveCharacterTextSplitter` 切断，出现"有 `<td>` 没 `<table>`"的孤儿片段 | 这是"表格跨 chunk"的通用问题（Markdown 管道表被切开一样丢表头），正确解法是让 chunker 视表格为不可分割单元。对现有 `.md` 知识库同样有收益，计划独立立项 |
| D3 | `WordDocumentExtractor` 的去留 | **保留但不被调用**（`DocumentLoader` 已改走外部解析）。它是一条不依赖网络的降级实现 | 若要启用为 DOCX 降级路径，需明确：两条路径产出的 chunk 不同，`content_hash`/`LOADER_VERSION` 语义要写清楚 |
| D4 | 幻影空 section | **暂不修改**。标题后**只跟一个空行**时，`DocumentLoader._extract_markdown_sections` 的 `flush()` 会产出一个 `content=""` 的 section——`"".isspace()` 为 `False`，逃过了空内容检查（跟两个以上空行行为还不一致） | 一行修复：`content.isspace()` → `not content.strip()`。chunker 会跳过空 section，所以 chunk 输出不变，只影响 `loaded.sections` 的可读性；但会改变所有 Markdown 文档的 sections 列表，需确认无消费者依赖 |

## 4. 已知坑与注意事项

1. **迁移 0012 的降级保护**：`downgrade()` 会先检查有无 `word`/`pdf` 行，
   有则 `RuntimeError` 拒绝降级。SQLite 改 CHECK 走的是
   `batch_alter_table(recreate="always")` 整表重建，`documents` 有入向外键
   （`document_versions` / `document_chunks` / `ingestion_jobs`），
   加新约束时保持同样写法即可。
2. **密钥位置**：`LLAMA_CLOUD_API_KEY` 放在被 gitignore 的 `.env` 里，
   `.env.example` 只有占位符。**不要**把密钥写进任何入库文件。
   另外该密钥曾在 `无关代码文件/TestLlamaParse.py`（同样被 gitignore）里以明文出现过，
   若担心泄露可考虑轮换。
3. **密钥缺失不是致命错误**：`create_knowledge_services` 在无密钥时照常装配，
   只有真正加载 DOCX/PDF 才以 `PARSING_UNAVAILABLE` 失败。
   新增代码不要把它改成启动期强校验。
4. **`MAX_DOCUMENT_BYTES = 2MB`** 现在只是上传体积闸门，不再是解析瓶颈；
   调整它等于直接影响 LlamaParse 计费，需谨慎。
5. **测试不得联网**：所有 LlamaParse 相关单测必须用
   `tests/knowledge/fixtures/llamaparse/` 下的固化产物或假客户端。
   CI 不应该消耗解析额度，也不应该依赖 27 秒的网络往返。
6. **缓存目录**：默认 `<项目根>/.artifacts/llamaparse-cache`，已被 .gitignore 忽略。
   缓存按「原始字节 + 档位 + 版本」做键——改档位会自动重新解析，不会读到旧档位结果；
   但 `version=latest` 意味着服务端解析器升级后缓存仍是旧的，必要时手工清理该目录。
7. **二进制格式不得做 UTF-8 校验**：这是本次最容易踩的坑。真实 docx（zip 包）
   与 pdf 的字节都不是合法 UTF-8，前端 `validateKnowledgeFileUtf8` 与后端
   `content.decode('utf-8')` 都只适用于 markdown/text。新增任何「读文件先校验编码」
   的逻辑时，务必按来源类型分支。

## 5. 整体验收口径

这批改造可视为完成，当且仅当：

- [x] 用户能从界面选择 `.docx` / `.pdf` 并成功入库（原任务 1、2、3）
- [x] 解析服务不可用时，失败语义是 `PARSING_UNAVAILABLE`（5xx）而非
      `INVALID_DOCUMENT`（422），且**不会**降级成空文档
- [x] 重复上传同一份文档不产生重复的解析计费（原任务 4）
- [ ] 入库的 docx/pdf 文档，其 chunk 的 `heading_path` 与同内容 `.md` 入库一致（任务 2）
- [ ] 大文档上传不会因请求超时而失败（任务 1）
- [ ] 扫描件场景有明确结论，`LLAMA_CLOUD_TIER` 默认值有依据（任务 3）
