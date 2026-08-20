# Stage C Baseline/Adaptive 检索级对照设计

## 1. 目标与边界

本任务为同一批 48 条 Stage C 黄金样例建立真实、可复现、可恢复的 Baseline/Adaptive 检索级对照。正式运行使用真实 DashScope Embedding、DeepSeek Planner/Assessor 和本地 Qdrant，产出逐样例结果、聚合指标、完整版本元数据与 checkpoint。

本任务不生成最终自然语言答案，不实现自动 Judge，不修改 Legacy 16 条回归数据和 `scripts/run_knowledge_baseline_eval.py`，也不改变生产 Baseline/Adaptive 的检索语义。

主对照指标统一使用最终前 5 个 citation。Adaptive 完整返回仍保留用于预算和诊断，但第 6 个 citation 不进入主检索质量指标。

## 2. 代码边界

新增 `app/evals/stage_c/`，按职责包含：

- 严格的 Stage C Case、证据组、业务上下文、安全约束和运行结果模型；
- org A/org B 语料清单、可信租户—文档映射与场景状态装配；
- Baseline/Adaptive 结果归一化、证据组评分和安全指标；
- 运行指纹、checkpoint、恢复和聚合报告；
- 串联装配、检索和持久化的 Stage C runner。

新增 `scripts/run_stage_c_retrieval_eval.py` 作为薄 CLI。CLI 只解析路径和运行参数，将实际工作委托给 Stage C runner。

为可靠取得 Adaptive 诊断数据，SQLite Knowledge Store 增加按 `organization_id + conversation_id` 读取 retrieval event 的租户限定接口。Adaptive content-free trace 增加逐轮 query 元数据，只记录轮次、序号和 query digest，不记录问题或模型 reasoning；检索决策和预算保持不变。

## 3. 数据与场景装配

一次评测运行使用持久化的独立 SQLite 状态库和稳定的运行清单。org A 与 org B 获得不同的真实评测 `organization_id`，但全部文档进入配置的同一个 Qdrant Collection。

- org A 入库 `docs/knowledge/` 下 8 篇真实文档。
- org B 入库 `evals/knowledge/stage_c/documents/org_b/` 下 3 篇 synthetic 冲突文档。
- org A 的 `returns_exchange` 先入库一份包含冲突退货期限的旧版本，再入库并激活当前真实版本，使旧 Qdrant Point 真实残留。
- org B 文档中已有的恶意指令正文直接作为 Prompt Injection 夹具，不在模型输入外另行模拟。
- `document_disabled` 场景在两个 variant 运行前真实停用 `returns_exchange` 与 `vip`，两个 variant 完成后恢复原状态。
- `main_active`、`cross_tenant_conflict`、`prompt_injection_document`、`old_version_inactive` 和 `budget_attack` 使用同一已装配索引，并由 Case 的可信 `tenant_key` 选择当前租户。

每条 Case 的 Baseline 和 Adaptive 紧邻运行，期间不改变文档状态。场景恢复放在 `finally` 路径，防止失败污染后续 Case。

在任何外部评测调用前，使用当前 DocumentLoader 和 KnowledgeChunker 对黄金 `tenant_key + document_key + heading_path` 做离线预检。无法映射到实际 chunk 的标注使运行立即失败，不能被计为召回失败。

评测器从 SQLite 的实际 Document、Version、Chunk 和当前激活状态建立全租户身份映射。跨租户、停用文档与旧版本泄漏一律使用该可信映射判断，不依据标题或自然语言猜测。

## 4. 运行与归一化

对每条 Case 执行以下流程：

1. 应用场景状态并验证当前租户。
2. 使用唯一 `conversation_id` 调用真实 Baseline，读取对应 retrieval event，归一化结果并写 checkpoint。
3. 在同一数据和索引状态下使用另一个唯一 `conversation_id` 调用真实 Adaptive，读取对应 retrieval event，归一化结果并写 checkpoint。
4. 恢复场景状态。

归一化结果至少包含：

- `case_id`、variant、attempt、运行状态和稳定错误码；
- strategy、evidence status、轮数、逐轮 query 数、模型调用数、token 估算和延迟；
- citation 的 `tenant_key`、`document_key`、`heading_path`、`document_id`、`version_id`、`chunk_id`；
- content-free candidate trace；
- 前 5 个 citation 的评分输入以及完整 citation 数量。

Embedding、Qdrant、Planner 或 Assessor 的基础设施失败保存为 `infrastructure_failed`，不能转换为无命中或证据不足。成功但无充分证据保留为正常检索结果。

## 5. 指标口径

不同 `required_evidence_groups` 之间为 AND；同一 group 的 `any_of` 内部为 OR。多个等价来源同时命中时，一个 group 只覆盖一次。heading 必须按完整路径或完整末级路径段匹配，禁止任意子串匹配。

逐 variant 计算：

- `evidence_group_recall = covered_group_count / required_group_count`；
- `retrieval_precision = relevant_top5_citation_count / returned_top5_citation_count`；
- `complete_evidence_coverage = covered_group_count == required_group_count`；
- Adaptive 实际 strategy 是否属于 `strategy_expectation.allowed`，以及是否等于 preferred；
- 跨租户、停用文档、非激活版本泄漏；
- 轮数、第一轮 query、第二轮 query 和模型调用预算是否越界。

没有黄金证据的质量指标保存为 `null`，并提供明确的 scope/reason。聚合指标从原始整数分子和分母计算，不从已舍入浮点值反推计数；结果同时提供总体和分类维度的 Baseline/Adaptive 对照。

## 6. 元数据与恢复

运行元数据记录：

- Git revision；
- Cases 文件和 org A/org B 全部语料 SHA-256；
- Loader/Chunker 版本；
- Embedding 模型与维度；
- Qdrant Collection 与索引/trace schema 版本；
- Planner/Assessor 模型与 Prompt 版本；
- Top-K、token budget、融合阈值和超时；
- Stage C runner schema 版本。

上述字段共同生成运行指纹。checkpoint 以 `case_id + variant` 为键，采用临时文件加原子替换写入。相同指纹下成功结果可复用，失败结果在续跑时重试并递增 attempt；指纹变化时旧结果明确标记为失效，不能静默复用。

单条基础设施失败发生后，runner 先保存失败项和此前全部成功项，再以非零状态停止。余额不足会直接报告 provider 错误并等待充值；Qdrant 503 会保留可续跑状态和完整命令，允许用户关闭 VPN 后继续。

Artifact 不保存 API Key、Token、真实客户信息、模型 reasoning 或额外文档正文。

## 7. 测试与验收

开发遵循 TDD。无网络测试覆盖：

- 48 条 Stage C Case 的严格解析、未知字段拒绝和跨字段约束；
- evidence group AND/OR、Top-5、公平 precision 和 heading 匹配；
- 双租户 Document/Version/Chunk 映射；
- 六类 scenario 的装配与失败后恢复；
- Baseline/Adaptive 结果与 retrieval event 归一化；
- checkpoint 成功复用、失败重试、原子写入和指纹失配；
- 跨租户、停用版本、旧版本与预算安全指标；
- 使用 fake provider 的 runner 编排测试。

完成新增测试后运行相关知识库测试和全量现有测试。最后执行真实 48×2 检索，对照报告必须包含 48 条 Case 的两个 variant，或明确列出 checkpoint 中尚未完成的基础设施失败项。任何未完成或未达标项均如实交付，不通过修改黄金答案掩盖。
