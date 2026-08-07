# 阶段 A 知识库 RAG Baseline 指标报告（Task 14）

> 本文档由 `scripts/run_knowledge_baseline_eval.py` 的真实运行结果整理而来。
> 除文字说明外，所有数值均从 `.artifacts/knowledge-baseline.json` 逐字复制，未做任何重算或美化。
> 评测集、Runner 与黄金答案均未修改。

## 1. 版本与数据集元数据

| 项目 | 值 |
|---|---|
| Git revision (`code_revision`) | `972cac644e502f5e7ccc757db2265dbc4ea53ca4` |
| 数据集 SHA-256 (`dataset_hash`) | `dc21a00f6503da8f01c69902765ef7f505c44abd2279bcbd7e442240bba6c412` |
| Loader 版本 | `supportpilot-loader-v1` |
| Chunker 版本 | `supportpilot-chunker-v1` |
| Embedding 模型 | `text-embedding-v4` |
| Embedding 维度 | 1024 |
| Qdrant Collection | `supportpilot_knowledge_te4_1024_v1` |
| Top-K (`top_k`) | 5 |
| Token 预算 (`token_budget`) | 3000 |
| Base Prefetch 上限 (`base_prefetch_limit`) | 8 |
| 检索轮数 | 1（无 Planner / Assessor / 第二轮） |
| 价格快照日期 | 2026-08-08 |
| 价格（CNY / 1k input tokens） | 0.0005 |

评测集共 16 条，四类各 4 条：`simple_policy`、`multi_condition_policy`、`mixed_fact_policy`、`safety_no_answer`。

## 2. 总体指标（逐字来自 JSON）

| 指标 | 值 | MVP 阈值 | 是否达标 |
|---|---|---|---|
| `retrieval_recall@5` | 0.000 | >= 0.85 | 否 |
| `citation_precision` | 0.000 | >= 0.95 | 否 |
| `cross_tenant_leak_rate` | 0.000 | 必须 = 0 | 是 |
| `average_search_rounds` | 1.00 | — | — |
| `average_model_calls` | 0.00 | — | — |
| `average_tokens` | 549.7 | — | — |
| `p50_latency_ms` | 230 | — | — |
| `p95_latency_ms` | 280 | — | — |
| `estimated_embedding_cost_cny` | 0.0043975 | — | — |

16 / 16 个样例全部完成；无任何样例被强转为“无命中”。本次运行通信下发的全部调用（16 条 query 嵌入 + 文档嵌入）均成功，无 embedding/Qdrant 基础设施失败——`model_calls = 0`、`average_search_rounds = 1.00`，与 Baseline 固定单轮、无模型判定的设计一致。

## 3. 分类指标表

| 分类 | 样例数 | recall@5 | citation_precision | cross_tenant_leak |
|---|---|---|---|---|
| simple_policy | 4 | 0.0 | 0.0 | 0 |
| multi_condition_policy | 4 | 0.0 | 0.0 | 0 |
| mixed_fact_policy | 4 | 0.0 | 0.0 | 0 |
| safety_no_answer | 4 | n/a | 0.0 | 0 |

> 说明：`safety_no_answer` 类目无 `expected_relevant`，`recall_at_5` 为 `null`，不参与召回聚合；其 `citation_precision` 因全部返回了非空 citations（且 `should_have_answer=false` 时非空引用计 0）而记为 0.0。该类的“正确拒答”能力不属于阶段 A 测量范围（见第 6 节）。

## 4. cross-tenant 隔离明细

`cross_tenant_leak_rate = 0.000`，即 16/16 样例均 `cross_tenant_leak=false`。

- 所有 `forbidden_tenant_keys`（`org_b`）在各查询的返回 citations 中均未出现。
- 特别地，`safety-orgb-policy-01` 询问“另一家 B 企业的退货期限”，返回 citations 全部属于 `org_a` 的 A 版文档，未泄漏 B 企业文档。
- 真实运行证明：单共享 Collection 上 dense+sparse 两个 prefetch 的 tenant payload 过滤 + SQLite 二次校验共同生效，跨租户隔离成立。

