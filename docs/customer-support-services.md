# SupportPilot 确定性客服业务服务

## 定位

`CustomerSupportService` 是 Store 和未来 Tool Gateway 之间的 Application
Service。它不调用 LLM，不依赖 FastAPI，也不信任调用者提供的租户 ID。

调用者必须提供已经通过成员校验的 `TenantContext`。公开业务方法从
`context.organization_id` 获取数据范围，从 `context.user_id` 获取当前操作人。

## 四个业务用例

- `get_order()`：返回当前企业的订单和客户。
- `get_logistics()`：返回订单、客户以及物流可用状态。
- `create_ticket()`：根据订单或客户创建工单。
- `add_ticket_note()`：以当前用户身份添加内部备注。

## 直接构造

```python
from app.application.customer_support_factory import (
    create_customer_support_service,
)

service = create_customer_support_service("./user.db")
```

## 查询订单和物流

```python
order_result = service.get_order(
    context=context,
    order_no="ORD-DELAY-001",
)
logistics_result = service.get_logistics(
    context=context,
    order_no="ORD-DELAY-001",
)
```

`get_logistics()` 的结果语义：

- `availability == AVAILABLE`：`shipment` 是现有物流记录。
- `availability == NOT_CREATED`：订单存在，但尚未产生物流记录。
- 订单不存在：抛出 `SupportOrderNotFoundError`。

## 创建工单和添加备注

```python
from app.tickets.base import TicketCategory, TicketPriority

ticket = service.create_ticket(
    context=context,
    order_no="ORD-DELAY-001",
    customer_no=None,
    summary="订单超过承诺时间仍未发货",
    category=TicketCategory.LOGISTICS,
    priority=TicketPriority.HIGH,
)
note = service.add_ticket_note(
    context=context,
    ticket_no=ticket.ticket_no,
    content="已联系仓库核查发货状态。",
)
```

新工单默认：

- 创建人为 `context.user_id`。
- 负责人为 `context.user_id`。
- 状态为 `open`。
- 工单号由服务生成。

`add_ticket_note()` 只创建 `internal` 备注，不用于向客户发送公开回复。

## 安全边界

- 不允许业务调用方给 Service 传入 `organization_id`。
- 不允许业务调用方冒充其他备注作者或工单创建人。
- 数据查询和写入始终限制在 `TenantContext` 的企业中。
- `TenantContext` 必须由现有认证和 membership 校验流程产生。

## 暂不包含

- HTTP 客服接口。
- LLM 工具和 Tool Gateway。
- 公开客户回复。
- 工单状态流转。
- 退款、补偿和审批。
- RAG。

下一阶段应把这些业务方法包装成受控 Tool Gateway；工具从服务器上下文取得
`TenantContext`，不能让模型提交可信租户或操作人字段。
