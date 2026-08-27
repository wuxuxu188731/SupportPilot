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

当前不包含：

- 退款、补偿和审批。
- LangGraph 工作流。
- 真实电商、物流和 CRM 集成。
