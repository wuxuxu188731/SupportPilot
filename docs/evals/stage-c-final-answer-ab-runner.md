# Stage C 最终答案 Baseline/Adaptive A/B Runner

## 目的

该 Runner 使用 `evals/knowledge/stage_c/cases.jsonl` 的同一批 48 条用例，分别通过传统 RAG（Baseline）和 Agentic Search（Adaptive）生成最终客服答案。两个变体固定使用：

- 同一个回答模型；
- 同一个 `SUPPORT_SYSTEM_PROMPT`；
- 同一组只读业务工具 `get_order`、`get_logistics`；
- 同一个知识工具名与参数 Schema；
- 同一份冻结业务事实和场景知识库；
- 同一套引用校验规则。

唯一有意变化的是 `search_knowledge` 内部连接的检索服务。

## 运行

先确保 Qdrant 可访问，并配置 `DEEPSEEK_API_KEY`、`DASHSCOPE_API_KEY`。然后执行：

```powershell
python scripts/run_stage_c_answer_ab_eval.py `
  --database .artifacts/stage-c-answer-ab/state.db `
  --cases evals/knowledge/stage_c/cases.jsonl `
  --checkpoint .artifacts/stage-c-answer-ab/checkpoint.json `
  --output .artifacts/stage-c-answer-ab/report.json `
  --model deepseek-v4-flash
```

Runner 在每个答案完成后立即原子写入 checkpoint。相同数据、语料、模型、Prompt、工具和集合配置会续跑缺失项；指纹变化会拒绝误用旧结果。

## 产物与口径

报告保存最终答案、服务端认可的结构化 citations、公开工具轨迹、引用完整性、回答模型调用次数、真实 usage 和延迟，不保存模型 reasoning。

每条用例的两个答案还会进行位置盲化的成对评审。评审依据为参考答案、原子关键事实、预期回答行为、安全约束，以及各答案实际获得的 citation 内容。聚合结果提供：

- Baseline / Adaptive / 平局次数；
- 分类胜负；
- 平均 Judge 总分；
- 有效引用答案数与 `answer_incomplete` 数；
- 回答 Agent 的模型调用、tokens 和端到端延迟。这里的 tokens 不包含
  Adaptive 内部 Planner/Assessor；检索编排成本以检索级报告为准。

成对 Judge 是自然语言质量的简单自动评估，不替代后续人工校准；租户、版本和引用 ID 等安全结论仍应优先采用确定性检查。
