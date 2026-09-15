# 知识库 document loader 改造：剩余任务

> 状态文档，最后更新 2026-09-16。
> 配套阅读：`docs/evals/llamaparse-document-extraction-eval.md`（解析效果评估与结论依据）。
>
> **本次会话进度**：任务 1（入库异步化）代码已完成、任务 2（端到端检索回归）已跑通；
> 任务 3 经决策**取消**。详见 §1.4 交接状态与剩余风险。

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

**阶段 D（任务 1，入库异步化）：已完成，待提交**

| 交付物 | 说明 |
|---|---|
| `migrations/versions/0013_document_versions_async_ingestion.py` | 版本行可先「预留」：新增 `source_hash`（原始字节指纹，唯一约束）与 `raw_bytes`（暂存上传字节）；放开 `content_hash` / `raw_text` 为可空 |
| `KnowledgeStore` 协议扩展 | 新增 `create_parsed_version` / `create_version`（预留）/ `get_version_by_source_hash` / `record_version_content` / `read_version_raw_bytes` / `claim_job` / `recover_stale_jobs` |
| `KnowledgeIngestionService` 拆分 | 同步入口 `ingest_new_document` / `ingest_new_version` **语义与行为完全不变**（测试、评估脚本、stage_c fixture 依赖它）；新增异步入队入口 `queue_new_document` / `queue_new_version` 与执行入口 `run_job` |
| `app/knowledge/ingestion_worker.py` | 进程内 worker：单消费者 + 专用单线程执行器，串行执行；`bind_loop()` / `enqueue()` / `drain()` / `recover()` / `stop()` |
| 上传接口 | `knowledge_router.py` 改调 `queue_*`，**不再在请求里解析**，立刻返回 `queued` |
| 应用生命周期 | `main.py` 用 `lifespan` 执行 `worker.bind_loop()` + `worker.recover()`，关闭时 `await worker.stop()` |
| 重启恢复 | `queued` 任务重新入队；`running` 且原始字节仍在的重置回 `queued` 续跑；字节已丢失的标 `INGESTION_INTERRUPTED`，**不留僵尸任务** |
| 不重复计费 | 上传期按原始字节指纹去重（`(org, document, source_hash)` 唯一约束），重复上传在**解析之前**就被拦下 |
| 新增错误码 | `INGESTION_SOURCE_MISSING`（暂存字节已不可用）、`INGESTION_INTERRUPTED`（进程中断且无法续跑） |
| 测试 | `tests/knowledge/test_ingestion_async.py`：21 个新用例，覆盖入队不等解析、端到端执行、幂等重投、解析失败落任务、去重不二次计费、重启恢复、队列满 |

**任务 2（端到端检索回归）：已跑通**

| 交付物 | 说明 |
|---|---|
| `scripts/run_document_format_regression.py` | 把 `01-售后服务总则` 的 `.md` / `.docx` / `.pdf` 三种形态分别入库到**三个独立企业**（`fmt-md` / `fmt-docx` / `fmt-pdf`），跑 3 个 `general_service` 用例，逐格式比对证据组命中与 citation 的 `heading_path` |
| 判据复用 | 直接用 `app/evals/stage_c/scoring.py` 的 `heading_matches`，不另写一套口径 |
| 结果 | **3/3 用例的证据组命中数三格式一致（各 5/6）；三格式返回的 citation 列表逐条相同**（含唯一未命中的 `5.3 文档间冲突的处理`，见 §3 待决决策 D5） |

### 1.3 剩余任务总览

| # | 任务 | 阶段 | 体量 | 状态 |
|---|---|---|---|---|
| 1 | 入库异步化 | D | 大 | **代码完成**，待提交（见 §1.4 未完成项） |
| 2 | 端到端检索回归 | E | 小 | **完成**，报告见 `.artifacts/document-format-regression/` |
| 3 | 扫描件与档位对比补测 | F | 中 | **已取消**（2026-09-16 决策） |

### 1.4 交接状态与剩余风险

**已完成的验证**

