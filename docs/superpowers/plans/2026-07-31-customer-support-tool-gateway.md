# Customer Support Tool Gateway Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将现有确定性客服业务服务封装成四个受控工具，使模型未来只能提交订单号、工单摘要等业务参数，而可信的租户、用户和角色始终由服务器侧 `TenantContext` 注入。

**Architecture:** 在 `CustomerSupportService` 与未来 Agent runner 之间新增独立 `CustomerSupportToolGateway`。Pydantic 参数模型负责拒绝额外字段和校验工具参数，Gateway 通过 `bind(context)` 返回捕获可信上下文的工具函数，并把领域对象序列化为稳定、JSON 安全的结果；应用层预期异常映射为稳定错误码，意外异常只返回通用错误并写入服务器日志。本计划只完成工具边界和直接调用能力，不把工具交给 LLM 自动选择。

**Tech Stack:** Python 3.10+、Pydantic 2.11、标准库 `dataclasses` / `enum` / `json` / `logging` / `pathlib` / `typing`、现有 `CustomerSupportService`、pytest。

## Global Constraints

- 当前 Alembic head 为 `0008_ticket_comments`，计划编写前全量测试为 `151 passed`。
- 第三步的 `CustomerSupportService`、`create_customer_support_service()` 和四个确定性业务方法已经存在。
- 本计划不修改数据库表，不新增 Alembic migration。
- 本计划只实现 Tool Gateway，不实现 Agent 自动选择工具、提示词编排、聊天闭环、RAG、退款、审批、LangGraph、前端或真实第三方系统。
- 本计划不得修改 `app/agent/runner.py`、`app/application/chat_service.py`、`main.py` 或 `app/tools/registry.py`；这些文件的集成属于第五步。
- 工具公开名称固定为 `get_order`、`get_logistics`、`create_ticket`、`add_ticket_note`。
- 模型可见参数中禁止出现 `organization_id`、`user_id`、`actor_user_id`、`role` 或 `context`。
- 工具执行所用 `TenantContext` 只能由服务器调用 `gateway.bind(context=trusted_context)` 时绑定。
- 任何工具参数中出现未声明字段都必须失败，不能静默忽略。
- Gateway 必须把原始字典交给 Pydantic 参数模型校验，不能把模型参数直接透传给 Service。
- `create_ticket` 的 `category` 和 `priority` 必须由 Pydantic 转换为现有 `TicketCategory` 和 `TicketPriority` 枚举。
- 工具成功结果和失败结果都必须能由 `json.dumps()` 直接序列化。
- 模型可见结果不得包含数据库内部 UUID，例如 `customer_id`、`order_id`、`shipment_id`、`ticket_id`、`comment_id`。
- 预期业务失败返回 `ok: false` 和稳定错误码，不把 Python 异常类名、堆栈或数据库错误暴露给模型。
- 意外异常必须写服务器日志，并只返回 `INTERNAL_ERROR / tool execution failed`。
- `get_logistics` 对“订单存在但尚无物流”返回成功结果，`availability` 为 `not_created`，不能返回错误。
- `add_ticket_note` 仍然只添加 `internal` 备注，Gateway 不提供可见性参数。
- `admin` 和 `agent` 都可使用这四个低风险工具；本计划不新增角色判断。
- 每个 Task 使用 TDD：先写失败测试并确认失败，再做最小实现，运行局部测试和全量测试，最后独立提交。

---

## 1. 第四步完成标准

完成后，服务器代码可以这样绑定可信上下文：

```python
gateway = create_customer_support_tool_gateway("./user.db")
tool_functions = gateway.bind(context=trusted_context)
```

调用工具时只传业务参数：

```python
order_result = tool_functions["get_order"](
    order_no="ORD-DELAY-001",
)

ticket_result = tool_functions["create_ticket"](
    order_no="ORD-DELAY-001",
    summary="订单超过承诺时间仍未发货",
    category="logistics",
    priority="high",
)
```

下面的调用必须被拒绝，而且不能触发业务 Service：

```python
tool_functions["get_order"](
    order_no="ORD-DELAY-001",
    organization_id="attacker-selected-tenant",
)
```

成功结果统一为：

```json
{
  "ok": true,
  "data": {
    "order": {
      "order_no": "ORD-DELAY-001"
    }
  }
}
```

失败结果统一为：

```json
{
  "ok": false,
  "error": {
    "code": "ORDER_NOT_FOUND",
    "message": "order not found"
  }
}
```

## 2. 工具契约

| 工具 | 模型可提交参数 | Gateway 注入 | 调用的 Service |
|---|---|---|---|
| `get_order` | `order_no` | `TenantContext` | `get_order` |
| `get_logistics` | `order_no` | `TenantContext` | `get_logistics` |
| `create_ticket` | `summary`、`category`、`priority`、可选 `customer_no`、可选 `order_no` | `TenantContext` | `create_ticket` |
| `add_ticket_note` | `ticket_no`、`content` | `TenantContext` | `add_ticket_note` |

`create_ticket` 必须至少提供 `customer_no` 或 `order_no`，允许同时提供两者；同时提供时，现有 Application Service 继续负责验证客户和订单关系。

## 3. 稳定错误码

| 错误码 | 来源 |
|---|---|
| `UNKNOWN_TOOL` | 服务器请求执行不存在的工具 |
| `INVALID_ARGUMENTS` | Pydantic 校验失败或 Application Service 参数校验失败 |
| `ORDER_NOT_FOUND` | `SupportOrderNotFoundError` |
| `CUSTOMER_NOT_FOUND` | `SupportCustomerNotFoundError` |
| `ORDER_CUSTOMER_MISMATCH` | `OrderCustomerMismatchError` |
| `TICKET_NOT_FOUND` | `SupportTicketNotFoundError` |
| `OPERATION_REJECTED` | `SupportOperationRejectedError` |
| `TICKET_NUMBER_GENERATION_FAILED` | `TicketNumberGenerationError` |
| `DATA_INTEGRITY_ERROR` | `SupportDataIntegrityError` |
| `INTERNAL_ERROR` | 未预期异常 |

## 4. 最终文件结构

```text
app/tools/
  support_arguments.py
    四个 Pydantic 参数模型

  support_results.py
    领域对象到模型可见 JSON 字典的转换

  support_gateway.py
    上下文绑定、参数校验、Service 调用和错误映射

  support_definitions.py
    从参数模型生成 LLM function definitions

  support_factory.py
    从数据库路径构造 Gateway

tests/tools/
  test_support_arguments.py
  test_support_results.py
  test_support_gateway.py
  test_support_definitions.py
  test_support_factory.py

docs/
  customer-support-tool-gateway.md
```

---

### Task 1: 锁定工具参数模型和禁止模型控制租户

**独立验收产物：** 四个工具拥有严格的 Pydantic 参数模型；额外字段全部拒绝；创建工单至少需要客户号或订单号；同时修正 `add_ticket_note()` 的 `ticket_no` 类型标注。

**Files:**
- Create: `app/tools/support_arguments.py`
- Create: `tests/tools/test_support_arguments.py`
- Modify: `app/application/customer_support_service.py`

