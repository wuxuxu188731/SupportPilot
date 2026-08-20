# Stage C 剩余任务说明

## 1. 文档目的

Stage C 已完成第一项工作：准备新的 48 条黄金评测样例及 org B 跨租户安全夹具。(具体查看evals\knowledge\stage_c目录下文件)

本文档说明后续五项任务的主要工作、交付重点和主要风险。任务应按顺序推进，因为后续答案生成、自动评分、人工复核和最终报告都依赖前面的评测运行产物。

当前项目已经具备 Baseline 检索、Adaptive 检索、QueryPlanner、EvidenceAssessor、知识工具、业务工具组合和最终 citation ID 校验等生产能力。后续工作的重点不是重新实现 RAG，而是建设可信、可复现、可恢复的 Stage C 对照评测链路。

## 2. 任务二：运行 Baseline/Adaptive 检索级对照

### 2.1 任务目标

让同一批 Stage C 样例分别通过 Baseline 和 Adaptive 两条真实检索链路，产出可复现的逐样例结果和聚合指标。

### 2.2 主要工作

1. 定义并校验 Stage C Eval Case 模型，支持以下新字段：
   - `scenario`
   - `reference_answer`
   - `key_answer_facts`
   - `required_evidence_groups`
   - `strategy_expectation`
   - `business_context`
   - `security_expectations`

2. 解析证据组语义：
   - 不同 evidence group 之间为 AND；
   - 同一 group 的 `any_of` 内部为 OR；
   - 多个等价来源同时命中时，同一证据组只计一次覆盖；
   - 只有真正支持目标事实的 chunk 才计为相关证据。

3. 建立 Stage C 语料装配：
   - org A 使用 `docs/knowledge/` 下的 8 篇真实文档；
   - org B 使用 `evals/knowledge/stage_c/documents/org_b/` 下的 synthetic 冲突文档；
   - org A 与 org B 使用不同的真实评测 `organization_id`，但进入同一个 Qdrant Collection；
   - 评测器维护完整的租户—文档映射，不依赖标题或自然语言猜测文档归属。

4. 支持 Stage C 场景状态：
   - `main_active`
   - `cross_tenant_conflict`
   - `prompt_injection_document`
   - `document_disabled`
   - `old_version_inactive`
   - `budget_attack`

5. 支持 org B 作为当前可信租户：
   - Prompt Injection 样例以 org B 身份执行；
   - `forbidden_tenant_keys` 相应指向 org A；
   - Runner 不能保留“所有样例都由 org A 执行”的 Legacy 假设。

6. 在同一数据版本和索引状态下分别执行：
   - Baseline：原问题、固定单查询、固定 Top-K 混合检索；
   - Adaptive：真实 Planner、SINGLE/MULTI/NONE、多查询、EvidenceAssessor，以及必要时的一次补充检索。

7. 将返回 citation 映射回稳定身份：
   - `tenant_key`
   - `document_key`
   - `heading_path`
   - `document_id`
   - `version_id`
   - `chunk_id`

8. 计算检索级指标：
   - 证据组召回率；
   - 检索精度；
   - 完整证据覆盖率；
   - 期望策略与实际策略；
   - 检索轮数；
   - 模型调用次数；
   - query 数量；
   - token 估算；
   - 检索延迟；
   - 跨租户泄漏；
   - 停用文档和旧版本泄漏；
   - 预算上限突破。

9. 保存可复现元数据：
   - Git revision；
   - cases 文件哈希；
   - org A/org B 全部语料哈希；
   - Loader/Chunker 版本；
   - Embedding 模型和维度；
   - Qdrant Collection 与索引版本；
   - Planner/Assessor 模型；
   - Prompt 版本；
   - Top-K、token budget、融合阈值和超时配置。

10. 实现逐样例 checkpoint 和续跑：
    - 已完成样例结果可安全复用；
    - 单条外部调用失败不应丢失此前全部结果；
    - 基础设施失败不能被记录为“无命中”或“证据不足”；
    - 重跑时能够区分成功结果、失败结果和配置变化导致的失效缓存。

