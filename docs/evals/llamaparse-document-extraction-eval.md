# LlamaParse 文档提取能力评估（docx / pdf）

> 评估目的：给 `app/knowledge/document_loader.py` 的改造做选型决策——
> LlamaParse 提取出来的内容，够不够支撑本项目的 RAG 检索？
>
> 评估时间：本次会话｜解析档位：`agentic`（`version=latest`）
> 样本：`docs/knowledge/01-售后服务总则.docx`、`docs/knowledge/01-售后服务总则.pdf`

## 1. 结论速览

**结论：满足，且相对现有 docx 路径是质的提升。可以按本报告第 5 节的方案接入。**

| 判断项 | 结论 | 依据 |
|---|---|---|
| 正文有没有丢？ | **没丢** | 归一化后与 ground-truth Markdown **字符序列完全一致**（相似度 1.0，缺失/新增片段均为空） |
| 标题层级有没有丢？ | **没丢** | `heading_path` 19/19 全部还原，与原生 md 完全一致 |
| 表格有没有丢？ | **内容没丢，格式变了** | 2 个表格全部保留为 HTML `<table>`，单元格文字逐字一致 |
| 关键数值有没有丢？ | **没丢** | 18/18 探针命中（含 24/48 小时、5 个工作日、1～3 个工作日、400-860-0000 等） |
| 能不能直接喂给现有 chunker？ | **能，且结果与原生 md 一致** | 15 块（基准也是 15 块）、偏移精确、0 个无标题块、最大 559 token（上限 700） |
| PDF 支不支持？ | **支持** | 现有 loader 完全不支持 PDF，LlamaParse 表现与 docx 齐平 |
| 有没有坑？ | **有，见第 4 节** | HTML 表格 token 开销 +12%、中英文间插空格、同步接口阻塞 ~27 秒 |

两条必须写进设计的前提：

1. **只能用 `markdown` 视图，不能用 `text` 视图。** text 视图丢掉全部标题标记
   （heading 数 = 0），纯文本相似度降到 0.961，还带 174 行 PDF 硬换行和孤立页码行。
   用了 text 视图，本项目 RAG 的证据定位键 `heading_path` 会全部退化成 `None`。
2. **接入必须解决同步阻塞。** 上传接口 `POST /knowledge/documents/`
   目前是在请求线程里同步跑 `ingest_new_document` 的
   （`app/api/knowledge_router.py:137-158`，没有 `BackgroundTasks`）。
   单份文档解析实测约 27 秒，直接串进去会把上传接口拖到超时。

## 2. 评估方法

### 2.1 基准从哪来

`docs/knowledge/01-售后服务总则.docx` 与 `.pdf` 都是由 `01-售后服务总则.md`
经 pandoc 转换而来（见 `docs/knowledge/文档转换教程.txt`）。因此那份 md
就是**内容上限基准**：提取结果越接近它，说明提取越无损。

这条基准比"人工抽查"强得多——它是逐字符可比的，而且**不与 LlamaParse 同源**
（pandoc 生成，非 LlamaParse 产物），不存在循环论证。

### 2.2 五类检查

| # | 检查 | 口径 |
|---|---|---|
| 1 | 文本保真度 | 去列表标记 / HTML 标签 / Markdown 标记 / 全部空白后做字符级比对，报相似度与增删片段 |
| 2 | 段落召回 | ground-truth 每个正文段落（列表项拆开计）是否都能在提取结果中找到 |
| 3 | 标题结构 | 把提取结果喂给**真实的** `DocumentLoader`，比对它能还原出的 `heading_path` 集合 |
| 4 | 关键事实 | 18 个探针：11 个关键数值（答错就是答错的那些数字）+ 7 条 stage_c 金标 `key_answer_facts` |
| 5 | 分块适配 | 喂给**真实的** `KnowledgeChunker`，校验 `content == text[start:end]` 硬约束、token 上限、标题覆盖 |

第 4 项的探针来源是 `evals/knowledge/stage_c/cases.jsonl` 中
`document_key == "general_service"` 的 3 个金标用例，期望 heading_path 为：

```
售后服务总则/2. 售后申请与通用处理流程/2.2 通用处理流程
售后服务总则/5. 特殊说明/5.2 活动期间的限制
售后服务总则/5. 特殊说明/5.3 文档间冲突的处理
```