**Interfaces:**
- Produces: `GetOrderArguments`
- Produces: `GetLogisticsArguments`
- Produces: `CreateTicketArguments`
- Produces: `AddTicketNoteArguments`
- Produces: `SUPPORT_ARGUMENT_MODELS`
- Corrects: `CustomerSupportService.add_ticket_note(ticket_no: str)`

- [ ] **Step 1: 写参数模型失败测试**

创建 `tests/tools/test_support_arguments.py`：

```python
from typing import get_type_hints

import pytest
from pydantic import ValidationError

from app.application.customer_support_service import (
    CustomerSupportService,
)
from app.tickets.base import TicketCategory, TicketPriority
from app.tools.support_arguments import (
    AddTicketNoteArguments,
    CreateTicketArguments,
    GetLogisticsArguments,
    GetOrderArguments,
    SUPPORT_ARGUMENT_MODELS,
)


def test_support_argument_models_match_public_tool_names():
    assert set(SUPPORT_ARGUMENT_MODELS) == {
        "get_order",
        "get_logistics",
        "create_ticket",
        "add_ticket_note",
    }


def test_query_arguments_strip_whitespace():
    arguments = GetOrderArguments(
        order_no="  ORD-DELAY-001  ",
    )

    assert arguments.order_no == "ORD-DELAY-001"


@pytest.mark.parametrize(
    "forbidden_name",
    [
        "organization_id",
        "user_id",
        "actor_user_id",
        "role",
        "context",
    ],
)
def test_query_arguments_reject_security_context_fields(
    forbidden_name,
):
    payload = {
        "order_no": "ORD-DELAY-001",
        forbidden_name: "attacker-controlled",
    }

    with pytest.raises(ValidationError):
        GetOrderArguments.model_validate(payload)


def test_create_ticket_arguments_convert_enums():
    arguments = CreateTicketArguments(
        order_no="ORD-DELAY-001",
        summary="订单超过承诺时间仍未发货",
        category="logistics",
        priority="high",
    )

    assert arguments.category is TicketCategory.LOGISTICS
    assert arguments.priority is TicketPriority.HIGH
    assert arguments.customer_no is None


def test_create_ticket_requires_customer_or_order():
    with pytest.raises(
        ValidationError,
        match="customer_no or order_no is required",
    ):
        CreateTicketArguments(
            summary="客户问题需要创建工单",
            category="other",
            priority="medium",
        )


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (GetOrderArguments, {"order_no": "   "}),
        (GetLogisticsArguments, {"order_no": "   "}),
        (
            CreateTicketArguments,
            {
                "customer_no": "CUST-001",
                "summary": "abcd",
                "category": "other",
                "priority": "medium",
            },
        ),
        (
            AddTicketNoteArguments,
            {
                "ticket_no": "TKT-001",
                "content": "x" * 2001,
            },
        ),
    ],
)
def test_argument_models_enforce_text_boundaries(model, payload):
    with pytest.raises(ValidationError):
        model.model_validate(payload)


def test_add_ticket_note_annotation_is_string():
    hints = get_type_hints(
        CustomerSupportService.add_ticket_note
    )

    assert hints["ticket_no"] is str
```

- [ ] **Step 2: 运行测试并确认失败**

Run:

```powershell
python -m pytest tests/tools/test_support_arguments.py -q
```

Expected: collection FAIL，因为 `app.tools.support_arguments` 尚不存在。

- [ ] **Step 3: 创建严格参数模型**

创建 `app/tools/support_arguments.py`：

```python
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

from app.tickets.base import TicketCategory, TicketPriority


BUSINESS_NUMBER_MAX_LENGTH = 100


class SupportToolArguments(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )


class GetOrderArguments(SupportToolArguments):
    order_no: str = Field(
        min_length=1,
        max_length=BUSINESS_NUMBER_MAX_LENGTH,
    )


class GetLogisticsArguments(SupportToolArguments):
    order_no: str = Field(
        min_length=1,
        max_length=BUSINESS_NUMBER_MAX_LENGTH,
    )


class CreateTicketArguments(SupportToolArguments):
    summary: str = Field(min_length=5, max_length=500)
    category: TicketCategory
    priority: TicketPriority
    customer_no: str | None = Field(
        default=None,
        min_length=1,
        max_length=BUSINESS_NUMBER_MAX_LENGTH,
    )
    order_no: str | None = Field(
        default=None,
        min_length=1,
        max_length=BUSINESS_NUMBER_MAX_LENGTH,
    )

    @model_validator(mode="after")
    def require_customer_or_order(
        self,
    ) -> "CreateTicketArguments":
        if self.customer_no is None and self.order_no is None:
            raise ValueError(
                "customer_no or order_no is required"
            )
        return self


class AddTicketNoteArguments(SupportToolArguments):
    ticket_no: str = Field(
        min_length=1,
        max_length=BUSINESS_NUMBER_MAX_LENGTH,
    )
    content: str = Field(min_length=1, max_length=2000)


SUPPORT_ARGUMENT_MODELS = {
    "get_order": GetOrderArguments,
    "get_logistics": GetLogisticsArguments,
    "create_ticket": CreateTicketArguments,
    "add_ticket_note": AddTicketNoteArguments,
}
```

- [ ] **Step 4: 修正 Application Service 的类型标注**

在 `app/application/customer_support_service.py` 中把：

```python
ticket_no: Ticket
```

修改为：

```python
ticket_no: str
```

不要修改方法运行逻辑；现有实现本来就是按字符串校验和查询。

- [ ] **Step 5: 运行参数模型测试**

Run:

```powershell
python -m pytest tests/tools/test_support_arguments.py -q
```

Expected: 全部 PASS。

- [ ] **Step 6: 运行第三步回归**

Run:

```powershell
python -m pytest `
  tests/application/test_customer_support_service.py `
  tests/application/test_customer_support_factory.py `
  -q
```

Expected: 全部 PASS。

- [ ] **Step 7: 运行全量回归并提交**

Run:

```powershell
python -m pytest -q
```

Expected: 全部 PASS。

Commit:

```powershell
git add app/tools/support_arguments.py tests/tools/test_support_arguments.py app/application/customer_support_service.py
git commit -m "feat: add strict support tool arguments"
```

---

### Task 2: 将领域结果转换成稳定且不泄露内部 ID 的 JSON

**独立验收产物：** 四个业务方法的返回对象可以转换成稳定字典；所有结果可直接 JSON 序列化；结果中不出现内部 UUID 字段。

**Files:**
- Create: `app/tools/support_results.py`
- Create: `tests/tools/test_support_results.py`

**Interfaces:**
- Consumes: `OrderDetails`
- Consumes: `LogisticsDetails`
- Consumes: `Ticket`
- Consumes: `TicketComment`
- Produces: `tool_success(data) -> dict`
- Produces: `tool_failure(code, message, details) -> dict`
- Produces: `serialize_order_details`
- Produces: `serialize_logistics_details`
- Produces: `serialize_ticket`
- Produces: `serialize_ticket_note`

- [ ] **Step 1: 写订单和物流序列化失败测试**

创建 `tests/tools/test_support_results.py`：

```python
import json