11. 为以下部分编写无网络测试：
    - Stage C Case 解析和严格校验；
    - evidence group AND/OR 评分；
    - 文档和租户映射；
    - scenario 状态装配；
    - Baseline/Adaptive 结果归一化；
    - checkpoint、恢复和版本失配；
    - 安全指标计算。

### 2.3 主要风险

1. 现有 `run_knowledge_baseline_eval.py` 只支持 Legacy 16 条，语料目录、租户、文档键和旧 Schema 都是硬编码的，不能直接承担 Stage C。
2. Stage C 的 heading 标注必须先验证能映射到实际 chunk；否则会把标注问题误诊为检索失败。
3. 停用文档和旧版本场景需要构造真实数据库与 Qdrant 状态，不能只修改 JSON 字段模拟。
4. 不同场景如果共享可变状态，停用、版本切换或 Prompt Injection 文档可能污染其他样例。
5. Adaptive 的模型调用数和 token 目前主要记录在 retrieval event 中，Eval Runner 需要可靠取得这些数据，不能从日志文本猜测。
6. Baseline 最终 Top-K 与 Adaptive 最终证据数量不同，需要提前固定公平的召回与覆盖口径。
7. 外部 Embedding、Qdrant 或结构化 LLM 失败必须中止或明确标记，不能被转换为低质量指标。
8. 如果没有 checkpoint，一次后段失败会导致整批外部调用重复执行，增加成本并降低可复现性。

## 3. 任务三：使用同一真实模型生成 Baseline/Adaptive 答案

### 3.1 任务目标

将 Baseline 和 Adaptive 检索得到的证据分别交给相同的真实答案生成模型，在相同 Prompt 和业务事实条件下生成两组最终答案，使答案质量差异尽可能只来源于检索链路。

### 3.2 主要工作

1. 为 Baseline 和 Adaptive 提供统一的评测知识工具接口：
   - 工具名和参数保持一致；
   - 工具公开结果结构保持一致；
   - 仅内部检索策略不同；
   - Baseline 不得意外复用 Adaptive 的 Planner 或 Assessor。

2. 固定答案生成条件：
   - 相同生成模型；
   - 相同系统 Prompt；
   - 相同生成参数；
   - 相同业务工具定义；
   - 相同业务事实夹具；
   - 相同 citation 规则。

3. 为 12 条 `mixed_fact_policy` 样例提供固定业务工具夹具：
   - `get_order` 返回 `business_context` 中的订单事实；
   - 需要时由 `get_logistics` 返回物流事实；
   - 使用虚构订单和客户数据；
   - 不依赖运行当天时间；
   - 不要求唯一工具调用顺序，但必须取得回答所需事实。

4. 运行完整 Agent 路径：
   - 用户通过订单号或政策问题发起请求；
   - Agent 按需调用业务工具；
   - Agent 调用 Baseline 或 Adaptive 版本的知识工具；
   - Agent 根据工具结果生成最终自然语言答案；
   - 最终政策结论使用服务端允许的 `[C#]`；
   - 无证据、预算失败和基础设施失败时执行诚实降级。

5. 复用并扩展确定性 citation 校验：
   - 剔除未知 citation ID；
   - 充分证据完全漏引时标记 `answer_incomplete`；
   - 第二次知识工具预算拒绝不能覆盖第一次有效 payload；
   - 最终结构化 citations 只能复制服务端返回对象。

6. 保存逐样例答案产物：
   - 原始 `case_id` 和 variant；
   - 最终答案；
   - 结构化 citations；
   - retrieval summary；
   - 工具调用轨迹；
   - 错误码；
   - `answer_incomplete`；
   - 生成模型和 Prompt 版本；
   - 模型调用次数、token usage、延迟和成本元数据。

7. 支持单样例恢复和重试：
   - Baseline/Adaptive 分开 checkpoint；
   - 不因单次 provider 故障重跑所有答案；
   - 重试结果保留 attempt 信息；
   - 配置、Prompt 或模型变化后不能错误复用旧结果。

8. 不在评测 artifact 中保存模型内部 reasoning；只保存最终答案、公开工具结果和必要的诊断元数据。

### 3.3 主要风险

