# Stage C 最终答案 Baseline/Adaptive A/B 结果

## 结论

在相同的 48 条用例、回答模型、系统 Prompt、业务工具、业务事实和引用规则下，
**最终答案质量基本持平，Adaptive 仅有非常轻微的自动评审优势；综合延迟、检索可靠性和复杂度后，仍建议 Baseline 作为当前默认方案。**

Adaptive 在 48 组成对盲评中取得 8 胜，Baseline 取得 5 胜，另有 35 平。
Adaptive 平均 Judge 总分为 15.208/16，Baseline 为 15.104/16，差值只有
0.104 分。13 个非平局中的 8:5 分布不足以说明稳定优势；以等概率胜负作简单
二项检验，双侧 p 值约为 0.58。

与此同时，Adaptive 的平均端到端回答延迟为 15.67 秒，Baseline 为 10.61 秒，
Adaptive 慢约 47.7%。因此当前不应仅凭 8:5 的小幅胜差把生产默认切换到
Adaptive。

## 运行范围

- 用例：`evals/knowledge/stage_c/cases.jsonl`，共 48 条；
- 答案：Baseline 48 条、Adaptive 48 条，共 96 条；
- 成对评审：48/48 完成；
- 回答模型与 Judge：`deepseek-v4-flash`；
- 知识依赖：DashScope Embedding/Rerank、Qdrant、DeepSeek Planner/Assessor；
- 最终答案 Prompt：两个变体完全相同；
- 业务工具：两个变体均只使用相同的 `get_order`、`get_logistics` 冻结夹具；
- 位置偏差控制：每条用例按 case ID 确定性打乱 A/B 标签，Judge 不知道变体名称；
- reasoning：不写入 artifact。

## 总体指标

| 指标 | Baseline | Adaptive | 说明 |
|---|---:|---:|---|
| 成对胜出 | 5 | 8 | 35 组平局 |
| 平均 Judge 总分 | 15.104/16 | 15.208/16 | 差 0.104 分 |
| 事实正确性 | 3.92/4 | 3.92/4 | 持平 |
| 关键事实完整性 | 3.69/4 | 3.77/4 | Adaptive 略高 |
| 引用支撑度 | 3.65/4 | 3.67/4 | 基本持平 |
| 行为符合度 | 3.85/4 | 3.85/4 | 持平 |
| 带合法 citation 的答案 | 45/48 | 44/48 | 仅表示 citation ID 合法 |
| `answer_incomplete` | 0 | 0 | 无未知 ID 或充分证据完全漏引 |
| 平均端到端延迟 | 10.61 秒 | 15.67 秒 | Adaptive 慢 47.7% |
| 延迟中位数 | 10.46 秒 | 13.70 秒 | Adaptive 慢 31.0% |
| P95 延迟 | 14.94 秒 | 25.14 秒 | Adaptive 慢 68.2% |
| 回答 Agent 平均模型调用 | 2.12 | 2.08 | 不含 Planner/Assessor |
| 回答 Agent 平均 tokens | 3,948 | 4,015 | 不含 Planner/Assessor |
| Canary 出现在最终答案 | 0 | 0 | 两边均未命中 Canary 字符串 |

端到端延迟包含知识检索和回答工具循环。回答 Agent 的调用与 tokens 只统计最终
回答循环，Adaptive 内部 Planner/Assessor 的额外模型调用不在该 token 数中；检索
级成本应结合现有 Stage C 检索报告查看。

## 分类结果

| 类别 | Baseline 胜 | Adaptive 胜 | 平 | Baseline 平均分 | Adaptive 平均分 |
|---|---:|---:|---:|---:|---:|
| 简单政策 | 0 | 1 | 11 | 15.83 | 15.92 |
| 多条件政策 | 2 | 2 | 8 | 14.42 | 14.58 |
| 事实与政策混合 | 1 | 2 | 9 | 15.58 | 15.75 |
| 安全场景 | 2 | 3 | 7 | 14.58 | 14.58 |

Adaptive 的小幅优势没有集中体现为某一类问题上的稳定碾压。安全场景的平均分
完全相同，但逐题波动明显：Adaptive 在 Prompt Injection 拒答样例上显著更好，
却在“要求拆成十个查询”的预算攻击样例上显著更差。Baseline 也存在个别无答案
场景处理不佳的问题。

## 如何理解与检索级报告的差异

历史检索级报告中 Baseline 曾明显高于当时的 Adaptive；该结果来自 Planner 与
rerank 优化前的版本。当前版本的证据召回率已更新为 Baseline 85%、Agentic Search
90.5%，不能再用历史召回差距解释本次最终答案结果。最终答案评测仍出现大量平局，
主要有三个原因：

1. 回答模型能够基于少量但足够的证据生成相同核心结论；
2. 无答案、拒答和澄清用例不以召回更多内容为优势；
3. 最终 Judge 同时评价事实、完整性、引用和行为，而不是只评价 Top-5 召回。

这不代表检索差距不重要。Adaptive 的长尾延迟更高，且逐题质量波动更大；当前
48 条结果不足以证明它能稳定抵消额外 Planner、Assessor、Reranker 和多轮查询成本。

## 建议

1. 生产默认继续使用 Baseline。
2. Adaptive 保持实验或受控路由，只用于已验证会受益的复杂问题。
3. 对 13 个非平局样例做人工复核，优先检查两个大幅反转的安全样例。
4. 为 Judge 建立人工校准集；当前结果是自动评审，不应被视为最终人工真值。
5. 优化 Adaptive 后使用同一 checkpoint 指纹规则重新运行，重点观察：
   - 非平局胜率是否稳定提高；
   - 多条件与混合问题是否形成可重复优势；
   - P95 延迟是否下降；
   - 预算攻击、无答案和 Prompt Injection 是否不再出现大幅退化。

## 数据来源

完整逐答案、公开工具轨迹和逐题 Judge 结果位于：
`.artifacts/stage-c-answer-ab/report.json`。恢复状态位于：
`.artifacts/stage-c-answer-ab/checkpoint.json`。
