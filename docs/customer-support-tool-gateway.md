# SupportPilot 客服 Tool Gateway

## 定位

`CustomerSupportToolGateway` 位于确定性 `CustomerSupportService` 和未来
Agent runner 之间。它负责验证模型可提交的业务参数、绑定服务器产生的
`TenantContext`、序列化结果并隐藏内部异常。

第四步只构建和测试工具边界，尚未让 LLM 自动选择或执行这些工具。

## 构造和绑定

```python
from app.tools.support_factory import (
    create_customer_support_tool_gateway,
)

gateway = create_customer_support_tool_gateway("./user.db")
functions = gateway.bind(context=trusted_context)
definitions = gateway.definitions
```

`trusted_context` 必须来自现有认证和 membership 校验流程，不能根据模型参数
构造。

## 工具

- `get_order(order_no)`
- `get_logistics(order_no)`
- `create_ticket(summary, category, priority, customer_no?, order_no?)`
- `add_ticket_note(ticket_no, content)`

模型不能提交 `organization_id`、`user_id`、`actor_user_id`、`role` 或
`context`。出现任何额外字段时，Gateway 返回 `INVALID_ARGUMENTS`，不会调用
业务 Service。

## 结果

成功：

```json
{"ok": true, "data": {"availability": "not_created"}}
```

失败：

```json
{
  "ok": false,
  "error": {
    "code": "ORDER_NOT_FOUND",
    "message": "order not found"
  }
}
```

模型可见结果只使用订单号、客户号、物流单号、跟踪号和工单号等业务编号，
不返回数据库内部 UUID。

## 下一步

第五步再修改聊天调用链：每个请求使用当前 `TenantContext` 绑定 Gateway，
把 `gateway.definitions` 和绑定后的函数交给 Agent runner，并用模拟模型完成
"物流延迟 → 查询订单 → 查询物流 → 创建工单 → 生成回复"黄金路径。

在完成第五步之前，不修改 `main.py` 的全局工具注册方式。