这 3 条路径是 `app/evals/stage_c/scoring.py:244` 的 `heading_matches` 用来判分
的证据定位键——**`heading_path` 为 `None` 的 chunk 永远匹配不上**，
所以第 3 项不是"锦上添花"，而是判分与证据提示词（`app/knowledge/evidence.py:237`）
的硬需求。

### 2.3 复现命令

```bash
# 提取（幂等：产物已存在会跳过，--force 可强制重跑，会消耗额度）
.venv/Scripts/python.exe scripts/run_llamaparse_extract.py \
    --input docs/knowledge/01-售后服务总则.docx \
    --input docs/knowledge/01-售后服务总则.pdf \
    --tier agentic

# 评估（产出 JSON + Markdown 摘要）
.venv/Scripts/python.exe scripts/evaluate_llamaparse_extraction.py
```

产物：`.artifacts/llamaparse-extract/`（已 gitignore）——
每份文档一个子目录，含 `markdown.md` / `text.txt` / `pages.json` / `meta.json`，
评估结果在同级 `eval-report.json` 与 `eval-report.md`。

## 3. 评估结果

### 3.1 主表

| 指标 | 01-售后服务总则.docx | 01-售后服务总则.pdf |
|---|---|---|
| 原始字节 / 识别页数 / 耗时 | 14598 B / 4 页 / 27.69 s | 86422 B / 3 页 / 26.17 s |
| 最低 / 平均解析置信度 | 0.917 / 0.9667 | 0.926 / 0.9537 |
| **归一化字符数（提取 / 基准）** | 1882 / 1882 | 1882 / 1882 |
| **字符相似度** | **1.0** | **1.0** |
| 增删片段 | 无 | 无 |
| **段落召回率** | **39/39 = 1.0** | **39/39 = 1.0** |
| **heading_path 召回率** | **19/19 = 1.0** | **19/19 = 1.0** |
| **金标 heading_path 命中** | **3/3** | **3/3** |
| **关键事实探针** | **18/18** | **18/18** |
| HTML 表格数（内容完整） | 2 | 2 |
| 页码 / 页脚噪声 | 无 | 无 |
| **分块数（提取 / 基准）** | 15 / 15 | 15 / 15 |
| 分块偏移精确 | ✅ | ✅ |
| 分块 token：均值 / 最大（上限 700） | 157.6 / 559 | 153.1 / 512 |
| 无标题分块数 | 0 | 0 |
| token 数（提取 / 基准） | 2601 / 2264（×1.149） | 2534 / 2264（×1.119） |
| 被插入空格的字符对 | `P会`、`双1` | `城客`、`P会`、`双1` |

### 3.2 逐项解读

**文本保真度：无损。** 「相似度 1.0」的准确含义是：把排版层面的东西
（Markdown 标记、HTML 标签、所有空白）归一化掉之后，两份文本的**字符序列逐字相同**，
`SequenceMatcher` 报不出任何一处增删。这不等于"字节相同"——第 4 节列了全部
残留的排版差异。

**标题结构：完全对齐。** LlamaParse 输出的 `#` / `##` / `###` 直接命中现有
`DocumentLoader._extract_markdown_sections` 的正则（`^#{1,6}\s+`），
19 个 `heading_path` 与原生 md 逐一相同，包括金标用例要的那 3 条深层路径。
**这意味着 chunker 一行都不用改**——提取结果按 MARKDOWN 类型走，
现有的标题栈、section 划分、偏移定位逻辑原样复用。

**关键事实：18/18。** 探针覆盖了客服问答里"答错就是事故"的硬数字：

```
400-860-0000   客服热线
9:00-21:00     热线服务时间
24小时          售后申请审核响应时限
48小时          退换货验收时限
5个工作日        质量检测结果出具时限
1～3个工作日     退款到账时限
15天            质量问题退换货时限
7天             大促无理由退货时限
618 / 双11      大促活动
2026-07-01     生效日期
```

外加 3 个金标用例的 7 条 `key_answer_facts`（含"审核响应不超过 24 小时"
"审核不通过会注明原因""大促无理由退货统一 7 天""专项政策优先于总则"等）。

