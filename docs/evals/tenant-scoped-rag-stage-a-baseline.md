# 阶段 A 知识库 RAG Baseline 指标报告（Task 14）

> 本文档由 `scripts/run_knowledge_baseline_eval.py` 的真实运行结果整理而来。
> 除文字说明外，所有数值均从 `.artifacts/knowledge-baseline.json` 逐字复制，未做任何重算或美化。
> 评测集、Runner、黄金答案与语料均未修改；本次运行已修复评分层的 heading_path 匹配约定（见第 5 节）。

## 1. 版本与数据集元数据

| 项目 | 值 |
|---|---|
| Git revision (`code_revision`) | `72cef7406b83da7f570a889518d8a4ebf24317ee` |
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
| `retrieval_recall@5` | 0.579（原始 `0.5789473684210527`） | >= 0.85 | 否 |
| `citation_precision` | 0.155（原始 `0.15492957746478872`） | >= 0.95 | 否 |
| `cross_tenant_leak_rate` | 0.000 | 必须 = 0 | 是 |
| `average_search_rounds` | 1.00 | — | — |
| `average_model_calls` | 0.00 | — | — |
| `average_tokens` | 553.6 | — | — |
| `p50_latency_ms` | 240 | — | — |
| `p95_latency_ms` | 277 | — | — |
| `estimated_embedding_cost_cny` | 0.0044285 | — | — |

16 / 16 个样例全部完成；无任何样例被强转为“无命中”。本次运行通信下发的全部调用（16 条 query 嵌入 + 文档嵌入）均成功，无 embedding/Qdrant 基础设施失败——`model_calls = 0`、`average_search_rounds = 1.00`，与 Baseline 固定单轮、无模型判定的设计一致。

## 3. 分类指标表

| 分类 | 样例数 | recall@5（均值） | citation_precision（均值） | cross_tenant_leak |
|---|---|---|---|---|
| simple_policy | 4 | 1.00 | 0.225 | 0 |
| multi_condition_policy | 4 | 0.333 | 0.175 | 0 |
| mixed_fact_policy | 4 | 0.75 | 0.25 | 0 |
| safety_no_answer | 4 | n/a | 0.0 | 0 |

> 说明：`safety_no_answer` 类目无 `expected_relevant`，`recall_at_5` 为 `null`，不参与召回聚合；其 `citation_precision` 因全部返回了非空 citations（且 `should_have_answer=false` 时非空引用计 0）而记为 0.0。该类的“正确拒答”能力不属于阶段 A 测量范围（见第 6 节）。
> 分类均值按 16 例 JSON 的 per-case `recall_at_5` / `citation_precision` 同权重平均；`recall@5` 总体加权值以第 2 节 JSON 的 `aggregates.retrieval_recall_at_5 = 0.579` 为准。

## 4. cross-tenant 隔离明细

`cross_tenant_leak_rate = 0.000`，即 16/16 样例均 `cross_tenant_leak=false`。

- 所有 `forbidden_tenant_keys`（`org_b`）在各查询的返回 citations 中均未出现。
- 特别地，`safety-orgb-policy-01` 询问“另一家 B 企业的退货期限”，返回 citations 全部属于 `org_a` 的 A 版文档，未泄漏 B 企业文档。
- 真实运行证明：单共享 Collection 上 dense+sparse 两个 prefetch 的 tenant payload 过滤 + SQLite 二次校验共同生效，跨租户隔离成立。

## 5. 失败样例归因（每个样例恰选一个主因）

> 归因口径：主因只允许 loader / chunking / embedding / retrieval / fusion / citation 之一。

### 5.1 评分约定不一致已修复

**背景（已解决）**：上一版本报告将 recall/precision 全记为 0，原因是 `runner` 的 citations 携带**完整 heading 路径**（如 `云舟商城退货政策（A 版）/退货时限`，由 Loader 的 heading stack 自 `#`/`##`/`###` 构造），而评测集黄金答案 `expected_relevant[].heading_path` 携带**裸二级标题**（如 `退货时限`）——二者无法按字符串精确相等配对，导致系统性的 0。

本次修复了**评分层匹配逻辑**（Runner 侧，未改黄金答案、未改语料结构）：一个返回对 `(document_key, heading_path)` 与期望对 `(document_key, expected_heading)` 匹配，当且仅当：