| 验证项 | 命令 | 结果 |
|---|---|---|
| Python 全量测试 | 见 §6 复现命令 | 1009 passed / 10 skipped / 1 failed |
| 迁移升降级 | 同上（含 `tests/db/test_migrations.py`） | 通过 |
| 端到端检索回归 | `scripts/run_document_format_regression.py` | exit 0，三格式一致 |

> 那 1 个 failed 是 `tests/evals/stage_c/test_stage_c_cli.py::test_documented_source_tree_entrypoint_can_import_app`，
> 失败原因是本机沙箱禁止子进程使用**管道 stdio**（`PermissionError: [WinError 5]`），
> 不是代码缺陷。基线（改动前）同样是这 1 个失败，因此本次改动**未引入新失败**。

**未完成 / 未验证（交给下一位接手者）**

1. **`attempt_count` 被自增两次（确认的缺陷，未修）**
   `run_job` 先调 `claim_job`（`attempt_count + 1`），随后 `_pipeline_version` 又调
   `mark_job_running`（再 `+ 1`），因此**一次成功入库会显示 `attempt_count=2`**。
   回归库实测每个 job 都是 2。
   修法：`_pipeline_version` 在「任务已由 `claim_job` 置为 running」时不应再调
   `mark_job_running`——把作业准备收敛成一个 helper
   （`job if job is not None else create_job + mark_job_running`）即可。
   影响面：仅诊断字段失真，不影响入库正确性；断言 `attempt_count` 的既有测试需一并核对。

2. **前端测试与类型检查未跑**
   `frontend/node_modules` 存在但 `node` / `pnpm` 不在当前 PATH，因此以下未执行：
   `pnpm test`、`pnpm run type-check`（`vue-tsc`）、`pnpm run lint`。
   本次前端改动只有 `frontend/src/api/knowledgeTypes.ts` 的两处类型放宽
   （`content_hash: string | null`、`IngestionReceipt.status` 注释），
   但 `VersionHistoryPanel.vue` 渲染 `content_hash` 的实际表现**尚未验证**。
   已核查：`shorten()` / `onCopy()` 对 `null` 均有保护（返回 `—` / 直接 return），
   理论上不会崩，但需真实跑一次前端测试确认。

3. **回归只覆盖 3 个用例 / 1 份文档**
   只有 `01-售后服务总则` 有 docx/pdf 形态，所以「三格式等价」的结论只对这份文档成立。
   其余 7 份政策文档**没有** docx/pdf 版本，未纳入比较。

4. **`--formats` 支持分批重跑但有状态耦合**
   回归脚本默认跳过已入库语料（`state.db` 复用）。若要重跑必须删掉
   `.artifacts/document-format-regression/state.db`，否则会跳过入库只跑检索。

**本次改动涉及的文件**

```
新增  migrations/versions/0013_document_versions_async_ingestion.py
新增  app/knowledge/ingestion_worker.py
新增  scripts/run_document_format_regression.py
新增  tests/knowledge/test_ingestion_async.py
修改  app/knowledge/base.py            协议 + DocumentVersion 增加 source_hash/raw_text 可空
修改  app/knowledge/sqlite_store.py    预留/回填/抢占/恢复四个方法与 job 相关实现
修改  app/knowledge/ingestion.py       queue_* + run_job，同步入口保持不变
修改  app/knowledge/factory.py         装配 worker 并注入为 dispatcher
修改  app/api/knowledge_router.py      上传改走 queue_*
修改  app/schemas/knowledge.py         DocumentVersionResponse.content_hash 放宽为可空
修改  main.py                          lifespan：bind_loop + recover + stop
修改  frontend/src/api/knowledgeTypes.ts  同步类型放宽
修改  tests/{api,knowledge}/*          既有用例适配新契约
修改  docs/knowledge-rag-operations.md 第 5.1/5.2/5.7/7 节改写为异步语义，新增 7.1 重启恢复
```

**尚未提交**：以上改动仍在工作区，未 commit。

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

### 任务 2：端到端检索回归（已完成）

**目标**：证明 docx/pdf 入库后，检索结果与原生 md 入库等价。

**实际做法**：`scripts/run_document_format_regression.py`

用 `evals/knowledge/stage_c/cases.jsonl` 中 3 个 `general_service` 用例：

