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
- 当前尚未把知识检索注册到客服 Agent；自适应规划、证据检查和聊天引用属于下一阶段。

当前不包含：

- 退款、补偿和审批。
- LangGraph 工作流。
- 真实电商、物流和 CRM 集成。
