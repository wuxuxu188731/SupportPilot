# Stage C 双方使用相同 Rerank 的检索评测报告（2026-09-06）

## 1. 结论

在传统 RAG 与 Agentic Search 都使用同一个 `qwen3-rerank`、同一条 rerank
指令、同一批 48 个 Stage C 问题和同一个 Top-5 评分口径时：

- 传统 RAG 的证据组召回率为 **57/67，85.07%**；
- Agentic Search 的证据组召回率为 **59/67，88.06%**；
- 传统 RAG 的完整证据覆盖率为 **31/40，77.50%**；
- Agentic Search 的完整证据覆盖率为 **32/40，80.00%**。

Agentic Search 比传统 RAG 多召回 2 个证据组，证据组召回率高 2.99 个百分点；
同时多完整覆盖 1 个可测问题，完整证据覆盖率高 2.50 个百分点。这个优势主要来自
多条件政策和事实/政策混合问题；简单政策与带黄金证据的安全问题两者均为满召回。

代价是 Agentic Search 的平均延迟为 12.02 秒，比传统 RAG 的 7.35 秒高约
63.4%；P95 延迟为 18.56 秒，约为传统 RAG 9.03 秒的 2.06 倍。因此，本轮结果
支持“复杂问题受控使用 Agentic Search”，但还不足以支持把所有请求默认切换到
Agentic Search。

## 2. 评测范围与配置

- 运行时间：2026-09-06；
- Git revision：`d697da6e7611cf7bf46f7d33a2471790b3bfe198`；
- 评测用例：`evals/knowledge/stage_c/cases.jsonl`，共 48 条；
- 可测召回用例：40 条，共 67 个必需证据组；
- 运行变体：传统 RAG 48 条、Agentic Search 48 条，共 96 条；
- 完成情况：96/96 完成，0 个最终基础设施失败，0 个缺失变体；
- Embedding：`text-embedding-v4`，1024 维；
- 向量库：Qdrant collection `supportpilot_knowledge_te4_1024_v1`；
- Rerank：双方共享同一个 `qwen3-rerank` 实例；
- Rerank 输入：只发送经 SQLite 二次校验后的 chunk 正文；
- Rerank 指令：`Given a web search query, retrieve relevant passages that answer the query.`；
- Planner/Assessor：`deepseek-v4-flash`，仅 Agentic Search 使用；
- 候选召回数：每个查询 8 个；
- 评分范围：最终前 5 个 citation；Agentic Search 的第 6 个 citation 不参与评分；
- 证据 token 预算：3000；
- Agentic Search 最大搜索轮数：2；
- 单次搜索超时：30 秒。

本轮使用的命令如下：

```text
python scripts/run_stage_c_retrieval_eval.py --database .artifacts/stage-c-retrieval-both-rerank-20260906/state.db --cases evals/knowledge/stage_c/cases.jsonl --checkpoint .artifacts/stage-c-retrieval-both-rerank-20260906/checkpoint.json --output .artifacts/stage-c-retrieval-both-rerank-20260906/report.json
```

完整逐题结果位于：
`.artifacts/stage-c-retrieval-both-rerank-20260906/report.json`。续跑状态位于：
`.artifacts/stage-c-retrieval-both-rerank-20260906/checkpoint.json`。

## 3. 指标口径

### 3.1 证据组召回率

每个问题定义零个或多个 `required_evidence_groups`：

- 不同证据组之间是 AND，问题要求的每一组都属于应召回证据；
- 同一证据组的 `any_of` 是 OR，召回其中任一等价来源即覆盖该组；
- 多个等价 citation 同时命中同一组时，该组仍只计一次；
- 只使用最终前 5 个 citation 评分；
- citation 必须通过可信身份、租户、启用状态、激活版本、`document_key` 和完整章节路径校验。

单题召回率为：

```text
covered_group_count / required_group_count
```

总体证据组召回率按原始计数进行微平均：

```text
所有可测问题覆盖的证据组总数 / 所有可测问题要求的证据组总数
```

没有黄金证据组的 8 个安全问题，其召回率、检索精确率和完整证据覆盖率为
`null`，不进入质量指标分母。

### 3.2 检索精确率

检索精确率为：

```text
前 5 个 citation 中相关 citation 数 / 前 5 个 citation 总数
```

同一个 citation 即使覆盖多个证据组，在精确率分子中也只计一次。

### 3.3 完整证据覆盖率

一个问题只有在所有必需证据组均被覆盖时，才算完整覆盖。总体完整证据覆盖率为：

```text
完整覆盖问题数 / 带黄金证据组的问题数
```

因此，证据组召回率衡量全部证据组的总体命中比例，完整证据覆盖率则对任何局部漏召回
更敏感。

## 4. 总体核心指标

| 指标 | 传统 RAG | Agentic Search | 差异与判断 |
|---|---:|---:|---|
| 证据组召回率 | 57/67，85.07% | 59/67，88.06% | Agentic +2.99 个百分点 |
| 检索精确率 | 64/200，32.00% | 67/200，33.50% | Agentic +1.50 个百分点 |
| 完整证据覆盖率 | 31/40，77.50% | 32/40，80.00% | Agentic +2.50 个百分点 |
| 平均延迟 | 7.35 秒 | 12.02 秒 | 传统 RAG 更快；Agentic 高约 63.4% |
| 延迟中位数 | 7.27 秒 | 9.48 秒 | 传统 RAG 更快 |
| P95 延迟 | 9.03 秒 | 18.56 秒 | Agentic 约为传统 RAG 的 2.06 倍 |
| 平均记录 tokens | 589 | 2,391 | Agentic 约为传统 RAG 的 4.06 倍 |
| 平均搜索轮数 | 1.00 | 1.02 | Agentic 仅 1 个用例进入第二轮 |
| 平均 Planner/Assessor 调用 | 0 | 2.02 | 不包含双方共有的 rerank 调用 |
| 首选策略命中率 | 不适用 | 32/39，82.05% | 仅 Agentic 有策略预期 |
| 预算耗尽 | 0 | 0 | 相同 |
| 安全泄漏 | 0 | 0 | 相同 |