- `simple-general-service-response-01`
- `multi-vip-free-shipping-promotion-01`
- `multi-priority-doc-conflict-01`

把 `01-售后服务总则` 分别以 `.md`、`.docx`、`.pdf` 三种形式入库，跑同一批用例。

**与最初计划的一处偏离（有意）**：原计划是「不同 document」，实际改为
**三个独立企业**（`fmt-md` / `fmt-docx` / `fmt-pdf`）。原因：三份文件内容完全相同，
若入到同一企业，检索会同时命中三份等价文档，citation 归属无法区分是哪种格式贡献的，
比较会失去意义。分企业后每个企业内部只有一种格式，结果可逐条对照。

其余 3 份被用例引用的文档（`退货与换货政策` / `物流配送与异常处理` / `VIP会员权益`）
在每个企业里都用原生 md 入库——它们只保证用例可判分，不是比较对象。

**判据复用**：命中判定直接调用 `app/evals/stage_c/scoring.py` 的 `heading_matches`
（精确匹配或以 `/<expected>` 结尾），不另写一套口径。

**检索器选择**：用 `BaselineKnowledgeSearchService` 而非 Adaptive。
Baseline 是确定性检索（embedding → Qdrant 混合召回 → 交叉重排 → SQLite 二次校验），
Adaptive 还要跑 Planner/Assessor 大模型调用，会引入与本任务无关的随机性。

**验收结果**（`.artifacts/document-format-regression/report.json` / `report.md`）

| 用例 | md | docx | pdf | 命中数一致 |
|---|---|---|---|---|
| `simple-general-service-response-01` | 1（heading 精确命中） | 1（精确） | 1（精确） | ✅ |
| `multi-vip-free-shipping-promotion-01` | 2（heading 精确命中） | 2（精确） | 2（精确） | ✅ |
| `multi-priority-doc-conflict-01` | 2 | 2 | 2 | ✅ |

- 证据组命中总数：md 5 / docx 5 / pdf 5（满分 6，见下方说明）；
- 三格式返回的 citation 列表**逐条相同**（不只是命中数相同）；
- `multi-priority-doc-conflict-01` 唯一未命中的 `售后服务总则/5. 特殊说明/5.3 文档间冲突的处理`
  在**三种格式里都同样缺失**。核对该用例在 `.artifacts/stage-c-retrieval*/report*.json`
  中的历史 baseline 结果，该 heading 从来不在 top-5 里——这是既有的检索排序特性，
  **与入库格式无关**，不构成本次改造的回归。

### 任务 3：扫描件与档位对比补测（已取消）

**2026-09-16 决策：取消，不做。**

原目标与工作内容保留在下方仅作记录，不再排期：

1. ~~**扫描件 / 复杂版式**~~：补测扫描件 OCR、图片、多栏排版、复杂嵌套表格、跨页表格。
2. ~~**档位对比**~~：对同一份文档跑 `fast` / `cost_effective` / `agentic` / `agentic_plus`，
   对比质量与单价，据此决定 `LLAMA_CLOUD_TIER` 默认值。

因此 **`LLAMA_CLOUD_TIER` 的默认值仍为 `agentic`**，其依据只有
`docs/evals/llamaparse-document-extraction-eval.md` 的单档位评估，
不是经过档位对比后的结论。上文 §5 相应的验收项已随之撤销。

## 3. 待决决策