**分块适配：与原生 md 完全等价。** 提取结果经 loader + chunker 得到 15 个分块，
与用原生 md 跑出来的**数量相同**，且：

- `content == text[start_offset:end_offset]` 对每个分块都成立（chunker 的硬契约）；
- 最大 559 / 512 token，低于 `MAX_CHUNK_TOKENS = 700`，不会触发截断；
- 0 个分块 `heading_path` 为 `None`，证据提示词里 `heading=` 字段不会是空的。

需要如实指出一处**劣化**：提取结果的最大分块 token（559 / 512）比基准 md 的
359 明显更高。分块数虽然相同，但承载 HTML 表格的那个 section 被标签撑大了，
单个 chunk 里塞进了更多噪声。这正是第 4 节第 1 条要求"把 HTML 表转回管道表"
的直接动机——不处理的话，每份含表格的文档都会白白多占检索窗口的预算。

**成本：比原生 md 贵约 12~15%。** 同一份内容，LlamaParse 的 Markdown 视图
比 ground-truth md 多花 12%~15% 的 token（2601 / 2534 vs 2264），
根因是 HTML 表格标签。这个开销在 embedding 计费和向量库体积上是线性放大的，
值得在处理环节抹掉。

### 3.3 视图对比：为什么必须是 markdown 视图

| 指标 | markdown 视图 | text 视图（docx） | text 视图（pdf） |
|---|---|---|---|
| `heading_path` 数 | **19** | **0** | **0** |
| 字符相似度 | 1.0 | 0.961 | 0.9942 |
| PDF 硬换行行数 | 0 | 174 | 49 |
| 孤立数字/页码行 | 无 | `1`、`24`、`4`、`48` | `1`、`2`、`3` |

text 视图的实际长相（pdf 第 1 页）：

```
1.2 售后服务体系            ← 标题标记没了
本总则是云舟商城售后服务体系的总入口。具体事项分别适用以下专项政策，各政
策之间存在衔接关系：          ← 一个句子被硬换行切成两行

专项政策            处理事项   ← 表格被空格对齐拍平，单元格还被折行
《退货与换货政策》       无理由退货、质量问题退换货、不可退
                换商品
...
1                          ← 页码混进正文
```

结论很明确：**text 视图会让 `heading_path` 全军覆没，并且把完整句子切碎**，
对"一句话被完整召回"是实打实的伤害。接入时只取 `markdown` 视图，
`text` 视图在本项目里没有使用场景。

### 3.4 与现有 loader 的对照

| 指标 | 现有 python-docx 路径 | LlamaParse | 原生 md（上限） |
|---|---|---|---|
| 支持 docx | ✅ | ✅ | — |
| 支持 pdf | ❌ **完全不支持** | ✅ | — |
| 内容相似度 | 0.9973 | 1.0 | 1.0 |
| **有标题的 chunk 数** | **0 / 4** | **15 / 15** | 15 / 15 |
| 分块数 | 4 | 15 | 15 |
| 最大分块 token | **659**（逼近 700 上限） | 559 | 359 |

**这正是用户提到"docx 的后续处理其实没考虑"的量化证据。**
现有 `WordDocumentExtractor` 靠 python-docx 直读 OOXML，正文一个字不差
（相似度 0.9973），但它拿不到标题层级——它把所有段落压成一个无标题大 section
（`document_loader.py:109-114` 显式写死 `heading_path=None`）。
后果是：

- `heading_path` 全为 `None` → 证据提示词里 `heading=` 恒为空，
  且 stage_c 的 `heading_matches` 判分**永远不可能通过**；
- 只有 4 个 600~659 token 的大块 → 一块里混着 1.1 节到 2.2 节，
  检索精度和"引用到具体条款"的能力都会下降；
- 表格单元格被拍平进正文，没有行列结构。

换句话说：**现有 docx 路径"内容对、结构丢"，LlamaParse 是"内容对、结构也在"。**

## 4. 残留差异清单（会实际影响 RAG 的具体问题）

以下差异不影响"内容正确性"（所以相似度仍是 1.0），但会影响检索与成本，
接入时应当逐条决策：