“平均记录 tokens”和“平均 Planner/Assessor 调用”采用当前 runner 的记录口径。
双方都会执行 rerank，但当前 `model_calls` 字段只记录 Agentic 的 Planner/Assessor，
不能用该字段比较双方的全部外部请求次数。

## 5. 分类结果

| 问题类型 | 传统 RAG 召回率 | Agentic 召回率 | 传统 RAG 完整覆盖率 | Agentic 完整覆盖率 | 传统 RAG 精确率 | Agentic 精确率 |
|---|---:|---:|---:|---:|---:|---:|
| 简单政策 | 12/12，100.00% | 12/12，100.00% | 12/12，100.00% | 12/12，100.00% | 21.67% | 21.67% |
| 多条件政策 | 26/32，81.25% | 27/32，84.38% | 7/12，58.33% | 7/12，58.33% | 46.67% | 50.00% |
| 事实与政策混合 | 15/19，78.95% | 16/19，84.21% | 8/12，66.67% | 9/12，75.00% | 28.33% | 30.00% |
| 安全场景 | 4/4，100.00% | 4/4，100.00% | 4/4，100.00% | 4/4，100.00% | 30.00% | 30.00% |

分类结果表明：

1. 简单政策问题没有从 Agentic 规划中获得额外召回收益；
2. 多条件政策中 Agentic 多覆盖 1 个证据组，但没有增加完整覆盖问题数；
3. 事实与政策混合问题中 Agentic 多覆盖 1 个证据组，并多完整覆盖 1 个问题；
4. 带黄金证据的安全场景两者均满召回，所有安全泄漏计数也均为 0。

## 6. 逐题差异

40 个可测问题中，Agentic Search 的单题证据组召回率胜出 3 题，传统 RAG 胜出
1 题，另外 36 题持平。

| 用例 | 传统 RAG | Agentic Search | 胜出方 |
|---|---:|---:|---|
| `multi-compensation-amount-limit-exclusion-01` | 2/4，50.00% | 3/4，75.00% | Agentic |
| `mixed-logistics-delay-shipment-01` | 2/2，100.00% | 1/2，50.00% | 传统 RAG |
| `mixed-logistics-delay-diamond-01` | 2/3，66.67% | 3/3，100.00% | Agentic |
| `mixed-member-return-diamond-01` | 1/2，50.00% | 2/2，100.00% | Agentic |

这说明 Agentic Search 的总体优势不是广泛分布在全部问题上，而是集中在少量复杂问题。
同时，查询改写和多查询融合也可能让个别问题退化，因此不能把 Agentic 视为对传统
RAG 的单调增强。

## 7. 安全、预算与运行完整性

两种方案均满足以下结果：

- 跨租户泄漏：0；
- 停用文档泄漏：0；
- 非激活版本泄漏：0；
- 未知 chunk 身份：0；
- 预算耗尽：0；
- 硬限制违规：0；
- 用例安全预期违规：0。

本轮第一次执行过程中有 1 个 Agentic 变体发生可恢复的外部基础设施异常。运行器在
checkpoint 中保存了状态，随后从同一 checkpoint 重试成功；最终报告为 96/96 完成，
该异常没有作为检索未命中进入任何质量指标。

## 8. 与历史数字的关系

`docs/agentic-search-optimization-roadmap.md` 记录的历史最佳检索报告为：传统 RAG
83.58%（56/67）、Agentic Search 91.04%（61/67）。该轮实验发生在传统 RAG 接入
rerank 之前，双方排序条件不同，不能继续作为当前公平对照。

本轮双方使用相同 rerank 后，结果更新为：传统 RAG 85.07%（57/67）、Agentic
Search 88.06%（59/67）。与历史运行之间的变化不能全部归因于 rerank，因为
Embedding、Planner、Assessor 和外部 rerank 服务都可能存在运行波动；本报告的价值
在于给出了当前代码、当前语料和相同 rerank 条件下的一次完整可审计对照。

项目其他文档中出现的“传统 RAG 85%、Agentic Search 90.5%”缺少与当前 Git
revision 对应的完整检索产物。本轮报告以整数分子/分母为准，应使用 85.07% 和
88.06% 作为当前最新单次实测结论。

## 9. 生产建议

1. 默认链路继续使用带 rerank 的传统 RAG。它在本轮只少覆盖 2/67 个证据组，但平均
   延迟低 4.66 秒，P95 延迟低 9.53 秒，成本和稳定性更容易控制。
2. 对多条件政策和事实/政策混合问题采用受控的 Agentic 路由。Agentic 的质量收益
   主要集中在这些类别，尤其是会员权益、物流赔偿和金额/上限/例外组合问题。
3. 不要把 Agentic Search 当作必然提高召回的后处理步骤；应持续监控查询改写导致的
   退化用例，例如本轮的 `mixed-logistics-delay-shipment-01`。
4. 下一轮至少重复 3 次相同配置，报告均值、方差和逐题稳定性，再判断 2.99 个百分点
   的召回优势是否足以覆盖约 63.4% 的平均延迟增幅。
5. 后续应把 rerank 模型、指令和输入格式纳入 runner metadata/fingerprint，并单独
   记录 rerank 调用次数、输入 tokens 与延迟，使报告能够仅依赖产物完成配置审计。