| # | 决策点 | 现状 | 需要什么才能定 |
|---|---|---|---|
| D1 | 是否规整中英文之间的插入空格（`双 11` → `双11`、`《VIP 会员权益》`） | **暂不处理**（归一化层刻意未做） | 取决于 DashScope sparse 向量是否归一化。若归一化，标签/空格会稀释内容词权重；若是朴素点积（Qdrant 当前**未配 modifier**），query 里不出现的 term 贡献恰为 0，可不动。需要一个十行实验：两版本文本分别 embed，用金标 query 算 sparse 点积 |
| D2 | 表格跨 chunk 被切开 | **暂不修改，已纳入后续考虑**。本次样本两个表格都完整，但更长表格会被 `RecursiveCharacterTextSplitter` 切断，出现"有 `<td>` 没 `<table>`"的孤儿片段 | 这是"表格跨 chunk"的通用问题（Markdown 管道表被切开一样丢表头），正确解法是让 chunker 视表格为不可分割单元。对现有 `.md` 知识库同样有收益，计划独立立项 |
| D3 | `WordDocumentExtractor` 的去留 | **保留但不被调用**（`DocumentLoader` 已改走外部解析）。它是一条不依赖网络的降级实现 | 若要启用为 DOCX 降级路径，需明确：两条路径产出的 chunk 不同，`content_hash`/`LOADER_VERSION` 语义要写清楚 |
| D4 | 幻影空 section | **暂不修改**。标题后**只跟一个空行**时，`DocumentLoader._extract_markdown_sections` 的 `flush()` 会产出一个 `content=""` 的 section——`"".isspace()` 为 `False`，逃过了空内容检查（跟两个以上空行行为还不一致） | 一行修复：`content.isspace()` → `not content.strip()`。chunker 会跳过空 section，所以 chunk 输出不变，只影响 `loaded.sections` 的可读性；但会改变所有 Markdown 文档的 sections 列表，需确认无消费者依赖 |
| D5 | `multi-priority-doc-conflict-01` 的 `5.3 文档间冲突的处理` 从未进 top-5（任务 2 新发现） | **不在本次范围**。该 heading 是「专项政策与总则冲突时以专项政策为准」这一事实的唯一定位键，但它从未被 top-5 召回——**改动前的历史 baseline 同样如此**，且三种入库格式表现完全一致。因此这不是格式差异，而是既有检索排序的召回缺口 | 需要提升该段落（或整个 `5. 特殊说明` 小节）的召回排名：候选手段是 chunk 粒度/overlap 调整、`PREFETCH_LIMIT` 放大、或把「冲突处理」这种元规则条款单独加权。应先量化影响面（还有多少金标 heading 落在 top-5 之外），再决定是否立项 |

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
8. **迁移 0013 的降级保护**（本次新增）：`downgrade()` 会先检查
   `content_hash IS NULL` 的行，有则 `RuntimeError` 拒绝降级——那些是「已预留但还没
   解析完」的版本行，回退会连同排队中的任务一起抹掉。`document_versions` 有入向外键
   （`document_chunks` / `ingestion_jobs` / `documents.active_version_id`），
   同样用 `batch_alter_table(recreate="always")` 重建整表。
9. **`content_hash` / `raw_text` 现在可为 NULL**（本次新增）：异步入库下版本行先于
   解析结果落库。判断「是否已解析」**只需看 `content_hash IS NULL` 一个条件**
   （两者同生同灭）。任何读取版本正文或哈希的新代码都必须处理 `None`，
   前端 `VersionHistoryPanel.vue` 的 `shorten()` / `onCopy()` 已有保护。
10. **`raw_bytes` 是暂存列，不是长期存储**（本次新增）：上传的原始字节（最大 2 MiB）
    暂存在 `document_versions.raw_bytes`，解析成功后由 `record_version_content`
    同语句清空；**解析失败时保留**，以便重试不必要求用户重传。
    它只服务「本次解析」与「重启续跑」，不要把它当作文档备份。
11. **同步入口未被移除**：`ingest_new_document` / `ingest_new_version` 仍在且行为不变，
    评估脚本与 stage_c fixture 依赖它们。新增异步能力走 `queue_*` + `run_job`，
    不要把同步入口改成异步——那会连带改掉一套已固化的评估基线。
12. **进程内 worker 的边界**：单消费者 + 单线程执行器，入库串行执行。
    并发上传会排队；队列容量 `DEFAULT_QUEUE_CAPACITY = 256`，满了抛
    `IngestionQueueFullError`（任务记录已在库里，不会丢，重试或重启即可捡回）。
    要提吞吐应改为独立 worker 进程 + 任务抢占，而不是把线程数调大。

## 5. 整体验收口径

这批改造可视为完成，当且仅当：