1. 当前生产 `search_knowledge` 连接 Adaptive；Baseline 需要评测专用适配器，不能修改生产语义来迁就 Eval。
2. Baseline 与 Adaptive 如果使用不同答案 Prompt、业务事实或工具返回结构，对照实验将失去公平性。
3. Mixed 样例若把全部业务事实直接拼入 Prompt，会绕过业务工具链路，无法证明 Agent 会正确组合业务事实和政策。
4. 当前 `LLMResponse` 没有完整答案生成 usage/cost 数据，需要在评测边界捕获，而不能从回答长度粗略代替真实 usage。
5. 顶层 Agent 可能生成不同的知识查询；必须在报告中如实记录，不能把这种差异悄悄抹平。
6. Agent 工具循环可能产生额外模型调用、无效工具调用或重复知识调用，必须受现有预算约束并记录。
7. 模型输出具有非确定性；必须固定模型、Prompt 和配置，并通过 checkpoint 保存实际运行结果。
8. 如果将模型 reasoning 写入 artifact，会增加敏感数据和不必要的评测噪声。

## 4. 任务四：自动评估事实、引用、拒答和安全

### 4.1 任务目标

根据 Stage C 的结构化黄金事实、证据组、安全约束和两组最终答案，生成可复核的逐样例判定与聚合指标。

### 4.2 主要工作

1. 实现确定性指标：
   - evidence group recall；
   - retrieval precision；
   - 完整证据覆盖率；
   - citation ID 有效性；
   - citation 的真实租户归属；
   - citation 是否来自当前激活版本；
   - 停用文档是否被引用；
   - Canary 是否出现在不允许的答案或输出中；
   - 实际策略、查询数、轮数和模型调用是否超限；
   - `answer_incomplete`；
   - 基础设施失败分类。

2. 评估答案事实：
   - 按 `key_answer_facts` 判断每个原子事实是否正确表达；
   - 区分正确、错误、遗漏和无法判断；
   - 支持同义表达、数值计算、否定和条件限制；
   - 不使用最终答案与 `reference_answer` 的逐字字符串匹配。

3. 评估 claim-citation 支持关系：
   - 提取最终答案中的政策结论；
   - 确认每项结论附有允许的 citation；
   - 判断 citation 内容是否真实支持该结论；
   - 区分“引用编号有效”和“引用内容能够支持结论”；
   - 识别引用正确但结论夸大的情况。

4. 评估回答行为：
   - `answer_grounded` 是否基于充分证据回答；
   - `abstain` 是否拒绝无依据的肯定结论；
   - `deny_cross_tenant` 是否拒绝披露其他租户；
   - `clarify` 是否提出与缺失信息相关的澄清问题；
   - Prompt Injection 是否改变权限、租户、工具行为或答案事实。

5. 计算最终答案指标：
   - `answer_fact_accuracy`；
   - `citation_precision`；
   - `grounded_answer_rate`；
   - `unsupported_claim_rate`；
   - `correct_abstention_rate`；
   - `cross_tenant_leak_rate`；
   - 分类指标和总体指标。

6. 对不能可靠确定性判断的自然语言部分使用结构化评审：
   - 固定 Judge 模型；
   - 固定 Judge Prompt 版本；
   - 只提供当前样例、黄金事实、合法 citations 和最终答案；
   - 要求严格 JSON 输出；
   - 对每个事实和结论给出稳定 reason code；
   - 保存 Judge 版本和结构化判定，不保存内部 reasoning；
   - Judge 只辅助自然语言语义判断，不能替代租户、版本、预算和 citation ID 的确定性校验。

7. 建立人工校准集：
   - 选取正确、错误、部分正确、拒答、歧义和注入样例；
   - 对比人工判定和 Judge 判定；
   - 记录不一致类型；
   - 修正 Judge Prompt 或把争议情况转入人工复核；
   - 禁止为了迁就模型输出而修改黄金答案。

8. 输出逐样例评分 artifact：
   - Baseline/Adaptive 分开记录；
   - 保留每个 fact 和 citation 的细粒度判定；
   - 聚合指标由明确的整数分子、分母计算；
   - 不从已舍入的浮点数反推计数；
   - 所有不可测指标使用 `null + scope/reason`，不能伪造为 0。

### 4.3 主要风险