from app.application.customer_support_service import (
    LogisticsAvailability,
    LogisticsDetails,
    OrderDetails,
)
from app.customers.base import Customer
from app.orders.base import Order, OrderStatus
from app.shipments.base import Shipment, ShipmentStatus
from app.tickets.base import (
    Ticket,
    TicketCategory,
    TicketComment,
    TicketCommentVisibility,
    TicketPriority,
    TicketStatus,
)
from app.tools.support_results import (
    serialize_logistics_details,
    serialize_order_details,
    serialize_ticket,
    serialize_ticket_note,
    tool_failure,
    tool_success,
)


CUSTOMER = Customer(
    customer_id="internal-customer-id",
    organization_id="internal-organization-id",
    customer_no="CUST-001",
    name="林晓",
    email="linxiao@example.test",
    phone="+86-000-0000-0001",
    created_at="2026-07-20T08:00:00+00:00",
)

ORDER = Order(
    order_id="internal-order-id",
    organization_id="internal-organization-id",
    order_no="ORD-DELAY-001",
    customer_id=CUSTOMER.customer_id,
    status=OrderStatus.PROCESSING,
    item_summary="无线耳机 x1",
    total_amount_cents=39900,
    currency="CNY",
    placed_at="2026-07-23T08:00:00+00:00",
    promised_ship_at="2026-07-25T08:00:00+00:00",
    created_at="2026-07-23T08:00:00+00:00",
    updated_at="2026-07-23T08:00:00+00:00",
)


def assert_json_safe_without_internal_ids(value):
    encoded = json.dumps(value, ensure_ascii=False)
    assert "internal-" not in encoded
    for forbidden_name in (
        "organization_id",
        "customer_id",
        "order_id",
        "shipment_id",
        "ticket_id",
        "comment_id",
    ):
        assert forbidden_name not in encoded


def test_serialize_order_details_uses_business_identifiers():
    result = serialize_order_details(
        OrderDetails(order=ORDER, customer=CUSTOMER)
    )

    assert result["order"]["order_no"] == "ORD-DELAY-001"
    assert result["order"]["status"] == "processing"
    assert result["customer"]["customer_no"] == "CUST-001"
    assert_json_safe_without_internal_ids(result)


def test_serialize_logistics_not_created_is_successful_data():
    result = serialize_logistics_details(
        LogisticsDetails(
            order=ORDER,
            customer=CUSTOMER,
            availability=LogisticsAvailability.NOT_CREATED,
            shipment=None,
        )
    )

    assert result["availability"] == "not_created"
    assert result["shipment"] is None
    assert_json_safe_without_internal_ids(result)


def test_serialize_existing_logistics_uses_tracking_number():
    shipment = Shipment(
        shipment_id="internal-shipment-id",
        organization_id="internal-organization-id",
        shipment_no="SHP-TRANSIT-001",
        order_id=ORDER.order_id,
        carrier="顺丰速运",
        tracking_no="SF-DEMO-TRANSIT-001",
        status=ShipmentStatus.IN_TRANSIT,
        last_event="快件已到达目的地分拨中心",
        shipped_at="2026-07-26T08:00:00+00:00",
        estimated_delivery_at="2026-08-01T08:00:00+00:00",
        delivered_at=None,
        created_at="2026-07-26T08:00:00+00:00",
        updated_at="2026-07-26T08:00:00+00:00",
    )

    result = serialize_logistics_details(
        LogisticsDetails(
            order=ORDER,
            customer=CUSTOMER,
            availability=LogisticsAvailability.AVAILABLE,
            shipment=shipment,
        )
    )

    assert result["shipment"]["tracking_no"] == (
        "SF-DEMO-TRANSIT-001"
    )
    assert result["shipment"]["status"] == "in_transit"
    assert_json_safe_without_internal_ids(result)
```

- [ ] **Step 2: 写工单、备注和结果信封失败测试**

在同一测试文件追加：

```python
def test_serialize_ticket_and_note_hide_internal_ids():
    ticket = Ticket(
        ticket_id="internal-ticket-id",
        organization_id="internal-organization-id",
        ticket_no="TKT-001",
        customer_id="internal-customer-id",
        order_id="internal-order-id",
        created_by_user_id="internal-user-id",
        assigned_to_user_id="internal-user-id",
        summary="订单超过承诺时间仍未发货",
        category=TicketCategory.LOGISTICS,
        priority=TicketPriority.HIGH,
        status=TicketStatus.OPEN,
        created_at="2026-07-31T08:00:00+00:00",
        updated_at="2026-07-31T08:00:00+00:00",
    )
    note = TicketComment(
        comment_id="internal-comment-id",
        organization_id="internal-organization-id",
        ticket_id=ticket.ticket_id,
        seq=1,
        author_user_id="internal-user-id",
        visibility=TicketCommentVisibility.INTERNAL,
        content="已联系仓库核查。",
        created_at="2026-07-31T08:01:00+00:00",
    )

    ticket_result = serialize_ticket(ticket)
    note_result = serialize_ticket_note(
        ticket_no=ticket.ticket_no,
        note=note,
    )

    assert ticket_result["ticket_no"] == "TKT-001"
    assert ticket_result["status"] == "open"
    assert note_result["ticket_no"] == "TKT-001"
    assert note_result["visibility"] == "internal"
    assert_json_safe_without_internal_ids(ticket_result)
    assert_json_safe_without_internal_ids(note_result)


def test_success_and_failure_envelopes_are_json_safe():
    success = tool_success({"value": "结果"})
    failure = tool_failure(
        code="INVALID_ARGUMENTS",
        message="tool arguments are invalid",
        details=[
            {
                "type": "missing",
                "loc": ["order_no"],
                "msg": "Field required",
            }
        ],
    )

    assert success == {
        "ok": True,
        "data": {"value": "结果"},
    }
    assert failure["ok"] is False
    assert failure["error"]["code"] == "INVALID_ARGUMENTS"
    json.dumps(success, ensure_ascii=False)
    json.dumps(failure, ensure_ascii=False)
```

- [ ] **Step 3: 运行测试并确认失败**

Run:

```powershell
python -m pytest tests/tools/test_support_results.py -q
```

Expected: collection FAIL，因为 `support_results.py` 尚不存在。

- [ ] **Step 4: 实现成功和失败信封**

创建 `app/tools/support_results.py`，先加入：

```python
from typing import Any

from app.application.customer_support_service import (
    LogisticsDetails,
    OrderDetails,
)
from app.customers.base import Customer
from app.orders.base import Order
from app.shipments.base import Shipment
from app.tickets.base import Ticket, TicketComment


JsonObject = dict[str, Any]


def tool_success(data: JsonObject) -> JsonObject:
    return {
        "ok": True,
        "data": data,
    }


def tool_failure(
    *,
    code: str,
    message: str,
    details: list[JsonObject] | None = None,
) -> JsonObject:
    error: JsonObject = {
        "code": code,
        "message": message,
    }
    if details is not None:
        error["details"] = details
    return {
        "ok": False,
        "error": error,
    }
```

- [ ] **Step 5: 实现客户、订单和物流序列化**

在同一文件追加：

```python
def serialize_customer(customer: Customer) -> JsonObject:
    return {
        "customer_no": customer.customer_no,
        "name": customer.name,
        "email": customer.email,
        "phone": customer.phone,
    }


