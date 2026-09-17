# SupportPilot

SupportPilot 是一个面向电商售后客服团队的多租户工单处理 Agent。它能够理解
客服请求，在当前企业范围内查询客户、订单和物流信息，辅助创建工单和添加内部
备注，并根据真实工具结果生成回复。不同企业的成员、会话、业务数据和未来知识库
互相隔离。

当前已完成：

- 用户认证、多租户和 `admin` / `agent` 最小 RBAC。
- 企业范围的客户、订单、物流、工单和工单备注。
- 不依赖 LLM 的确定性客服业务服务。
- 使用可信 `TenantContext` 为每个请求重新绑定的受控 Tool Gateway。
- “查询订单 → 查询物流 → 创建工单 → 生成回复”Agent 黄金路径。
- 退款与优惠券补偿提案、管理员审批、修改后批准和拒绝。
- 使用独立 SQLite checkpointer 的 LangGraph 确定性暂停/恢复工作流。
- 版本化幂等执行、可重试失败恢复、跨租户隐藏和结构化待审批响应。

### 退款/补偿审批与可靠执行

- Agent 只暴露 `propose_refund`、`propose_compensation` 和只读
  `get_action_status`；审批、恢复与执行器不会暴露给模型。
- 提案创建不产生退款或补偿副作用，只有当前企业 `admin` 的持久化批准决定才能授权
  执行。
- 审批支持批准、修改后批准和拒绝；重复决定、重复恢复与节点重放由业务幂等键保护。
- 等待审批、决定提交后恢复失败和可重试执行失败都可以使用原 Run 恢复。
- 退款与补偿结果均为模拟业务记录，不代表真实到账或真实发券。
- 部署、备份、状态判断和故障恢复步骤见
  [`docs/refund-compensation-operations.md`](docs/refund-compensation-operations.md)。

知识库：

- 已完成企业范围的 Markdown/TXT 知识入库、版本管理和停用/启用。
- 已完成 DashScope dense+sparse + Qdrant RRF 的传统 RAG Baseline。
- 已完成 Stage B：NONE/SINGLE/MULTI 三路 Planner、多查询混合召回、最多一次补充检索、EvidenceAssessor，以及业务工具与知识工具的同回合组合。
- Baseline 与 Agentic Search 都会对每次 embedding 召回且经 SQLite 校验的 chunk 调用同一个百炼 `qwen3-rerank`；Baseline 仍保持单原问题、单轮混合召回，Agentic Search 额外使用 Planner、查询拆解和 EvidenceAssessor。
- `search_knowledge(question)` 由请求级可信租户上下文绑定；模型不能传租户、Top-K、轮数或超时。
- 充分证据以服务端 C1..Cn 结构化引用返回；未知引用或完全漏引会被确定性标记为 `answer_incomplete`。
- 阶段 A 已修正 ordinal 邻接误删互补章节的问题，并将 raw Top-K precision 与阶段 B
  最终 citation precision 分开。历史候选的无网络回放为 20/20 黄金章节命中；真实
  DashScope/Qdrant 重跑命令见 `docs/evals/tenant-scoped-rag-stage-a-baseline.md`。
- Stage C 提供 48 条样例的 Baseline/Adaptive 检索级对照；本地 fake/黄金路径测试不代表真实环境质量达标。

### Stage C 检索评测

配置 `DASHSCOPE_API_KEY`、`DEEPSEEK_API_KEY` 并启动本地 Qdrant 后，在仓库根目录运行（同一命令会从 checkpoint 续跑）：

重排序默认使用北京业务空间 `ws-tocwkn1wc3xhur1f` 和通用问答检索指令，Baseline 与 Agentic Search 共享相同实例，并仅对 embedding 召回且经 SQLite 校验的 chunk 正文排序。可通过 `KNOWLEDGE_RERANK_INSTRUCT=Retrieve semantically similar text.` 切换为语义相似度策略；密钥始终从 `DASHSCOPE_API_KEY` 读取。

```text
python scripts/run_stage_c_retrieval_eval.py --database .artifacts/stage-c-retrieval/state.db --cases evals/knowledge/stage_c/cases.jsonl --checkpoint .artifacts/stage-c-retrieval/checkpoint.json --output .artifacts/stage-c-retrieval/report.json
```

运行状态、恢复记录和结果分别写入 `.artifacts/stage-c-retrieval/state.db`、`.artifacts/stage-c-retrieval/checkpoint.json` 与 `.artifacts/stage-c-retrieval/report.json`。两种策略统一按最终前 5 个 citation 评分；Adaptive 的第 6 个 citation 仅保留为诊断信息。

Embedding、模型或 Qdrant 基础设施失败会写入可续跑 checkpoint 并以非零状态退出，不能按“无结果”解读。DashScope/DeepSeek 余额不足时充值后使用同一命令续跑；Qdrant 返回 503/Bad Gateway 时先排除 VPN/代理干扰，再使用同一命令续跑。

前端：

- 已完成注册登录、企业创建与切换、客服对话（含引用、检索摘要、Agent 事件与待审批卡片）、
  审批中心、知识库管理与企业成员管理。
- 已完成知识库正文查看与引用跳转：正文按**版本**读取入库时转换好的 Markdown
  （`GET /knowledge/documents/{id}/versions/{id}/content/`），引用卡片可跳到正文并
  按引用偏移**高亮**对应片段，偏移缺失/越界时降级为章节或文档顶部定位。
  桌面端为对话区右侧内嵌面板（与对话同屏并排，不是遮罩），窄屏降级为整页跳转；
  正文缓存为“内存 + 会话内”，企业切换即清理；**不提供原文下载**（读到的是转换后的
  Markdown，不是 PDF/Word 原件）。
- 已完成**引用随回答持久化**：引用（含来源文档、标题路径、证据片段快照与偏移）与
  「引用是否可能不完整」标记随回答一起落库（迁移 `0014_message_citations`），
  刷新页面或重新进入会话后引用卡片仍在、可继续「查看原文位置」并高亮，
  展示与刷新前完全一致；偏移按**生成时冻结的版本**解释，不会重新对齐到最新版本。
  同一轮内多次知识检索的引用编号**全局连续**（不会互相冲突），检索摘要如实反映
  实际检索轮数。
- 审批中心覆盖列表筛选与分页、审批详情与版本时间线、批准 / 修改后批准 / 拒绝三种决定，
  以及 Run 状态查看与恢复；`agent` 角色只读，决定入口仅对 `admin` 展示。
- 企业切换或退出登录时统一清理租户相关状态，并丢弃旧企业迟到响应。

当前不包含：

- 真实支付退款、真实优惠券发放或外部 CRM 写入。
- 消息通知、Worker、定时重试和多实例分布式执行租约。
- 真实电商、物流和 CRM 集成。
- 知识库原文下载与正文分页。
- 处理过程（`events`）、检索摘要行（`retrieval_summary`）与待审批卡片
  （`pending_approvals`）的持久化：刷新后只恢复问答文本与**引用**，
  这三项仍会消失（属既定范围，见设计稿第 11 节）。