1. 自然语言不能只靠关键词判断；否定句、条件句、同义表达和计算结果容易产生误判。
2. 单纯使用 LLM-as-a-Judge 会引入模型偏差和不稳定性，不能作为安全指标的唯一依据。
3. citation ID 合法不代表 citation 内容支持结论；如果只校验编号，会高估 citation precision。
4. `reference_answer` 是人类参考，不是唯一允许措辞；逐字匹配会大量误伤正确答案。
5. 84 个原子事实必须保持评分粒度一致，否则不同样例的 accuracy 不可比较。
6. 无答案、跨租户、澄清和 Prompt Injection 的成功标准不同，不能使用同一个“是否拒答”布尔值粗略处理。
7. 基础设施失败、证据不足和最终生成错误必须分开归因，否则指标会掩盖真实故障层。
8. Judge Prompt 如果在看到系统结果后反复调优，可能形成评测过拟合；版本变化必须记录并重新校准。

## 5. 任务五：人工复核失败和争议样例

### 5.1 任务目标

对 Baseline/Adaptive 的最终答案、自动评分和安全结果进行人工审核，纠正自动判分误差，并为失败样例确定真实归因。

### 5.2 主要工作

1. 生成便于审核的逐样例视图，至少同时展示：
   - case 元数据；
   - 用户问题；
   - 业务工具事实；
   - reference answer；
   - key answer facts；
   - required evidence groups；
   - Baseline/Adaptive 最终答案；
   - 返回 citations 及对应 chunk；
   - 检索策略、轮数和工具轨迹；
   - 自动评分和 reason code。

2. 对两组答案执行人工判定：
   - 关键事实是否正确；
   - 是否遗漏必要条件或例外；
   - 是否存在无依据结论；
   - citation 是否支持对应结论；
   - 应拒答时是否错误作答；
   - 应澄清时是否提出有效问题；
   - 是否泄漏租户、旧版本、停用内容或 Canary；
   - Prompt Injection 是否影响行为。

3. 复核自动评分争议：
   - 人工与 Judge 不一致；
   - Judge 返回无法判断；
   - 关键词或数值规则产生边界结果；
   - citation 支持关系存在多种合理解释；
   - 文档本身存在重复、交叉引用或冲突。

4. 为每个失败样例选择一个主要失败阶段：
   - 数据集/黄金标注；
   - 入库或分块；
   - Embedding 召回；
   - 融合和候选选择；
   - QueryPlanner；
   - EvidenceAssessor；
   - 业务工具调用；
   - 最终答案生成；
   - citation 选择或校验；
   - 评测器误判；
   - 基础设施失败。

5. 记录修订规则：
   - 只有黄金标注明确客观错误时才能修改数据集；
   - 数据集修订必须提升版本并记录原因；
   - 不得因为系统未召回某章节就删除正确黄金证据；
   - Prompt、模型或阈值变化后必须重新运行受影响样例；
   - 人工判定需要保留 reviewer、时间和备注。

6. 输出人工审核结果：
   - 每条样例最终 verdict；
   - 自动评分是否被覆盖；
   - 覆盖理由；
   - 失败归因；
   - 是否需要数据修订、代码修复或配置调优；
   - 是否需要重跑。

### 5.3 主要风险

1. 只审核自动判定失败的样例，会遗漏自动评分的假阳性；第一轮正式报告应包含全量或明确比例的成功样例抽查。
2. 如果审核界面不同时展示答案、事实、citation 和 chunk，人工容易凭印象判断而不是依据证据。
3. 不同 reviewer 对“部分正确”“有据可依”和“正确拒答”的理解可能不一致，需要统一 rubric。
4. 人工发现黄金标注错误后若直接覆盖原文件，会破坏数据集版本和历史结果可复现性。
5. 将多个失败阶段同时作为主因会让报告无法解释系统最主要的改进方向。
6. 人工复核可能触发数据、评分器或 Prompt 修改；必须明确哪些结果需要失效和重跑。

## 6. 任务六：输出质量、成本和延迟对比报告

### 6.1 任务目标

把检索、答案、人工复核和运行元数据整理为一份可复现、可解释的 Baseline/Adaptive 对照报告，并形成 README 与面试展示材料。

### 6.2 主要工作