def serialize_order(order: Order) -> JsonObject:
    return {
        "order_no": order.order_no,
        "status": order.status.value,
        "item_summary": order.item_summary,
        "total_amount_cents": order.total_amount_cents,
        "currency": order.currency,
        "placed_at": order.placed_at,
        "promised_ship_at": order.promised_ship_at,
    }


def serialize_shipment(
    shipment: Shipment,
) -> JsonObject:
    return {
        "shipment_no": shipment.shipment_no,
        "carrier": shipment.carrier,
        "tracking_no": shipment.tracking_no,
        "status": shipment.status.value,
        "last_event": shipment.last_event,
        "shipped_at": shipment.shipped_at,
        "estimated_delivery_at": (
            shipment.estimated_delivery_at
        ),
        "delivered_at": shipment.delivered_at,
    }


def serialize_order_details(
    details: OrderDetails,
) -> JsonObject:
    return {
        "order": serialize_order(details.order),
        "customer": serialize_customer(details.customer),
    }


def serialize_logistics_details(
    details: LogisticsDetails,
) -> JsonObject:
    return {
        "order": serialize_order(details.order),
        "customer": serialize_customer(details.customer),
        "availability": details.availability.value,
        "shipment": (
            serialize_shipment(details.shipment)
            if details.shipment is not None
            else None
        ),
    }
```

- [ ] **Step 6: 实现工单和备注序列化**

追加：

```python
def serialize_ticket(ticket: Ticket) -> JsonObject:
    return {
        "ticket_no": ticket.ticket_no,
        "summary": ticket.summary,
        "category": ticket.category.value,
        "priority": ticket.priority.value,
        "status": ticket.status.value,
        "created_at": ticket.created_at,
        "updated_at": ticket.updated_at,
    }


def serialize_ticket_note(
    *,
    ticket_no: str,
    note: TicketComment,
) -> JsonObject:
    return {
        "ticket_no": ticket_no,
        "seq": note.seq,
        "visibility": note.visibility.value,
        "content": note.content,
        "created_at": note.created_at,
    }
```

- [ ] **Step 7: 运行结果测试和全量回归**

Run:

```powershell
python -m pytest tests/tools/test_support_results.py -q
python -m pytest -q
```

Expected: 两条命令全部 PASS。

- [ ] **Step 8: 提交 Task 2**

```powershell
git add app/tools/support_results.py tests/tools/test_support_results.py
git commit -m "feat: serialize support tool results"
```

---

### Task 3: 建立上下文绑定 Gateway 并完成两个只读工具

**独立验收产物：** `bind(context)` 返回只读工具函数；函数始终使用绑定的上下文；模型试图提交租户字段时参数校验失败且 Service 不会被调用；查询错误映射为稳定结果。

**Files:**
- Create: `app/tools/support_gateway.py`
- Create: `tests/tools/test_support_gateway.py`

**Interfaces:**
- Consumes: `CustomerSupportService`
- Consumes: `SUPPORT_ARGUMENT_MODELS`
- Consumes: Task 2 serializers
- Produces: `ToolFunction`
- Produces: `CustomerSupportToolGateway.bind`
- Produces: `CustomerSupportToolGateway.execute`
- Initially exposes: `get_order`、`get_logistics`

- [ ] **Step 1: 写记录上下文的 Fake Service**

创建 `tests/tools/test_support_gateway.py`：

```python
from dataclasses import dataclass, field
import json

import pytest

from app.application.customer_support_service import (
    LogisticsAvailability,
    LogisticsDetails,
    OrderDetails,
    SupportOrderNotFoundError,
)
from app.application.organization_service import TenantContext
from app.customers.base import Customer
from app.orders.base import Order, OrderStatus
from app.organizations.base import MembershipRole
from app.tools.support_gateway import CustomerSupportToolGateway


CUSTOMER = Customer(
    customer_id="customer-id",
    organization_id="org-a",
    customer_no="CUST-001",
    name="林晓",
    email="linxiao@example.test",
    phone=None,
    created_at="2026-07-20T08:00:00+00:00",
)

ORDER = Order(
    order_id="order-id",
    organization_id="org-a",
    order_no="ORD-DELAY-001",
    customer_id=CUSTOMER.customer_id,
    status=OrderStatus.PROCESSING,
    item_summary="无线耳机 x1",
    total_amount_cents=39900,
    currency="CNY",
    placed_at="2026-07-23T08:00:00+00:00",
    promised_ship_at="2026-07-25T08:00:00+00:00",
    created_at="2026-07-23T08:00:00+00:00",
    updated_at="2026-07-23T08:00:00+00:00",
)

CONTEXT = TenantContext(
    user_id="user-a",
    organization_id="org-a",
    role=MembershipRole.AGENT,
)


@dataclass
class FakeSupportService:
    calls: list[tuple] = field(default_factory=list)
    fail_with: Exception | None = None

    def get_order(self, *, context, order_no):
        self.calls.append(("get_order", context, order_no))
        if self.fail_with is not None:
            raise self.fail_with
        return OrderDetails(order=ORDER, customer=CUSTOMER)

    def get_logistics(self, *, context, order_no):
        self.calls.append(
            ("get_logistics", context, order_no)
        )
        if self.fail_with is not None:
            raise self.fail_with
        return LogisticsDetails(
            order=ORDER,
            customer=CUSTOMER,
            availability=LogisticsAvailability.NOT_CREATED,
            shipment=None,
        )
```

- [ ] **Step 2: 写可信上下文绑定和租户注入攻击测试**

追加：

```python
def test_bound_get_order_uses_server_context():
    service = FakeSupportService()
    functions = CustomerSupportToolGateway(
        service=service
    ).bind(context=CONTEXT)

    result = functions["get_order"](
        order_no="  ORD-DELAY-001  ",
    )

    assert result["ok"] is True
    assert result["data"]["order"]["order_no"] == (
        "ORD-DELAY-001"
    )
    assert service.calls == [
        ("get_order", CONTEXT, "ORD-DELAY-001")
    ]


