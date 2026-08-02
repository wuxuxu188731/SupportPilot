# SupportPilot 客服 Agent 黄金路径

## 请求级工具绑定

每次聊天请求先通过认证和 membership 校验得到 `TenantContext`。`ChatService` 将
该上下文传给 `CustomerSupportAgentRunner`，runner 再调用 `gateway.bind(context)` 创建
本次请求专属工具函数。

工具函数不能在应用启动时全局绑定，因为同一个进程会同时服务多个企业。

## 黄金路径

```text
用户报告订单延迟并明确要求创建工单
→ get_order
→ get_logistics
→ create_ticket
→ 模型引用工具返回的 ticket_no 生成回复
```

`availability=not_created` 表示订单存在但尚未产生物流记录。只有 `create_ticket` 返回
`ok=true` 时，回复才能声称工单创建成功。

## 可靠性

- Agent 工具轮数有固定上限。
- 工具参数由 Pydantic 校验。
- Tool Gateway 注入可信租户和操作人。
- 意外工具异常不会向模型暴露内部错误。
- Agent 回合失败时不保存部分会话消息。

## 测试

黄金路径测试使用假模型响应和真实 SQLite Store，不访问外部模型 API。测试验证工具
顺序、数据库写入、消息持久化、跨租户重新绑定和失败结果恢复。

## 下一阶段

下一阶段再增加企业范围 RAG、退款审批和更复杂工作流。RAG 检索必须继续使用
`TenantContext.organization_id` 作为强制过滤条件。