1. **表格输出为 HTML `<table>` 而非 Markdown 管道表格。**
   ```html
   <table>
     <tr><th>环节</th><th>时限</th></tr>
     <tr><td>售后申请审核响应</td><td>不超过24小时</td></tr>
   ```
   内容 100% 正确，但每个单元格都套了标签，直接推高 token（+12%~15%），
   并让"行"的语义依赖标签而非换行。同时 docx 与 pdf 两种来源的表格
   HTML 结构还不一致（pdf 带 `<thead>/<tbody>`，docx 不带），
   下游清洗要能同时吃两种。
   **建议**：在 loader 归一化阶段把 HTML 表转回管道表，
   顺便把这一份的 2601 token 压回 2264 附近。

2. **中英文/数字之间被插入空格**，精确命中 3 处：
   | 位置 | ground-truth | LlamaParse |
   |---|---|---|
   | VIP 标题 | `《VIP会员权益》` | `《VIP 会员权益》` |
   | 大促 | `双11` | `双 11` |
   | 编制单位（仅 pdf） | `云舟商城客户服务部` | `云舟商城 客户服务部` |
   对**语义向量召回**基本无害；但如果接入稀疏向量 / BM25 混合检索，
   `双11` 这类查询会漏召回。
   **建议**：归一化时按"中文↔ASCII 边界"规则消除这类空格（Llamaparse 自身的
   排版习惯，不是内容错误，规整是安全的）。

3. **列表被输出成"松散列表"**：每个列表项之间都插了空行，
   `.md` 里紧凑的 `- a\n- b\n- c` 变成 `* a\n\n* b\n\n* c`。
   对分块影响不大（列表项本来就该独立成段），但会略微增加空行 token，
   且 docx 与 pdf 的列表标记还不统一（`*   ` 三个空格 vs `* ` 一个空格）。

4. **粗体范围被修改**：ground-truth 的 `**24 小时内**` 变成 `**24** 小时内`。
   纯文本语义不变，但依赖 Markdown 强调做加权的下游会受影响（本项目当前不依赖）。

5. **引用块标记丢失**：文档信息行 `> 文档信息：…` 变成裸段落。
   内容在，层级语义没了。对 RAG 无实质影响。

6. **分页切分**：docx 被识别为 4 页、pdf 为 3 页，页与页拼接处靠
   `\n\n` 连接。本次样本两页交界处没有出现内容错乱，但
   **一份 section 有可能被页边界切开**（本次没有），
   如果后续遇到跨页表格/跨页段落需要专门验证。

7. **页码没混进来**（这点是好的）：markdown 视图的"孤立数字行噪声"检查为 0，
   说明 LlamaParse 在 markdown 视图里已经剔除了 pandoc 生成的页码。

## 5. 对 loader 改造的建议

### 5.1 接入点

```
DocumentSourceType 新增 PDF（DOCX 保留）
        ↓
PDF / DOCX  →  LlamaParse（markdown 视图）  →  LoadedDocument
                                                    ↑
                        复用现有 DocumentLoader._extract_markdown_sections
```

关键判断：**LlamaParse 的 markdown 输出与现有 MARKDOWN 分支的输入契约完全兼容**，
所以改造的最小形态是"在 `DocumentLoader.load` 前面加一层 LlamaParse 提取"，
chunker 与下游（SQLite / Qdrant / 证据提示词）**一行不用改**。
`LOADER_VERSION` 建议同步升到 `supportpilot-loader-v2`，因为同一份文档走
新老路径产出的 chunk 会不同，需要让版本可追溯。

### 5.2 必须处理的四件事

1. **同步阻塞**：现有上传接口在请求线程里同步入库。27 秒的解析必须挪到
   后台任务/队列，或者让接口返回 `queued` 后由 job 轮询推进
   （`IngestionStatus.QUEUED/RUNNING/SUCCEEDED/FAILED` 这套状态机已经具备了，
   缺的只是把执行从请求里搬出去）。
2. **归一化**：新增一个"LlamaParse 输出 → 项目 Markdown 规范"的清洗步骤，
   处理第 4 节的第 1、2、3 条（HTML 表转管道表、中英文空格规整、列表压紧）。
   这一步同时把 token 开销抹平。
3. **缓存**：按 `content_hash` 缓存解析结果。上传同一份文档、或重试失败的 job
   时不应重复调用 LlamaParse——这是按页计费的。