@pytest.mark.parametrize(
    "forbidden_name",
    [
        "organization_id",
        "user_id",
        "actor_user_id",
        "role",
        "context",
    ],
)
def test_bound_tool_rejects_model_controlled_context(
    forbidden_name,
):
    service = FakeSupportService()
    functions = CustomerSupportToolGateway(
        service=service
    ).bind(context=CONTEXT)

    result = functions["get_order"](
        order_no="ORD-DELAY-001",
        **{forbidden_name: "org-b"},
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_ARGUMENTS"
    assert service.calls == []
```

- [ ] **Step 3: 写物流、业务错误和意外异常测试**

追加：

```python
def test_bound_get_logistics_returns_not_created_as_success():
    service = FakeSupportService()
    functions = CustomerSupportToolGateway(
        service=service
    ).bind(context=CONTEXT)

    result = functions["get_logistics"](
        order_no="ORD-DELAY-001",
    )

    assert result["ok"] is True
    assert result["data"]["availability"] == "not_created"
    assert result["data"]["shipment"] is None


def test_gateway_maps_order_not_found():
    service = FakeSupportService(
        fail_with=SupportOrderNotFoundError(
            "database-specific detail"
        )
    )
    functions = CustomerSupportToolGateway(
        service=service
    ).bind(context=CONTEXT)

    result = functions["get_order"](
        order_no="ORD-MISSING",
    )

    assert result == {
        "ok": False,
        "error": {
            "code": "ORDER_NOT_FOUND",
            "message": "order not found",
        },
    }


def test_gateway_hides_unexpected_exception(caplog):
    service = FakeSupportService(
        fail_with=RuntimeError(
            "database password and stack detail"
        )
    )
    functions = CustomerSupportToolGateway(
        service=service
    ).bind(context=CONTEXT)

    result = functions["get_order"](
        order_no="ORD-DELAY-001",
    )

    assert result == {
        "ok": False,
        "error": {
            "code": "INTERNAL_ERROR",
            "message": "tool execution failed",
        },
    }
    assert "database password" not in str(result)
    assert "support tool failed" in caplog.text


def test_gateway_rejects_unknown_tool_without_service_call():
    service = FakeSupportService()
    gateway = CustomerSupportToolGateway(service=service)

    result = gateway.execute(
        context=CONTEXT,
        tool_name="unknown_tool",
        arguments={},
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "UNKNOWN_TOOL"
    assert service.calls == []
```

- [ ] **Step 4: 运行测试并确认失败**

Run:

```powershell
python -m pytest tests/tools/test_support_gateway.py -q
```

Expected: collection FAIL，因为 `support_gateway.py` 尚不存在。

- [ ] **Step 5: 创建 Gateway、绑定函数和参数错误转换**

创建 `app/tools/support_gateway.py`：

```python
import logging
from typing import Any, Protocol

from pydantic import ValidationError

from app.application.customer_support_service import (
    CustomerSupportService,
    InvalidSupportRequestError,
    SupportDataIntegrityError,
    SupportOrderNotFoundError,
)
from app.application.organization_service import TenantContext
from app.tools.support_arguments import SUPPORT_ARGUMENT_MODELS
from app.tools.support_results import (
    JsonObject,
    serialize_logistics_details,
    serialize_order_details,
    tool_failure,
    tool_success,
)


logger = logging.getLogger(__name__)


class ToolFunction(Protocol):
    def __call__(self, **arguments: Any) -> JsonObject:
        raise NotImplementedError


class CustomerSupportToolGateway:
    def __init__(
        self,
        *,
        service: CustomerSupportService,
    ):
        self._service = service

    def _bind_one(
        self,
        *,
        context: TenantContext,
        tool_name: str,
    ) -> ToolFunction:
        def invoke(**arguments: Any) -> JsonObject:
            return self.execute(
                context=context,
                tool_name=tool_name,
                arguments=arguments,
            )

        return invoke

    def bind(
        self,
        *,
        context: TenantContext,
    ) -> dict[str, ToolFunction]:
        return {
            "get_order": self._bind_one(
                context=context,
                tool_name="get_order",
            ),
            "get_logistics": self._bind_one(
                context=context,
                tool_name="get_logistics",
            ),
        }

    @staticmethod
    def _validation_failure(
        exc: ValidationError,
    ) -> JsonObject:
        details = [
            {
                "type": item["type"],
                "loc": list(item["loc"]),
                "msg": item["msg"],
            }
            for item in exc.errors(
                include_input=False,
                include_url=False,
            )
        ]
        return tool_failure(
            code="INVALID_ARGUMENTS",
            message="tool arguments are invalid",
            details=details,
        )
```

- [ ] **Step 6: 实现两个只读工具和错误边界**

在 `CustomerSupportToolGateway` 中追加：

```python
    def execute(
        self,
        *,
        context: TenantContext,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> JsonObject:
        argument_model = SUPPORT_ARGUMENT_MODELS.get(
            tool_name
        )
        if argument_model is None or tool_name not in {
            "get_order",
            "get_logistics",
        }:
            return tool_failure(
                code="UNKNOWN_TOOL",
                message="tool is not available",
            )

        try:
            parsed = argument_model.model_validate(arguments)
        except ValidationError as exc:
            return self._validation_failure(exc)

        try:
            if tool_name == "get_order":
                details = self._service.get_order(
                    context=context,
                    order_no=parsed.order_no,
                )
                return tool_success(
                    serialize_order_details(details)
                )

            details = self._service.get_logistics(
                context=context,
                order_no=parsed.order_no,
            )
            return tool_success(
                serialize_logistics_details(details)
            )
        except InvalidSupportRequestError:
            return tool_failure(
                code="INVALID_ARGUMENTS",
                message="tool arguments are invalid",
            )
        except SupportOrderNotFoundError:
            return tool_failure(
                code="ORDER_NOT_FOUND",
                message="order not found",
            )
        except SupportDataIntegrityError:
            return tool_failure(
                code="DATA_INTEGRITY_ERROR",
                message="support data is inconsistent",
            )
        except Exception:
            logger.exception(
                "support tool failed",
                extra={"tool_name": tool_name},
            )
            return tool_failure(
                code="INTERNAL_ERROR",
                message="tool execution failed",
            )
```

- [ ] **Step 7: 运行 Gateway 测试和全量回归**

Run:

```powershell
python -m pytest tests/tools/test_support_gateway.py -q
python -m pytest -q
```

Expected: 两条命令全部 PASS。

- [ ] **Step 8: 提交 Task 3**

```powershell
git add app/tools/support_gateway.py tests/tools/test_support_gateway.py
git commit -m "feat: bind tenant context to support query tools"
```

---

### Task 4: 增加创建工单和内部备注两个写工具

**独立验收产物：** Gateway 暴露四个完整工具；写工具只使用绑定上下文；字符串枚举转换正确；所有预期业务异常都有稳定错误码。

**Files:**
- Modify: `app/tools/support_gateway.py`
- Modify: `tests/tools/test_support_gateway.py`

**Interfaces:**
- Consumes: `CreateTicketArguments`
- Consumes: `AddTicketNoteArguments`
- Consumes: `CustomerSupportService.create_ticket`
- Consumes: `CustomerSupportService.add_ticket_note`
- Produces: bound `create_ticket`
- Produces: bound `add_ticket_note`

- [ ] **Step 1: 扩展 Fake Service 支持写操作**

在 `tests/tools/test_support_gateway.py` imports 加入：

```python
from app.tickets.base import (
    Ticket,
    TicketCategory,
    TicketComment,
    TicketCommentVisibility,
    TicketPriority,
    TicketStatus,
)
```

在测试常量区域增加：

```python
TICKET = Ticket(
    ticket_id="ticket-id",
    organization_id="org-a",
    ticket_no="TKT-NEW-001",
    customer_id=CUSTOMER.customer_id,
    order_id=ORDER.order_id,
    created_by_user_id=CONTEXT.user_id,
    assigned_to_user_id=CONTEXT.user_id,
    summary="订单超过承诺时间仍未发货",
    category=TicketCategory.LOGISTICS,
    priority=TicketPriority.HIGH,
    status=TicketStatus.OPEN,
    created_at="2026-07-31T08:00:00+00:00",
    updated_at="2026-07-31T08:00:00+00:00",
)

NOTE = TicketComment(
    comment_id="comment-id",
    organization_id="org-a",
    ticket_id=TICKET.ticket_id,
    seq=1,
    author_user_id=CONTEXT.user_id,
    visibility=TicketCommentVisibility.INTERNAL,
    content="已联系仓库核查。",
    created_at="2026-07-31T08:01:00+00:00",
)
```

在 `FakeSupportService` 中增加：

```python
    def create_ticket(self, **arguments):
        self.calls.append(("create_ticket", arguments))
        if self.fail_with is not None:
            raise self.fail_with
        return TICKET

    def add_ticket_note(self, **arguments):
        self.calls.append(("add_ticket_note", arguments))
        if self.fail_with is not None:
            raise self.fail_with
        return NOTE
```

- [ ] **Step 2: 写创建工单绑定上下文和枚举转换测试**

追加：

```python
def test_bound_create_ticket_injects_context_and_converts_enums():
    service = FakeSupportService()
    functions = CustomerSupportToolGateway(
        service=service
    ).bind(context=CONTEXT)

    result = functions["create_ticket"](
        order_no="ORD-DELAY-001",
        summary="订单超过承诺时间仍未发货",
        category="logistics",
        priority="high",
    )

    assert result["ok"] is True
    assert result["data"]["ticket_no"] == "TKT-NEW-001"
    call_name, arguments = service.calls[0]
    assert call_name == "create_ticket"
    assert arguments == {
        "context": CONTEXT,
        "summary": "订单超过承诺时间仍未发货",
        "category": TicketCategory.LOGISTICS,
        "priority": TicketPriority.HIGH,
        "customer_no": None,
        "order_no": "ORD-DELAY-001",
    }
```

- [ ] **Step 3: 写内部备注工具测试**

追加：

```python
def test_bound_add_ticket_note_uses_context_and_hides_ids():
    service = FakeSupportService()
    functions = CustomerSupportToolGateway(
        service=service
    ).bind(context=CONTEXT)

    result = functions["add_ticket_note"](
        ticket_no="TKT-NEW-001",
        content="  已联系仓库核查。  ",
    )

    assert result["ok"] is True
    assert result["data"]["ticket_no"] == "TKT-NEW-001"
    assert result["data"]["visibility"] == "internal"
    assert "comment_id" not in result["data"]
    assert service.calls == [
        (
            "add_ticket_note",
            {
                "context": CONTEXT,
                "ticket_no": "TKT-NEW-001",
                "content": "已联系仓库核查。",
            },
        )
    ]


def test_invalid_create_ticket_result_is_json_safe():
    service = FakeSupportService()
    functions = CustomerSupportToolGateway(
        service=service
    ).bind(context=CONTEXT)

    result = functions["create_ticket"](
        summary="客户问题需要创建工单",
        category="other",
        priority="medium",
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_ARGUMENTS"
    json.dumps(result, ensure_ascii=False)
    assert service.calls == []
```

- [ ] **Step 4: 写应用层错误映射测试**

在 imports 中加入：

```python
from app.application.customer_support_service import (
    OrderCustomerMismatchError,
    SupportCustomerNotFoundError,
    SupportOperationRejectedError,
    SupportTicketNotFoundError,
    TicketNumberGenerationError,
)
```

追加：

```python
@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (
            SupportCustomerNotFoundError("private detail"),
            "CUSTOMER_NOT_FOUND",
        ),
        (
            OrderCustomerMismatchError("private detail"),
            "ORDER_CUSTOMER_MISMATCH",
        ),
        (
            SupportTicketNotFoundError("private detail"),
            "TICKET_NOT_FOUND",
        ),
        (
            SupportOperationRejectedError("private detail"),
            "OPERATION_REJECTED",
        ),
        (
            TicketNumberGenerationError("private detail"),
            "TICKET_NUMBER_GENERATION_FAILED",
        ),
    ],
)
def test_gateway_maps_write_errors_without_private_detail(
    error,
    expected_code,
):
    service = FakeSupportService(fail_with=error)
    functions = CustomerSupportToolGateway(
        service=service
    ).bind(context=CONTEXT)

    result = functions["create_ticket"](
        order_no="ORD-DELAY-001",
        summary="订单超过承诺时间仍未发货",
        category="logistics",
        priority="high",
    )

    assert result["ok"] is False
    assert result["error"]["code"] == expected_code
    assert "private detail" not in str(result)
```

- [ ] **Step 5: 运行测试并确认失败**

Run:

```powershell
python -m pytest tests/tools/test_support_gateway.py -q
```

Expected: FAIL，因为 Gateway 尚未绑定和分发两个写工具。

- [ ] **Step 6: 增加写工具 imports 和绑定**

在 `app/tools/support_gateway.py` 的 Application Service imports 中加入：

```python
    OrderCustomerMismatchError,
    SupportCustomerNotFoundError,
    SupportOperationRejectedError,
    SupportTicketNotFoundError,
    TicketNumberGenerationError,
```

在结果 imports 中加入：

```python
    serialize_ticket,
    serialize_ticket_note,
```

把 `bind()` 返回值修改为四个工具：

```python
    def bind(
        self,
        *,
        context: TenantContext,
    ) -> dict[str, ToolFunction]:
        return {
            tool_name: self._bind_one(
                context=context,
                tool_name=tool_name,
            )
            for tool_name in SUPPORT_ARGUMENT_MODELS
        }
```

- [ ] **Step 7: 重构分发逻辑为独立方法**

在 Gateway 中增加：

```python
    def _dispatch(
        self,
        *,
        context: TenantContext,
        tool_name: str,
        parsed: Any,
    ) -> JsonObject:
        if tool_name == "get_order":
            details = self._service.get_order(
                context=context,
                order_no=parsed.order_no,
            )
            return tool_success(
                serialize_order_details(details)
            )

        if tool_name == "get_logistics":
            details = self._service.get_logistics(
                context=context,
                order_no=parsed.order_no,
            )
            return tool_success(
                serialize_logistics_details(details)
            )

        if tool_name == "create_ticket":
            ticket = self._service.create_ticket(
                context=context,
                summary=parsed.summary,
                category=parsed.category,
                priority=parsed.priority,
                customer_no=parsed.customer_no,
                order_no=parsed.order_no,
            )
            return tool_success(serialize_ticket(ticket))

        note = self._service.add_ticket_note(
            context=context,
            ticket_no=parsed.ticket_no,
            content=parsed.content,
        )
        return tool_success(
            serialize_ticket_note(
                ticket_no=parsed.ticket_no,
                note=note,
            )
        )
```

在 `execute()` 中：

1. 删除只允许两个查询工具的集合判断，只保留 `argument_model is None`。
2. 用下面的调用替换原有查询分支：

```python
return self._dispatch(
    context=context,
    tool_name=tool_name,
    parsed=parsed,
)
```

- [ ] **Step 8: 补齐预期异常映射**

在 `execute()` 的 `except SupportOrderNotFoundError` 后依次增加：

```python
        except SupportCustomerNotFoundError:
            return tool_failure(
                code="CUSTOMER_NOT_FOUND",
                message="customer not found",
            )
        except OrderCustomerMismatchError:
            return tool_failure(
                code="ORDER_CUSTOMER_MISMATCH",
                message="order does not belong to customer",
            )
        except SupportTicketNotFoundError:
            return tool_failure(
                code="TICKET_NOT_FOUND",
                message="ticket not found",
            )
        except SupportOperationRejectedError:
            return tool_failure(
                code="OPERATION_REJECTED",
                message="support operation was rejected",
            )
        except TicketNumberGenerationError:
            return tool_failure(
                code="TICKET_NUMBER_GENERATION_FAILED",
                message="ticket number generation failed",
            )
```

保留原有 `SupportDataIntegrityError` 和最后的通用 `Exception` 处理。

- [ ] **Step 9: 运行完整 Gateway 测试和全量回归**

Run:

```powershell
python -m pytest tests/tools/test_support_gateway.py -q
python -m pytest -q
```

Expected: 两条命令全部 PASS。

- [ ] **Step 10: 提交 Task 4**

```powershell
git add app/tools/support_gateway.py tests/tools/test_support_gateway.py
git commit -m "feat: add controlled support command tools"
```

---

### Task 5: 生成与 Gateway 一致且不包含安全字段的工具定义

**独立验收产物：** 四个 OpenAI-compatible function definitions 从同一组参数模型生成；工具名与 Gateway 完全一致；定义中不出现任何可信上下文字段。

**Files:**
- Create: `app/tools/support_definitions.py`
- Create: `tests/tools/test_support_definitions.py`
- Modify: `app/tools/support_gateway.py`

**Interfaces:**
- Consumes: `SUPPORT_ARGUMENT_MODELS`
- Produces: `get_support_tool_definitions() -> list[dict]`
- Produces: `CustomerSupportToolGateway.definitions`

- [ ] **Step 1: 写定义集合和安全字段失败测试**

创建 `tests/tools/test_support_definitions.py`：

```python
import json

from app.application.organization_service import TenantContext
from app.organizations.base import MembershipRole
from app.tools.support_definitions import (
    get_support_tool_definitions,
)
from app.tools.support_gateway import CustomerSupportToolGateway


class EmptyService:
    pass


def tool_names(definitions):
    return {
        item["function"]["name"]
        for item in definitions
    }


def test_definitions_have_the_four_gateway_tool_names():
    gateway = CustomerSupportToolGateway(
        service=EmptyService()
    )
    definitions = get_support_tool_definitions()
    functions = gateway.bind(
        context=TenantContext(
            user_id="user-a",
            organization_id="org-a",
            role=MembershipRole.AGENT,
        )
    )

    assert tool_names(definitions) == set(functions) == {
        "get_order",
        "get_logistics",
        "create_ticket",
        "add_ticket_note",
    }


def test_definitions_never_expose_trusted_context_fields():
    encoded = json.dumps(
        get_support_tool_definitions(),
        ensure_ascii=False,
    )

    for forbidden_name in (
        "organization_id",
        "user_id",
        "actor_user_id",
        "role",
        "context",
    ):
        assert forbidden_name not in encoded


def test_definitions_forbid_additional_properties():
    definitions = get_support_tool_definitions()

    for definition in definitions:
        parameters = definition["function"]["parameters"]
        assert parameters["type"] == "object"
        assert parameters["additionalProperties"] is False


def test_create_ticket_definition_describes_target_rule():
    definitions = get_support_tool_definitions()
    create_ticket = next(
        item
        for item in definitions
        if item["function"]["name"] == "create_ticket"
    )

    description = create_ticket["function"]["description"]
    assert "customer_no" in description
    assert "order_no" in description
    assert "至少" in description
```

- [ ] **Step 2: 运行测试并确认失败**

Run:

```powershell
python -m pytest tests/tools/test_support_definitions.py -q
```

Expected: collection FAIL，因为 `support_definitions.py` 尚不存在。

- [ ] **Step 3: 实现由 Pydantic 模型生成定义**

创建 `app/tools/support_definitions.py`：

```python
from copy import deepcopy

from app.tools.support_arguments import SUPPORT_ARGUMENT_MODELS


TOOL_DESCRIPTIONS = {
    "get_order": (
        "查询当前企业内的订单和对应客户信息。"
    ),
    "get_logistics": (
        "查询当前企业内订单的物流状态；订单存在但未发货时"
        "会返回 not_created。"
    ),
    "create_ticket": (
        "为当前企业创建客服工单；customer_no 和 order_no "
        "至少提供一个，同时提供时必须属于同一客户。"
    ),
    "add_ticket_note": (
        "以当前登录客服身份为当前企业的工单添加内部备注。"
    ),
}


def get_support_tool_definitions() -> list[dict]:
    definitions: list[dict] = []
    for tool_name, argument_model in (
        SUPPORT_ARGUMENT_MODELS.items()
    ):
        parameters = deepcopy(
            argument_model.model_json_schema()
        )
        parameters.pop("title", None)
        definitions.append(
            {
                "type": "function",
                "function": {
                    "name": tool_name,
                    "description": (
                        TOOL_DESCRIPTIONS[tool_name]
                    ),
                    "parameters": parameters,
                },
            }
        )
    return definitions
```

- [ ] **Step 4: 给 Gateway 增加只读 definitions 属性**

在 `app/tools/support_gateway.py` imports 中加入：

```python
from app.tools.support_definitions import (
    get_support_tool_definitions,
)
```

在 `CustomerSupportToolGateway` 中增加：

```python
    @property
    def definitions(self) -> list[dict]:
        return get_support_tool_definitions()
```

在定义测试中追加：

```python
def test_gateway_definitions_returns_fresh_value():
    gateway = CustomerSupportToolGateway(
        service=EmptyService()
    )

    first = gateway.definitions
    second = gateway.definitions
    first.clear()

    assert len(second) == 4
```

- [ ] **Step 5: 运行定义测试和所有工具测试**

Run:

```powershell
python -m pytest tests/tools -q
```

Expected: 全部 PASS。

- [ ] **Step 6: 运行全量回归并提交**

Run:

```powershell
python -m pytest -q
```

Expected: 全部 PASS。

Commit:

```powershell
git add app/tools/support_definitions.py tests/tools/test_support_definitions.py app/tools/support_gateway.py
git commit -m "feat: publish safe support tool definitions"
```

---

### Task 6: 提供 Gateway 工厂、真实数据黄金路径和边界文档

**独立验收产物：** 可以从数据库路径构造 Gateway，使用模拟业务数据走通四个工具；文档明确第四步尚未把工具接入 Agent。

**Files:**
- Create: `app/tools/support_factory.py`
- Create: `tests/tools/test_support_factory.py`
- Create: `docs/customer-support-tool-gateway.md`

**Interfaces:**
- Consumes: `create_customer_support_service`
- Produces: `create_customer_support_tool_gateway`
- Verifies: four-tool golden path without LLM

- [ ] **Step 1: 写真实 SQLite 黄金路径失败测试**

创建 `tests/tools/test_support_factory.py`：

```python
from datetime import datetime, timezone

from app.application.organization_service import TenantContext
from app.customers.sqlite_store import SQLiteCustomerStore
from app.demo_data.seeder import DemoDataSeeder
from app.orders.sqlite_store import SQLiteOrderStore
from app.organizations.base import MembershipRole
from app.organizations.sqlite_store import SQLiteOrganizationStore
from app.shipments.sqlite_store import SQLiteShipmentStore
from app.tickets.sqlite_store import SQLiteTicketStore
from app.tools.support_factory import (
    create_customer_support_tool_gateway,
)
from app.users.sqlite_store import SQLiteUserStore


def test_gateway_factory_completes_four_tool_golden_path(
    tmp_path,
):
    database_path = tmp_path / "support.db"
    users = SQLiteUserStore(database_path)
    organizations = SQLiteOrganizationStore(database_path)
    customers = SQLiteCustomerStore(database_path)
    orders = SQLiteOrderStore(database_path)
    shipments = SQLiteShipmentStore(database_path)
    tickets = SQLiteTicketStore(database_path)

    user = users.create_user(
        username="alice",
        password_hash="hash",
    )
    organization = organizations.create_with_admin(
        name="Company A",
        admin_user_id=user.user_id,
    )
    DemoDataSeeder(
        organization_store=organizations,
        customer_store=customers,
        order_store=orders,
        shipment_store=shipments,
        ticket_store=tickets,
    ).seed(
        organization_id=organization.organization_id,
        actor_user_id=user.user_id,
        reference_time=datetime(
            2026,
            7,
            31,
            8,
            0,
            tzinfo=timezone.utc,
        ),
    )
    context = TenantContext(
        user_id=user.user_id,
        organization_id=organization.organization_id,
        role=MembershipRole.ADMIN,
    )
    gateway = create_customer_support_tool_gateway(
        database_path,
        ticket_no_factory=lambda: "TKT-GATEWAY-001",
    )
    functions = gateway.bind(context=context)

    order_result = functions["get_order"](
        order_no="ORD-DELAY-001",
    )
    logistics_result = functions["get_logistics"](
        order_no="ORD-DELAY-001",
    )
    ticket_result = functions["create_ticket"](
        order_no="ORD-DELAY-001",
        summary="订单超过承诺时间仍未发货",
        category="logistics",
        priority="high",
    )
    note_result = functions["add_ticket_note"](
        ticket_no="TKT-GATEWAY-001",
        content="已联系仓库核查发货状态。",
    )

    assert order_result["ok"] is True
    assert logistics_result["data"]["availability"] == (
        "not_created"
    )
    assert ticket_result["data"]["ticket_no"] == (
        "TKT-GATEWAY-001"
    )
    assert note_result["data"]["visibility"] == "internal"
    assert len(gateway.definitions) == 4
```

- [ ] **Step 2: 运行测试并确认失败**

Run:

```powershell
python -m pytest tests/tools/test_support_factory.py -q
```

Expected: collection FAIL，因为 `support_factory.py` 尚不存在。

- [ ] **Step 3: 实现 Gateway 工厂**

创建 `app/tools/support_factory.py`：

```python
from pathlib import Path

from app.application.customer_support_factory import (
    create_customer_support_service,
)
from app.application.customer_support_service import (
    TicketNumberFactory,
)
from app.tools.support_gateway import (
    CustomerSupportToolGateway,
)


def create_customer_support_tool_gateway(
    database_path: str | Path,
    *,
    ticket_no_factory: TicketNumberFactory | None = None,
) -> CustomerSupportToolGateway:
    service = create_customer_support_service(
        database_path,
        ticket_no_factory=ticket_no_factory,
    )
    return CustomerSupportToolGateway(service=service)
```

- [ ] **Step 4: 运行黄金路径测试**

Run:

```powershell
python -m pytest tests/tools/test_support_factory.py -q
```

Expected: `1 passed`。

- [ ] **Step 5: 编写第四步使用和安全边界文档**

创建 `docs/customer-support-tool-gateway.md`，内容必须包含：

```markdown
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
“物流延迟 → 查询订单 → 查询物流 → 创建工单 → 生成回复”黄金路径。

在完成第五步之前，不修改 `main.py` 的全局工具注册方式。
```

- [ ] **Step 6: 验证工具定义不含租户字段**

Run:

```powershell
python -c "import json; from app.tools.support_definitions import get_support_tool_definitions; value=json.dumps(get_support_tool_definitions()); forbidden={'organization_id','user_id','actor_user_id','role','context'}; assert not any(item in value for item in forbidden); print('SAFE_TOOL_SCHEMA=PASS')"
```

Expected:

```text
SAFE_TOOL_SCHEMA=PASS
```

- [ ] **Step 7: 运行第四步专项验收**

Run:

```powershell
python -m pytest `
  tests/tools/test_support_arguments.py `
  tests/tools/test_support_results.py `
  tests/tools/test_support_gateway.py `
  tests/tools/test_support_definitions.py `
  tests/tools/test_support_factory.py `
  tests/application/test_customer_support_service.py `
  -q
```

Expected: 全部 PASS。

- [ ] **Step 8: 确认未提前接入 Agent**

Run:

```powershell
Select-String `
  -Path .\main.py,.\app\agent\runner.py,.\app\application\chat_service.py,.\app\tools\registry.py `
  -Pattern "CustomerSupportToolGateway|support_factory|support_gateway"
```

Expected: 无输出。

- [ ] **Step 9: 运行全量回归**

Run:

```powershell
python -m pytest -q
```

Expected: 全部 PASS。

- [ ] **Step 10: 提交 Task 6**

```powershell
git add app/tools/support_factory.py tests/tools/test_support_factory.py docs/customer-support-tool-gateway.md
git commit -m "docs: add controlled support tool gateway entrypoint"
```

---

## 5. 最终验收清单

- [ ] `python -m pytest -q` 全部通过。
- [ ] Alembic head 仍为 `0008_ticket_comments`。
- [ ] 工具名正好是 `get_order`、`get_logistics`、`create_ticket`、`add_ticket_note`。
- [ ] 四个工具定义和四个绑定函数名称完全一致。
- [ ] 工具定义中没有 `organization_id`、`user_id`、`actor_user_id`、`role` 或 `context`。
- [ ] 工具参数出现额外字段时返回 `INVALID_ARGUMENTS`，Service 没有被调用。
- [ ] Service 收到的上下文与 `gateway.bind()` 时传入的是同一个 `TenantContext`。
- [ ] `category` 和 `priority` 已转换为领域枚举。
- [ ] “订单存在但无物流”返回成功和 `not_created`。
- [ ] 所有成功和失败结果都能被 `json.dumps()` 序列化。
- [ ] 模型可见结果中没有数据库内部 UUID。
- [ ] 预期 Application Service 异常映射为稳定错误码。
- [ ] 意外异常不会把异常文本或堆栈返回给模型。
- [ ] `add_ticket_note` 不允许模型指定作者、租户或可见性。
- [ ] `main.py`、Agent runner、`ChatService` 和旧工具注册表没有改动。
- [ ] `docs/customer-support-tool-gateway.md` 与代码接口一致。

## 6. 第四步完成后的边界

完成本计划后，开发顺序状态为：

```text
1. 最小多租户基础                   已完成
2. 模拟客服业务数据                 已完成
3. 不依赖 LLM 的确定性业务服务      已完成
4. 受控 Tool Gateway               本计划完成后即完成
5. Agent 选择工具并生成回复         下一步
6. RAG、审批和 LangGraph            后续
```

第五步的关键变化是“按请求绑定工具”，不能在应用启动时创建一个不带用户上下文的
全局客服工具字典。推荐下一份计划名称：

```text
2026-xx-xx-customer-support-agent-golden-path.md
```
