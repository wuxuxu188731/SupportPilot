# Agentic Search 待优化方向

## 1. 当前基线

Stage C 共包含 48 个用例、67 个必需证据组。当前效果最好的 Agentic Search 配置为：

- embedding 与 Qdrant dense+sparse RRF 每个查询召回 8 个 chunk；
- 仅对 SQLite 二次校验后的 chunk 正文调用 `qwen3-rerank`；
- 使用通用问答检索指令：`Given a web search query, retrieve relevant passages that answer the query.`；
- 多查询结果按 rerank score 聚合，最多选择 6 个 chunk、总计不超过 3000 token；
- Assessor 判断证据是否充分，仅 `MULTI` 策略允许进入第二轮补充检索。

当前最佳报告为 `.artifacts/stage-c-retrieval-rerank/report.json`，核心指标如下：

| 指标 | 传统 RAG | Agentic Search |
|---|---:|---:|
| 证据组召回率 | 83.58%（56/67） | 91.04%（61/67） |
| 检索精确率 | 31.50% | 34.00% |
| 完整证据覆盖率 | 75.00% | 85.00% |
| 平均延迟 | 6.53 秒 | 12.78 秒 |

Agentic Search 的主要收益来自多条件政策和事实/政策混合问题，代价是更多 Planner、Embedding、Qdrant、Rerank 与 Assessor 调用。

## 2. 已验证但不应保留的方向

### 2.1 在 rerank 输入中拼接文档标题和章节路径

已分别测试：

1. `标题 + 章节 + 正文` 配合售后政策领域 instruct；
2. `标题 + 章节 + 正文` 配合原通用问答 instruct。

第二组实验排除了领域 instruct 的主要干扰，Agentic 证据组召回率仍从 91.04% 降至 85.07%，且没有用例获得提升。因此当前实现应继续只向 rerank 模型发送 chunk 正文。

### 2.2 直接增加最终 Top-K 或 token budget

Stage C 只评价最终前 5 个 citation。单纯增加最终返回数量不能解决前五名的排序问题，还会增加 Assessor 和最终回答上下文成本，因此不应作为首选方案。

### 2.3 首轮固定执行 3 个查询

Planner 当前将 `MULTI` 首轮限制为 2 个查询，是为了避免 30 秒预算耗尽。固定恢复 3 个查询会显著增加 embedding、Qdrant 和 rerank 成本，只有在低成本方案验证不足后才应考虑。

## 3. 推荐优化方向

### P1：强化 Assessor 的证据完整性判断

#### 问题

当前仍未完整召回的 6 个用例均被 Assessor 判断为 `SUFFICIENT`，所以第二轮检索一次都没有触发。现有提示词没有明确要求分别核对问题中的条件、例外、金额、时限和政策优先级，容易把“主题相关”误判为“已直接回答”。

#### 建议

- 要求 Assessor 将问题拆成原子方面；
- 每个方面必须由具体 chunk 直接支持；
- 数值、条件、例外、时限和政策冲突优先级分别判断；
- 缺失时生成可独立检索、包含业务关键词的 follow-up query；
- 将原始用户问题一并传给 Assessor，而不只传 Planner 查询。

#### 改动范围

- `app/knowledge/evidence.py`：修改 prompt 与 `assess()` 输入；
- `app/knowledge/service.py`：传递原始问题；
- `tests/knowledge/test_evidence.py`；
- `tests/knowledge/test_adaptive_service.py`。

#### 成本与预期

代码改动小；仅在真正缺证据时增加第二轮 embedding、Qdrant、rerank 和 assessor 调用。它可能改善 5 个当前错误提前停止的 `MULTI` 用例，能效比较高。

### P1：按查询覆盖融合多查询结果

#### 问题

当前直接比较不同 rerank 请求返回的 `rerank_score`。不同 query 的分数未必处于可直接比较的尺度，一个 query 的候选可能占满 Top-6，挤掉另一个 query 的重要证据。

#### 建议

- 使用每个 query 内部的 `rerank_rank` 做 Reciprocal Rank Fusion；或
- 先保证每个 planned query 至少贡献 1～2 个候选，再按融合分数填充剩余位置；
- 命中多个查询的 chunk 获得额外融合权重；
- trace 同时保留 query 内排名和最终融合排名。

#### 改动范围

- `app/knowledge/evidence.py`：扩展 `RoundEvidence` 排名信息；
- `app/knowledge/service.py`：修改跨查询聚合和 `_select_evidence()`；
- `tests/knowledge/test_adaptive_service.py`；
- Stage C trace/report 兼容性测试。

#### 成本与预期

不增加任何外部调用或模型 token，只改变本地融合算法。当前至少有“包装要求”和“积分扣回”证据已进入候选池但被最终选择挤掉，这一方向可能直接改善它们，能效比很高。