4. **失败与降级**：
   - 密钥缺失 / 网络失败 / 额度耗尽 → 需要新的错误码（可复用
     `EmbeddingUnavailableError` 的语义，新增 `PARSING_UNAVAILABLE`），
     并按 `VECTOR_STORE_UNAVAILABLE` 的既有约定**不降级成空文档**；
   - DOCX 可以降级到 python-docx（正文仍在，只是没标题）；
   - **PDF 没有降级路径**，必须明确失败。

### 5.3 顺带要改的地方

- `app/api/knowledge_router.py:73` 的 `_extract_source_type` 目前只认
  `.md/.markdown/.txt`，要放开 `.docx` 与 `.pdf`（`.docx` 早已在枚举里，
  但接口层根本没放行——这是"docx 后续没考虑"的另一处证据）。
- `MAX_DOCUMENT_BYTES = 2MB` 对 LlamaParse 路径已经不是解析瓶颈，
  但仍应保留为上传体积闸门，避免把超大文件直接送去计费。

### 5.4 后续回归建议

本次只做了单文档的离线评估。真正接线后，建议用
`evals/knowledge/stage_c/cases.jsonl` 里 3 个 `general_service` 用例
（`simple-general-service-response-01`、`multi-vip-free-shipping-promotion-01`、
`multi-priority-doc-conflict-01`）跑一次端到端检索回归，
把 docx/pdf 入的文档与现有 md 入的文档做同题对比——
只要 `heading_path` 完全还原，这 3 个用例的 `required_evidence_groups`
就应当能正常命中。

## 6. 本次评估的局限

- **单文档样本**：1 份 3~4 页的政策文档，且是 pandoc 生成的**数字化原生**
  文件（文字层完整）。**没有覆盖扫描件 OCR、图片、多栏排版、复杂嵌套表格、
  手写体**——这些恰恰是 LlamaParse 相对本地解析最有价值的场景，
  也是它可能翻车的地方，需要单独补测。
- **未跑端到端检索**：本次校验到"chunk 层面事实可检索"为止，
  没有真跑 DashScope embedding + Qdrant + 重排（需要外部服务）。
- **只测了 `agentic` 一档**：`fast` / `cost_effective` / `agentic_plus` 的
  质量与价格权衡未评估。对这类纯文字政策文档，更便宜的档位很可能同样够用，
  建议在接入前补一组档位对比，直接决定单位成本。
- **"相似度 1.0"是归一化后的结论**：归一化会抹掉空白与标记差异，
  引用/展示场景若要求与原文逐字节一致，需要看第 4 节的残留差异清单。

## 7. 落地结果：归一化层已实现后的实测

第 4 节第 1 条（HTML 表转管道表）与第 5 节 A 阶段已经落地：

* `app/knowledge/markdown_normalizer.py` 负责 HTML 表 → 管道表转换；
* `app/knowledge/llamaparse_extractor.py` 收敛外部解析调用；
* `DocumentLoader` 在 DOCX/PDF 路径上「提取 → 归一化 → 复用 Markdown 标题栈」；
* `LOADER_VERSION` 升为 `supportpilot-loader-v2`。

用同一批 fixture 复跑 loader + chunker，分块指标已**回到原生 Markdown 的水平**：

| | 分块数 | 最大分块 token | `2.3 处理时限汇总` 分块 token |
|---|---|---|---|
| 原生 Markdown 基准 | 15 | 359 | 114 |
| docx，归一化前 | 15 | 559 | 252 |
| pdf，归一化前 | 15 | 512 | 234 |
| **docx，归一化后** | 15 | **359** | **102** |
| **pdf，归一化后** | 15 | **360** | **114** |

整篇 token：docx 2601 → 2251、pdf 2534 → 2262（原生基准 2264），
HTML 表格引入的 12%~15% 开销已经抹平。

> 口径说明：本表"归一化前"的表格块 token（252 / 234）比 §3.2 记录的
> 559 / 512 更细——§3.2 给的是**整篇最大分块**（落在 `1.2 售后服务体系`
> 那张表上），本表最后一列单看 `2.3 处理时限汇总` 那一块。两者都对，
> 只是观察对象不同。

仍未覆盖的部分见第 6 节（扫描件、复杂版式、档位对比、端到端检索回归）。