1. 汇总总体指标：
   - retrieval recall；
   - evidence group coverage；
   - retrieval precision；
   - answer fact accuracy；
   - citation precision；
   - grounded answer rate；
   - unsupported claim rate；
   - correct abstention rate；
   - cross-tenant leak rate；
   - 平均检索轮数；
   - 平均模型调用数；
   - token 和成本；
   - P50/P95 延迟。

2. 按四个类别分别比较：
   - simple policy；
   - multi-condition policy；
   - mixed fact policy；
   - safety。

3. 明确检查设计验收要求：
   - 跨租户泄漏必须为 0；
   - citation precision；
   - retrieval recall；
   - 无答案/攻击类正确拒答；
   - unsupported claim；
   - Adaptive 在复杂问题上的答案准确率提升；
   - Adaptive 在简单问题上的正确率退化；
   - 平均和最大检索轮数；
   - Adaptive/Baseline 延迟倍率。

4. 展示质量、延迟和成本权衡：
   - Adaptive 提升了哪些类别；
   - 哪些简单问题不需要额外模型调用；
   - 第二轮检索在哪些样例中真正补齐了证据；
   - 提升对应增加了多少调用、token 和延迟；
   - 哪些失败无法通过增加检索轮数解决。

5. 选取代表性案例：
   - Baseline 和 Adaptive 都成功；
   - Adaptive 补齐多条件证据；
   - Adaptive 不必要地使用 MULTI；
   - 第二轮仍证据不足并正确拒答；
   - 跨租户和 Prompt Injection 被阻止；
   - 旧版本或停用文档被正确排除；
   - 最终生成错误但检索正确；
   - 评测器或黄金标注错误。

6. 为失败样例提供分层归因，而不是只列最终分数：
   - ingestion；
   - chunking；
   - embedding；
   - retrieval；
   - fusion/selection；
   - planning；
   - evidence assessment；
   - business tool；
   - answer generation；
   - citation；
   - infrastructure；
   - evaluation data/scorer。

7. 记录完整版本元数据：
   - code revision；
   - dataset/corpus hash；
   - Prompt 版本；
   - Embedding/Planner/Assessor/Generator/Judge 模型；
   - Qdrant Collection 和索引版本；
   - 阈值和预算配置；
   - 价格快照和成本口径；
   - 人工审核版本。

8. 输出交付物：
   - 机器可读 JSON 报告；
   - 人类可读 Markdown 报告；
   - README Stage C 摘要；
   - 面试展示用指标表、架构说明和典型案例；
   - 真实失败与未达标项，不隐藏、不调整黄金答案美化结果。

### 6.3 主要风险

1. 如果前序任务只输出控制台日志而没有稳定 JSON artifact，报告将难以复算和审计。
2. 聚合指标必须由原始整数分子、分母计算，不能从已舍入的 per-case 浮点数反推。
3. 检索精度与最终 citation precision 是不同指标，混用会产生错误结论。
4. 基础设施失败不能计为无答案或模型质量失败，也不能从报告中静默删除。
5. 只报告总体平均值会掩盖 Adaptive 在简单问题退化、复杂问题提升或安全类别失败。
6. 成本必须明确包含哪些调用；检索 Embedding 成本、Planner/Assessor 成本和最终生成成本不能混成没有口径的总数。
7. 未达到验收阈值时必须如实报告并给出失败归因，不能通过删除困难样例或修改黄金答案制造达标结果。
8. README 和面试材料必须引用同一版本的正式报告，避免多个数字版本互相矛盾。

## 7. 任务依赖与共同约束

任务依赖关系：

```text
任务二：检索级对照
    ↓
任务三：答案生成
    ↓
任务四：自动评分
    ↓
任务五：人工复核
    ↓
任务六：最终报告
```

所有任务共同遵守：

- Legacy 16 条回归集保持不变；
- Stage C 数据、代码、Prompt、模型和索引都要版本化；
- org A/org B 租户上下文只来自可信 Eval 装配；
- 文档正文视为不可信数据；
- 任何请求不得突破服务器端预算；
- 外部服务失败与证据不足分开处理；
- 不保存 API Key、Token、真实客户信息或模型内部 reasoning；
- 不为提高分数而调整黄金答案；
- 每个聚合指标都能追溯到逐样例原始结果。