### P2：仅扩大 Agentic 的候选池

#### 问题

剩余缺失证据中至少有以下章节没有进入每个查询的 Top-8：

- 单笔订单赔偿上限；
- 标准地区预计送达时间；
- 文档间冲突处理优先级。

Rerank 无法找回未进入候选池的内容。

#### 建议

- `HybridRetriever.retrieve()` 接受可选 `prefetch_limit` 和 `result_limit`；
- 默认值仍为 8，传统 RAG 行为完全不变；
- Agentic Search 先试 12，再根据评测决定是否增加到 16；
- 最终 citation 上限保持不变。

#### 改动范围

- `app/knowledge/retrieval.py`：增加带默认值的召回参数；
- `app/knowledge/service.py`：Agentic 传入更大候选数；
- retrieval/adaptive/rerank 相关测试。

#### 成本与预期

不增加 rerank 调用次数，但会增加 Qdrant 返回量和 rerank 输入 token。它是提高候选召回最直接的方案，预计成本中等、收益较高。

### P2：修正 Planner 对复合问题的分类

#### 问题

部分同时询问“能否执行”和“退款到账/金额/后续规则”的问题仍被分类为 `SINGLE`，导致只检索其中一个方面。当前 preferred strategy 命中率为 79.49%，仍有改进空间。

#### 建议

- 增加“操作条件 + 后续金额/到账时间”应使用 `MULTI` 的规则和示例；
- 业务事实加政策解释时优先拆成 2 个自包含查询；
- 先使用 prompt 优化，避免过早加入复杂启发式规则。

#### 改动范围

- `app/knowledge/planning.py`；
- `tests/knowledge/test_planning.py`。

#### 成本与预期

代码改动小，不增加 Planner 调用；被调整为 `MULTI` 的请求会多一次检索与 rerank。预期可改善混合事实/政策问题。

### P3：允许 SINGLE 条件性补充检索

#### 问题

当前服务只有 `MULTI` 可以执行第二轮。即使 Assessor 正确发现 `SINGLE` 证据不完整并返回 follow-up query，服务也会立即停止。

#### 建议

- `SINGLE` 最多允许一个 follow-up query；
- 只有 Assessor 明确返回缺失方面时触发；
- 总轮数、模型调用数和超时上限保持不变。

#### 改动范围

- `app/knowledge/service.py`；
- `tests/knowledge/test_adaptive_service.py`。

#### 成本与预期

改动小，但收益依赖 Assessor 判断质量。应在 P1 Assessor 优化完成后实施，避免误判造成不必要调用。

## 4. 推荐实施顺序

建议每次只改变一个变量并重新运行 Stage C：

1. 强化 Assessor，并让它看到原始问题；
2. 改为覆盖感知的多查询融合；
3. 将 Agentic 候选池从 8 增至 12；
4. 优化 Planner 的混合问题分类；
5. 根据前述结果决定是否允许 SINGLE 补检索；
6. 只有召回仍不足时，才评估 16 个候选或 3 个首轮查询。

每一步应同时比较召回率、精确率、完整覆盖率、P50/P95 延迟、预算耗尽数量及调用/token 成本。

## 5. Stage C A/B 评测约束

### 5.1 复用同一个 fixture 数据库

后续实验必须复用同一个 `state.db`，只更换 checkpoint 和 report。若每轮都使用新的 SQLite 数据库，fixture 会生成新的 document/version UUID；chunk ID 又包含 version ID，因此相同语料会以新 point ID 重新写入同一个 Qdrant collection。

虽然检索会按当前 active version ID 过滤，旧 point 通常不会进入结果，但重复入库会：

- 增加 Qdrant point 数和存储；
- 重复产生 embedding 费用；
- 引入向量生成与基础设施波动，降低 A/B 可比性。

推荐固定：

```text
--database .artifacts/stage-c-retrieval-rerank/state.db
```

每次实验仅使用新的：

```text
--checkpoint .artifacts/stage-c-experiments/<experiment>-checkpoint.json
--output .artifacts/stage-c-experiments/<experiment>-report.json
```

### 5.2 补全实验元数据

当前 Stage C fingerprint 尚未包含 rerank 配置，导致纯正文、结构化输入和不同 instruct 的报告可能拥有相同 fingerprint。后续应将以下信息写入 metadata/fingerprint：

- rerank 模型；
- rerank instruct 的摘要或哈希；
- rerank 输入格式版本；
- 每个查询的候选数量；
- 融合算法版本；
- rerank 调用次数、输入 token 和延迟。

这项改动不直接提高召回率，但对实验复现、成本评估和防止错误续跑非常重要。
