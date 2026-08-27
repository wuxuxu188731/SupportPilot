# Stage C 双方 Rerank 最终答案 A/B 结果

## 结论

传统 RAG 与 Agentic Search 均使用 `qwen3-rerank` 后，两者的最终答案质量仍然
基本持平。Agentic Search 在 48 组成对盲评中取得 8 胜，传统 RAG 取得 4 胜，
另有 36 平；但传统 RAG 的平均 Judge 总分略高，为 14.938/16，Agentic Search
为 14.854/16。两种统计口径方向相反，说明当前样本没有呈现稳定、显著的质量差距。

Agentic Search 的平均端到端延迟为 16.08 秒，传统 RAG 为 12.00 秒，前者慢约
34.0%。因此，当前仍建议以带 rerank 的传统 RAG 作为默认方案，把 Agentic Search
用于经过验证确实能受益的复杂问题。

## 对照条件

- 用例：`evals/knowledge/stage_c/cases.jsonl`，共 48 条；
- 答案：传统 RAG 48 条、Agentic Search 48 条，共 96 条；
- 成对评审：48/48 完成；
- 回答模型与 Judge：`deepseek-v4-flash`；
- 最终答案 Prompt：`support-system-prompt-stage-c-answer-v1`；
- Rerank：双方均使用相同的 `qwen3-rerank` 模型和 instruction；
- 业务工具：双方使用相同的 `get_order`、`get_logistics` 冻结夹具；
- A/B 标签：按 case ID 确定性打乱，Judge 不知道变体名称。

## 总体指标

| 指标 | 传统 RAG | Agentic Search | 说明 |
|---|---:|---:|---|
| 成对胜出 | 4 | 8 | 36 组平局 |
| 平均 Judge 总分 | 14.938/16 | 14.854/16 | 传统 RAG 高 0.084 分 |
| 事实正确性 | 3.833/4 | 3.875/4 | Agentic 略高 |
| 关键事实完整性 | 3.708/4 | 3.667/4 | 传统 RAG 略高 |
| 引用支撑度 | 3.604/4 | 3.542/4 | 传统 RAG 略高 |
| 行为符合度 | 3.792/4 | 3.771/4 | 基本持平 |
| 带合法 citation 的答案 | 43/48 | 40/48 | 只表示 citation ID 合法 |
| `answer_incomplete` | 0 | 0 | 两边均无协议层不完整标记 |
| 平均端到端延迟 | 12.00 秒 | 16.08 秒 | Agentic 慢 34.0% |
| 延迟中位数 | 12.06 秒 | 15.98 秒 | Agentic 慢 32.6% |
| P95 延迟 | 17.87 秒 | 24.44 秒 | Agentic 慢 36.8% |
| 回答 Agent 平均模型调用 | 2.04 | 1.96 | 不含 Planner/Assessor |
| 回答 Agent 平均 tokens | 3,726 | 3,756 | 不含 Planner/Assessor |

## 分类胜负

| 类别 | 传统 RAG 胜 | Agentic Search 胜 | 平 |
|---|---:|---:|---:|
| 简单政策 | 0 | 0 | 12 |
| 多条件政策 | 1 | 4 | 7 |
| 事实与政策混合 | 2 | 1 | 9 |
| 安全场景 | 1 | 3 | 8 |

Agentic Search 的胜例主要集中在多条件政策和安全场景，但并不稳定：混合场景中
传统 RAG 反而多胜一例，简单政策全部打平。12 个非平局样例只占全集的 25%，
不宜把 8:4 解读为已经证明 Agentic Search 全局更优。

## 与上一轮的关系

上一轮是“传统 RAG 无 rerank、Agentic Search 有 rerank”，结果为 Agentic 8 胜、
传统 RAG 5 胜、35 平，平均分分别为 15.208 和 15.104。本轮给传统 RAG 增加
rerank 后，结果变为 8 胜、4 胜、36 平，且传统 RAG 平均分略高。

不能把两轮分数变化直接归因于 rerank：回答模型和 Judge 都存在采样波动，第二轮
也重新生成了 Agentic 答案。更可靠的结论是，两轮都出现大量平局，Agentic Search
没有在这 48 条最终答案上形成足以覆盖额外延迟与复杂度的稳定优势。若要精确估计
Baseline rerank 的因果增益，应固定检索结果或答案，并进行多随机种子重复评测。

## 建议

1. 生产默认使用带 rerank 的传统 RAG。
2. Agentic Search 保持复杂问题的受控路由，优先研究 4 个多条件胜例的共同特征。
3. 对 12 个非平局样例进行人工复核，避免单一 LLM Judge 的偏差。
4. 下一轮至少重复 3 个随机种子，并报告均值、方差和人工校准结果。

## 数据来源

完整逐答案、公开工具轨迹和逐题 Judge 结果位于：
`.artifacts/stage-c-answer-ab-both-rerank/report.json`。恢复状态位于：
`.artifacts/stage-c-answer-ab-both-rerank/checkpoint.json`。
