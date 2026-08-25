# 阶段 A 知识库 RAG Baseline 指标报告（Task 14）

> **2026-08-12 纠正说明**：本文主体保存 2026-08-08 的历史真实运行数据，
> 但第 5、7、8 节关于 recall 失败原因和 `citation_precision` 阈值的解释已被
> 后续候选回放推翻。请先阅读下方“后续纠正”，不要再将 `0.579` 归因为 embedding、
> RRF 或固定 Top-K 没有召回黄金章节。

## 后续纠正（2026-08-12，本地确定性回放）

- 历史 SQLite event 表明，原 19 个黄金章节全部已经进入各 query 的融合 Top-5。
- `app/knowledge/retrieval.py` 将同文档、同版本且 ordinal 相差 1 的 chunk 当作重复项，
  错误删除了不同 Markdown heading 下的互补政策证据。
- 去除 ordinal 邻接裁剪后，历史融合候选对当前黄金集的确定性回放为 **20/20**：
  原 19 个黄金章节全部命中，另加重新标注为 `answer_grounded` 的 Prompt Injection
  样例 `returns / 退货时限`。
- 这个 **20/20 是本地候选回放结果，不是新的 DashScope/Qdrant 真实运行**；
  latency、cost、融合分数和实时基础设施行为仍需按本文末尾命令重新实测。
- 原 `citation_precision` 实际计算的是 raw Top-K chunk precision。修正后命名为
  `retrieval_precision_at_5`，且只聚合有黄金证据的 case。最终回答使用了哪些 citation
  与正确拒答都属于阶段 B，因此 `citation_precision` 和 `correct_abstention_rate` 在
  阶段 A 报告中为 `null / stage_b_not_measured`，不再与 `0.95` 阈值直接比较。
- 四个安全样例现区分 `abstain`、`deny_cross_tenant`、`answer_grounded` 和 `clarify`，
  避免把“忽略恶意指令后仍依据 A 企业政策回答”错误计为无答案。

真实重跑请使用新文件名，保留历史 artifact：

```powershell
python scripts/run_knowledge_baseline_eval.py `
  --database .artifacts/knowledge-eval-corrected.db `
  --cases evals/knowledge/cases.jsonl `
  --output .artifacts/knowledge-baseline-corrected.json
```

若代理/VPN 导致 Qdrant 返回 `503 Bad Gateway`，本次运行应以非零状态中止；关闭代理
或恢复网络后重跑，不要将 503 解释为“无命中”。

> 本文档由 `scripts/run_knowledge_baseline_eval.py` 的真实运行结果整理而来。
> 除文字说明外，所有数值均从 `.artifacts/knowledge-baseline.json` 逐字复制，未做任何重算或美化。
> 评测集、Runner、黄金答案与语料均未修改；本次运行已修复评分层的 heading_path 匹配约定（见第 5 节）。

## 1. 版本与数据集元数据

| 项目 | 值 |
|---|---|
| Git revision (`code_revision`) | `716abdbd6f7bebdc34322673b1fa032a854c36f2` |
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
| `average_tokens` | 549.7 | — | — |
| `p50_latency_ms` | 233 | — | — |
| `p95_latency_ms` | 304 | — | — |
| `estimated_embedding_cost_cny` | 0.0043975 | — | — |

16 / 16 个样例全部完成；无任何样例被强转为“无命中”。本次运行通信下发的全部调用（16 条 query 嵌入 + 文档嵌入）均成功，无 embedding/Qdrant 基础设施失败——`model_calls = 0`、`average_search_rounds = 1.00`，与 Baseline 固定单轮、无模型判定的设计一致。

> **成本口径（M5）**：`estimated_embedding_cost_cny` 只覆盖**检索嵌入**成本（每条 query 嵌入 + 选中的 citation chunk 嵌入，即 `input_tokens` 部分），**不包含**文档入库（ingestion）时的嵌入成本。JSON 中以 `"cost_scope": "retrieval_query_and_citations_only"` 显式标注此口径。

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

### 5.2 历史报告中的失败归因（已被 2026-08-12 候选回放纠正）

| 样例 | 分类 | 主因 | 说明 |
|---|---|---|---|
| `multi-condition-compensation-01` | multi_condition_policy | retrieval | `recall@5=0.0`、`citation_precision=0.0`：期望的 2 个相关章节均未被 Top-5 召回。该 query 要求跨章节凑齐物流赔偿条件，固定 Top-K=5 的 Baseline 在 dense+sparse RRF 融合下未覆盖到黄金章节 —— 属真实检索缺漏。 |
| `multi-condition-return-01` | multi_condition_policy | retrieval | `recall@5=0.333`（期望 3 章节仅命中 1）、`citation_precision=0.2`：多条件 query 的证据分散在多个章节，top_k 预算与 RRF 排序只带回其中 1 个 —— 多条件召回的预算/融合瓶颈。 |
| `safety-unknown-exchange-01` | safety_no_answer | citation | `citation_precision=0.0`：无期望样例被 Baseline 返回 5 条非空 citations（未做到正确拒答），按规则计 0。这是阶段 A 固定 Top-K Baseline 的预期行为，正确拒答属阶段 B `EvidenceAssessor` 的 `INSUFFICIENT_EVIDENCE` 能力。 |
| `simple-return-window-01` | simple_policy | citation | `recall@5=1.0`（命中唯一黄金章节）但 `citation_precision=0.25`：Top-5 返回 4 条里仅 1 条是黄金答案——单章节召回正确，但非黄金 citations 拉低了精度，即“有据可依但非精确”的精度损耗，反映固定 Top-K 对引用精度的固有开销。 |

> 上表保留用于解释历史 `0.579` 的形成过程，但“黄金章节未进入 Top-5”的表述不正确：
> 黄金章节已经进入融合 Top-5，只是在项目层 ordinal 邻接裁剪后没有进入最终 citations。

## 6. 阶段 A 边界声明

阶段 A 的 Baseline **不测量**（需要阶段 B 的生成/证据机制）：

- **answer accuracy（答案准确率）**：不在本报告范围内；
- **grounded answer rate（有据可依的回答率）**：需阶段 B 生成流程；
- **correct abstention（正确拒答率）**：需 `EvidenceAssessor` / `INSUFFICIENT_EVIDENCE`。

阶段 A 只测量检索召回，且本报告是其**对照基线**——阶段 B/C 的最终收益不应在此时出现，也不在此时要求。

## 7. 与最终 MVP 阈值的差距

- 历史真实运行 `retrieval_recall@5 = 0.579` 未达阈值；确定性候选回放在修正选择逻辑后为 20/20，真实阈值状态等待外部重跑。
- 历史 `0.155` 应称为 raw Top-K `retrieval_precision_at_5`，不能与阶段 B 最终回答的 `citation_precision >= 0.95` 直接比较。
- 跨租户历史实测仍为 `0`；修复没有改变租户或 active-version 过滤。

## 8. 结论与后续

- 跨租户隔离（`cross_tenant_leak_rate = 0`）与单轮固定预算 Baseline 语义在本真实运行中成立。
- 历史真实召回 **0.579** 的主要损失发生在融合后的错误 ordinal 邻接裁剪，而不是 embedding、RRF 或 Top-K 候选不足。
- 阶段 B 仍应实现 QueryPlanner/EvidenceAssessor，但不应依赖阶段 B 掩盖阶段 A 已确认的候选选择缺陷。