## 5. 失败样例归因（每个样例恰选一个主因）

> 归因口径：主因只允许 loader / chunking / embedding / retrieval / fusion / citation 之一。

**系统级发现（16/16 样例共同主因）：chunking —— heading_path 约定不一致**

在阶段 A 当前实现下，`documents` / `cells` 在入库时由 `DocumentLoader` 构造的 heading stack 生成**完整路径**，`KnowledgeChunker` 将该路径写入 `document_chunks.heading_path`，最终由 `Citation.heading_path` 原样带回。真实结果为：

- 实际存储的 `heading_path` 形如 `云舟商城退货政策（A 版）/退货时限`；
- 评测集 `cases.jsonl` 的黄金答案 `expected_relevant[].heading_path` 形如 `退货时限`（不带文档标题前缀）。

两者无法相等，因此（document_key, heading_path）配对对全部样例都命中不了。经人工探针验证，检索链确实定位到了**正确的文档与正确的章节**（例如 `simple-return-window-01` 返回的第一条 citation 即 `returns` / `云舟商城退货政策（A 版）/退货时限`，其内容即黄金答案所指向的“普通会员 7 天”章节），并非检索到无关内容。也就是说，0.0 的召回/精度在真实运行下主要由**评分约定的字符串不一致**导致，而非检索质量本身完全失效。

（遵守 Task 14 的硬性要求：不得修改黄金答案来美化分数，故本报告保留 JSON 的原始 0.0 数值，不做任何纠正性重算。）

以下为代表性样例的逐条归因（每条恰选一个主因）：

| 样例 | 分类 | 主因 | 说明 |
|---|---|---|---|
| `simple-return-window-01` | simple_policy | chunking | 返回 `returns`/`云舟商城退货政策（A 版）/退货时限`，文档与章节正确，因 heading_path 前缀约定不匹配而未命中黄金答案。 |
| `multi-condition-return-01` | multi_condition_policy | chunking | 返回的 `退货时限`、`商品状态`、`包装要求` 三章节均位于 `returns` 文档且内容正确，但均带完整路径前缀，无法与裸章节黄金答案配对。 |
| `safety-unknown-exchange-01` | safety_no_answer | citation | 无答案样例被 Baseline 返回了 4 条非空 citations（未做到正确拒答），`citation_precision` 按规则计 0。这是阶段 A 固定 Top-K Baseline 的预期行为，正确拒答属阶段 B `EvidenceAssessor` 的 `INSUFFICIENT_EVIDENCE` 能力。 |

## 6. 阶段 A 边界声明

阶段 A 的 Baseline **不测量**（需要阶段 B 的生成/证据机制）：

- **answer accuracy（答案准确率）**：不在本报告范围内；
- **grounded answer rate（有据可依的回答率）**：需阶段 B 生成流程；
- **correct abstention（正确拒答率）**：需 `EvidenceAssessor` / `INSUFFICIENT_EVIDENCE`。

阶段 A 只测量检索召回，且本报告是其**对照基线**——阶段 B/C 的最终收益不应在此时出现，也不在此时要求。

## 7. 与最终 MVP 阈值的差距

- `retrieval_recall@5 = 0.000` vs 阈值 `>= 0.85`：未达标。
- `citation_precision = 0.000` vs 阈值 `>= 0.95`：未达标。
- 差距的直接来源是第 5 节所述的 heading_path 约定不一致（scoring 层面的系统性不匹配），而非跨租户泄漏或基础设施故障（两者均无问题，隔离 = 0）。

## 8. 结论与后续

- 跨租户隔离（`cross_tenant_leak_rate = 0`）与单轮固定预算 Baseline 语义在本真实运行中成立。
- 由于黄金答案与真实 heading_path 约定不一致，本报告对召回/精度给出的是**真实、未美化的 0.0**，并如实记录其系统级归因。
- 阶段 B 开始前，需就 heading_path 评分约定与评测集黄金答案达成一致（或对齐检索输出的 heading 形态），再据此作为阶段 B/C 的有效对照组。