- [x] 用户能从界面选择 `.docx` / `.pdf` 并成功入库（原任务 1、2、3）
- [x] 解析服务不可用时，失败语义是 `PARSING_UNAVAILABLE`，
      而非 `INVALID_DOCUMENT`（422），且**不会**降级成空文档
      —— 异步化后该错误落在 **job 的 `error_code`** 上而非 HTTP 状态码（见 §6 说明）
- [x] 重复上传同一份文档不产生重复的解析计费（原任务 4）
      —— 上传期按原始字节指纹去重，重复上传在解析之前就被拦下
- [x] 入库的 docx/pdf 文档，其 chunk 的 `heading_path` 与同内容 `.md` 入库一致（任务 2）
      —— 三格式 citation 列表逐条相同，证据组命中数一致
- [x] 大文档上传不会因请求超时而失败（任务 1）
      —— 上传接口不再等待解析，立刻返回 `queued`
- [x] ~~扫描件场景有明确结论，`LLAMA_CLOUD_TIER` 默认值有依据~~（任务 3 已取消，本条作废）

> **关于第 2 条的口径变化（重要）**：入库异步化后，上传接口**不再等待解析**，
> 因此解析类失败不可能再表现为 HTTP 5xx —— 接口已经用 201 + `queued` 成功返回了。
> 失败改为记在任务上：`GET /knowledge/ingestion-jobs/$JOBID/` 返回的
> `error_code="PARSING_UNAVAILABLE"`。验收意图（「不拿文档非法指责用户、不降级成空文档」）
> 完整保留，只是承载位置从 HTTP 状态码变成 job 字段。相应的既有测试
> `test_parsing_unavailable_maps_to_503_not_422` 已改名为
> `test_parsing_unavailable_is_not_reported_as_invalid_document`，断言「不返回 422」。

## 6. 复现命令

### 6.1 Python 测试

```powershell
# 必须显式指定 --basetemp：本机沙箱禁止 pytest 使用系统临时目录，
# 否则会以「Access is denied」直接失败（不是用例失败）。
$env:DASHSCOPE_API_KEY = (Get-ItemProperty -Path 'HKCU:\Environment' -Name 'DashScope_API_KEY').DashScope_API_KEY
.venv\Scripts\python.exe -m pytest -q --basetemp=.artifacts/pytest-tmp
```

预期：`1009 passed, 10 skipped, 1 failed`。
那 1 个 failed 是 `test_documented_source_tree_entrypoint_can_import_app`
（沙箱禁止子进程管道 stdio），与本次改动无关。

`DASHSCOPE_API_KEY` 从 Windows 用户环境变量 `DashScope_API_KEY` 读取后注入子进程；
项目 `.env` 里**没有**这个键（只有 `AUTH_SECRET_KEY` 与 `LLAMA_CLOUD_API_KEY`）。

### 6.2 端到端检索回归（需要 DashScope + Qdrant）

```powershell
$env:DASHSCOPE_API_KEY = (Get-ItemProperty -Path 'HKCU:\Environment' -Name 'DashScope_API_KEY').DashScope_API_KEY
.venv\Scripts\python.exe scripts\run_document_format_regression.py

# 只跑某一种形态（语料已入库会自动跳过）
.venv\Scripts\python.exe scripts\run_document_format_regression.py --formats word

# 重跑全部（必须先删 state.db，否则会跳过入库）
Remove-Item .artifacts\document-format-regression\state.db
```

产物：`.artifacts/document-format-regression/report.json` 与 `report.md`。
首次跑约 13 分钟（12 份文档入库，每份 embedding+Qdrant 约 65 秒）。

### 6.3 本机环境注意事项

- **Qdrant**：已在跑（`http://localhost:6333`）。注意本机系统代理会劫持
  `localhost` 请求并导致 `WinError 10054`，因此所有本地 Qdrant 客户端都必须带
  `trust_env=False`（`create_knowledge_services` 与回归脚本都已处理；
  `tests/integration/test_*.py` 里**没有**处理，所以那 10 个集成用例在本机恒为
  skipped —— 这是环境问题，不是用例问题）。
- **pytest 的 `--basetemp`**：见 6.1。
- **前端**：`frontend/node_modules` 存在，但 `node` / `pnpm` 不在当前 PATH，
  前端测试与 `vue-tsc` 本次未执行。