- `document_key` 相等；**且**
- `heading_path` 的最后一个 `/` 段（split on "/" 后的末段）与 `expected_heading` 完全相等（整体段级相等）。

精确相等仍优先（无 `/` 的 citation heading 直接等于期望 heading）；仅把期望文本作为前缀/子串包含、但末段不同的 heading（如 `退货时限延长期`、`普通退货时限`）不算命中。`hit_count` / precision / recall 其余语义保持不变。

修复后真实分数为 **recall@5 = 0.579、citation_precision = 0.155**（见第 2、3 节），验证了此前关于“检索链实际定位到正确文档与章节、0 分主要由评分约定导致”的推断——简单/mixed 类多为召回满分，检索质量本身有效。

### 5.2 当前真实失败样例（≥3，每个恰选一个主因）

| 样例 | 分类 | 主因 | 说明 |
|---|---|---|---|
| `multi-condition-compensation-01` | multi_condition_policy | retrieval | `recall@5=0.0`、`citation_precision=0.0`：期望的 2 个相关章节均未被 Top-5 召回。该 query 要求跨章节凑齐物流赔偿条件，固定 Top-K=5 的 Baseline 在 dense+sparse RRF 融合下未覆盖到黄金章节 —— 属真实检索缺漏。 |
| `multi-condition-return-01` | multi_condition_policy | retrieval | `recall@5=0.333`（期望 3 章节仅命中 1）、`citation_precision=0.2`：多条件 query 的证据分散在多个章节，top_k 预算与 RRF 排序只带回其中 1 个 —— 多条件召回的预算/融合瓶颈。 |
| `safety-unknown-exchange-01` | safety_no_answer | citation | `citation_precision=0.0`：无期望样例被 Baseline 返回 5 条非空 citations（未做到正确拒答），按规则计 0。这是阶段 A 固定 Top-K Baseline 的预期行为，正确拒答属阶段 B `EvidenceAssessor` 的 `INSUFFICIENT_EVIDENCE` 能力。 |
| `simple-return-window-01` | simple_policy | citation | `recall@5=1.0`（命中唯一黄金章节）但 `citation_precision=0.25`：Top-5 返回 4 条里仅 1 条是黄金答案——单章节召回正确，但非黄金 citations 拉低了精度，即“有据可依但非精确”的精度损耗，反映固定 Top-K 对引用精度的固有开销。 |

> 共性：召回分主要由多条件卡点的 retrieval 缺漏主导；精度分普遍偏低（0.2~0.25）是固定 Top-K（无论相关与否都返回 5 条）的既定行为——阶段 A 只测检索召回，不评答案依据度（见第 6 节）。

## 6. 阶段 A 边界声明

阶段 A 的 Baseline **不测量**（需要阶段 B 的生成/证据机制）：

- **answer accuracy（答案准确率）**：不在本报告范围内；
- **grounded answer rate（有据可依的回答率）**：需阶段 B 生成流程；
- **correct abstention（正确拒答率）**：需 `EvidenceAssessor` / `INSUFFICIENT_EVIDENCE`。

阶段 A 只测量检索召回，且本报告是其**对照基线**——阶段 B/C 的最终收益不应在此时出现，也不在此时要求。

## 7. 与最终 MVP 阈值的差距

- `retrieval_recall@5 = 0.579` vs 阈值 `>= 0.85`：未达标（差 0.271）。
- `citation_precision = 0.155` vs 阈值 `>= 0.95`：未达标。
- 差距来源已不再是评分约定不一致（已修复）：召回差距主要由多条件样例的检索缺漏（`multi-condition-compensation-01`）拉低；精度差距是固定 Top-K 非相关引用造成的 Baseline 固有开销。跨租户泄漏与基础设施均无问题（隔离 = 0）。

## 8. 结论与后续

- 跨租户隔离（`cross_tenant_leak_rate = 0`）与单轮固定预算 Baseline 语义在本真实运行中成立。
- 修复评分层 heading_path 匹配后，真实召回 **0.579** 表明检索链对“答案落在单章节”的查询准确（simple/mixed 类多为 1.0），剩余主要损失在多条件跨章节召回与固定 Top-K 的引用精度。
- 阶段 B 应优先补足多条件证据聚合与 `EvidenceAssessor` 的拒答/依据判定能力，再据此作为阶段 B/C 的有效对照组。
