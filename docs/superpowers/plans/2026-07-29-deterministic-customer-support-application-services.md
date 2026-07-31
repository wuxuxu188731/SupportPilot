# Deterministic Customer Support Application Services Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有多租户客服业务数据和 Store 之上，实现一组不依赖 LLM、可直接测试和调用的确定性客服业务服务，完成订单查询、物流查询、创建工单和添加内部备注四个核心用例。

**Architecture:** 新增一个 `CustomerSupportService` 作为 Application Service，通过现有 `CustomerStore`、`OrderStore`、`ShipmentStore` 和 `TicketStore` 组合业务用例。所有公开方法只接收经过认证和成员校验后产生的 `TenantContext`，由服务内部提取 `organization_id` 和 `user_id`；查询结果使用稳定的 dataclass 表达，底层 Store 异常映射为 Application Service 异常。最后提供一个不依赖 FastAPI 和 LLM 的装配工厂，让普通 Python 代码和后续 Tool Gateway 都能复用同一业务入口。

**Tech Stack:** Python 3.10+、标准库 `dataclasses` / `enum` / `pathlib` / `typing` / `uuid`、现有同步 SQLite Store、pytest。

## Global Constraints

- 当前代码基线为 Alembic `0008_ticket_comments`，计划编写前全量测试为 `128 passed`。
- 本计划只实现确定性 Application Service，不修改数据库表，不新增 Alembic migration。
- 本计划不实现 HTTP 客服业务 API、Agent 工具、Tool Gateway、RAG、退款、审批、LangGraph、前端页面或真实第三方系统。
- 不修改 `app/tools/registry.py`，现有 `get_food` 和 `send_popup_to_user` 仍保持不变。
- 不在 `main.py` 中把客服业务方法直接注册为 LLM 工具；这是下一阶段 Tool Gateway 的职责。
- `CustomerSupportService` 的公开方法必须接收 `TenantContext`，不得接收裸的 `organization_id` 或 `actor_user_id`。
- `organization_id` 必须始终来自 `context.organization_id`，创建人和备注作者必须始终来自 `context.user_id`。
- `TenantContext` 被视为可信的应用层授权上下文；真实入口必须通过现有 `OrganizationService.get_tenant_context()` 或 FastAPI 的 `get_current_tenant` 产生它。
- `admin` 和 `agent` 都可以执行本计划中的四个低风险客服用例；本阶段不新增角色。
- 所有业务编号和文本参数进入 Store 前必须去除首尾空白；空字符串必须在 Service 层拒绝。
- 订单不存在必须抛出稳定的 `SupportOrderNotFoundError`，不能把 `OrderNotFoundError` 直接暴露给上层。
- 物流查询必须区分“订单不存在”和“订单存在但尚未产生物流记录”；后者是正常业务结果，不是异常。
- 创建工单时必须由 Service 设置 `created_by_user_id=context.user_id`、`assigned_to_user_id=context.user_id` 和 `status=TicketStatus.OPEN`。
- 创建工单时如果提供订单号，必须从订单推导客户；同时提供客户号时，两者必须指向同一个客户。
- 添加备注只创建 `TicketCommentVisibility.INTERNAL` 的内部备注；公开回复不在本计划范围内。
- Application Service 不得导入 OpenAI/DeepSeek 客户端、`app.agent`、FastAPI 或 `app.tools`。
- 每个 Task 都采用 TDD：先写失败测试并确认失败，再做最小实现，最后运行局部测试和全量回归。
- 每个 Task 独立提交；开始 Task 前确认上一 Task 的测试和提交已经完成。

---

## 1. 完成标准

本计划完成后，普通 Python 代码可以在完全没有 LLM 的情况下执行：

```python
order_result = service.get_order(
    context=context,
    order_no="ORD-DELAY-001",
)

logistics_result = service.get_logistics(
    context=context,
    order_no="ORD-DELAY-001",
)

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

验收语义：

| 用例 | 输入 | 成功结果 | 关键失败结果 |
|---|---|---|---|
| 查询订单 | `TenantContext + order_no` | `OrderDetails` | `SupportOrderNotFoundError` |
| 查询物流 | `TenantContext + order_no` | `LogisticsDetails` | 订单不存在抛异常；无物流返回 `NOT_CREATED` |
| 创建工单 | 上下文、客户/订单、摘要、分类、优先级 | `Ticket` | 客户不存在、订单不存在、客户订单不匹配、参数非法 |
| 添加备注 | `TenantContext + ticket_no + content` | `TicketComment` | 工单不存在、内容非法、底层拒绝写入 |

## 2. 文件结构

```text
app/application/
  customer_support_service.py
    业务结果模型、应用层异常、四个确定性客服用例

  customer_support_factory.py
    使用 SQLite Store 装配 CustomerSupportService

tests/application/
  test_customer_support_service.py
    四个业务用例的规则、错误映射和租户隔离测试

  test_customer_support_factory.py
    从模拟数据到业务服务的无 LLM 集成测试

docs/
  customer-support-services.md
    服务边界、直接调用方式、错误语义和下一阶段边界
```

现有 Store 协议保持不变：

```text
CustomerStore
  get_by_id(organization_id, customer_id)
  get_by_no(organization_id, customer_no)

OrderStore
  get_by_no(organization_id, order_no)

ShipmentStore
  get_by_order_no(organization_id, order_no)

TicketStore
  create_ticket
  get_by_no(organization_id, ticket_no)
  add_comment
```

## 3. 最终公开接口

最终 `CustomerSupportService` 必须提供以下签名：

| 方法 | 参数 | 返回值 |
|---|---|---|
| `get_order` | `context: TenantContext, order_no: str` | `OrderDetails` |
| `get_logistics` | `context: TenantContext, order_no: str` | `LogisticsDetails` |
| `create_ticket` | `context: TenantContext, summary: str, category: TicketCategory, priority: TicketPriority, customer_no: str \| None = None, order_no: str \| None = None` | `Ticket` |
| `add_ticket_note` | `context: TenantContext, ticket_no: str, content: str` | `TicketComment` |

---

### Task 1: 建立 Application Service 契约并完成订单查询

**独立验收产物：** 可以通过 `TenantContext + order_no` 查询当前企业的订单及其客户；输入会被规范化；空订单号被拒绝；错误企业或不存在的订单统一返回 Application Service 异常。

**Files:**
- Create: `app/application/customer_support_service.py`
- Create: `tests/application/test_customer_support_service.py`

**Interfaces:**
- Consumes: `TenantContext`
- Consumes: `CustomerStore.get_by_id -> Customer`
- Consumes: `OrderStore.get_by_no -> Order`
- Produces: `OrderDetails`
- Produces: `InvalidSupportRequestError`
- Produces: `SupportOrderNotFoundError`
- Produces: `SupportDataIntegrityError`
- Produces: `CustomerSupportService.get_order -> OrderDetails`

- [ ] **Step 1: 写订单查询失败测试和共用测试装配**

创建 `tests/application/test_customer_support_service.py`：

```python
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.application.customer_support_service import (
    CustomerSupportService,
    InvalidSupportRequestError,
    SupportDataIntegrityError,
    SupportOrderNotFoundError,
)
from app.application.organization_service import TenantContext
from app.customers.base import Customer, CustomerNotFoundError
from app.customers.sqlite_store import SQLiteCustomerStore
from app.orders.base import Order, OrderStatus
from app.orders.sqlite_store import SQLiteOrderStore
from app.organizations.base import MembershipRole
from app.organizations.sqlite_store import SQLiteOrganizationStore
from app.shipments.sqlite_store import SQLiteShipmentStore
from app.tickets.sqlite_store import SQLiteTicketStore
from app.users.sqlite_store import SQLiteUserStore


@dataclass(frozen=True)
class SupportScope:
    database_path: Path
    service: CustomerSupportService
    context: TenantContext
    customer: Customer
    order: Order
    customer_store: SQLiteCustomerStore
    order_store: SQLiteOrderStore
    shipment_store: SQLiteShipmentStore
    ticket_store: SQLiteTicketStore


def build_support_scope(
    tmp_path,
    *,
    ticket_nos: Sequence[str] = ("TKT-NEW-001",),
) -> SupportScope:
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
    customer = customers.create_customer(
        organization_id=organization.organization_id,
        customer_no="CUST-001",
        name="林晓",
        email="linxiao@example.test",
        phone="+86-000-0000-0001",
    )
    order = orders.create_order(
        organization_id=organization.organization_id,
        order_no="ORD-DELAY-001",
        customer_id=customer.customer_id,
        status=OrderStatus.PROCESSING,
        item_summary="无线耳机 x1",
        total_amount_cents=39900,
        currency="CNY",
        placed_at="2026-07-23T08:00:00+00:00",
        promised_ship_at="2026-07-25T08:00:00+00:00",
    )
    number_iterator = iter(ticket_nos)
    service = CustomerSupportService(
        customer_store=customers,
        order_store=orders,
        shipment_store=shipments,
        ticket_store=tickets,
        ticket_no_factory=number_iterator.__next__,
    )
    return SupportScope(
        database_path=database_path,
        service=service,
        context=TenantContext(
            user_id=user.user_id,
            organization_id=organization.organization_id,
            role=MembershipRole.ADMIN,
        ),
        customer=customer,
        order=order,
        customer_store=customers,
        order_store=orders,
        shipment_store=shipments,
        ticket_store=tickets,
    )


def test_get_order_returns_order_and_customer_from_current_tenant(tmp_path):
    scope = build_support_scope(tmp_path)

    result = scope.service.get_order(
        context=scope.context,
        order_no="  ORD-DELAY-001  ",
    )

    assert result.order == scope.order
    assert result.customer == scope.customer


def test_get_order_rejects_blank_order_number(tmp_path):
    scope = build_support_scope(tmp_path)

    with pytest.raises(
        InvalidSupportRequestError,
        match="order_no must not be blank",
    ):
        scope.service.get_order(
            context=scope.context,
            order_no="   ",
        )


def test_get_order_maps_broken_customer_reference_to_integrity_error(
    tmp_path,
    monkeypatch,
):
    scope = build_support_scope(tmp_path)

    def raise_missing_customer(**kwargs):
        raise CustomerNotFoundError("customer not found")

    monkeypatch.setattr(
        scope.customer_store,
        "get_by_id",
        raise_missing_customer,
    )

    with pytest.raises(
        SupportDataIntegrityError,
        match="order references a missing customer",
    ):
        scope.service.get_order(
            context=scope.context,
            order_no=scope.order.order_no,
        )


@pytest.mark.parametrize(
    "organization_id",
    ["missing-organization", "another-organization"],
)
def test_get_order_hides_missing_or_other_tenant_order(
    tmp_path,
    organization_id,
):
    scope = build_support_scope(tmp_path)
    other_context = TenantContext(
        user_id=scope.context.user_id,
        organization_id=organization_id,
        role=MembershipRole.AGENT,
    )

    with pytest.raises(
        SupportOrderNotFoundError,
        match="order not found",
    ):
        scope.service.get_order(
            context=other_context,
            order_no="ORD-DELAY-001",
        )
```

- [ ] **Step 2: 运行测试并确认失败**

Run:

```powershell
python -m pytest tests/application/test_customer_support_service.py -q
```

Expected: collection FAIL，因为 `app.application.customer_support_service` 尚不存在。

- [ ] **Step 3: 创建结果类型、异常和 Service 构造函数**

创建 `app/application/customer_support_service.py`：

```python
from collections.abc import Callable
from dataclasses import dataclass
from uuid import uuid4

from app.application.organization_service import TenantContext
from app.customers.base import (
    Customer,
    CustomerNotFoundError,
    CustomerStore,
)
from app.orders.base import (
    Order,
    OrderNotFoundError,
    OrderStore,
)
from app.shipments.base import (
    ShipmentStore,
)
from app.tickets.base import (
    TicketStore,
)


TicketNumberFactory = Callable[[], str]


@dataclass(frozen=True)
class OrderDetails:
    order: Order
    customer: Customer


class InvalidSupportRequestError(ValueError):
    pass


class SupportOrderNotFoundError(LookupError):
    pass


class SupportDataIntegrityError(RuntimeError):
    pass


def generate_ticket_no() -> str:
    return f"TKT-{uuid4().hex.upper()}"


class CustomerSupportService:
    def __init__(
        self,
        *,
        customer_store: CustomerStore,
        order_store: OrderStore,
        shipment_store: ShipmentStore,
        ticket_store: TicketStore,
        ticket_no_factory: TicketNumberFactory | None = None,
    ):
        self._customer_store = customer_store
        self._order_store = order_store
        self._shipment_store = shipment_store
        self._ticket_store = ticket_store
        self._ticket_no_factory = ticket_no_factory or generate_ticket_no

    @staticmethod
    def _required_text(*, value: str, field_name: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise InvalidSupportRequestError(
                f"{field_name} must not be blank"
            )
        return normalized
```

- [ ] **Step 4: 实现订单查询和异常映射**

在 `CustomerSupportService` 中增加：

```python
    def get_order(
        self,
        *,
        context: TenantContext,
        order_no: str,
    ) -> OrderDetails:
        normalized_order_no = self._required_text(
            value=order_no,
            field_name="order_no",
        )
        try:
            order = self._order_store.get_by_no(
                organization_id=context.organization_id,
                order_no=normalized_order_no,
            )
        except OrderNotFoundError as exc:
            raise SupportOrderNotFoundError(
                "order not found"
            ) from exc

        try:
            customer = self._customer_store.get_by_id(
                organization_id=context.organization_id,
                customer_id=order.customer_id,
            )
        except CustomerNotFoundError as exc:
            raise SupportDataIntegrityError(
                "order references a missing customer"
            ) from exc

        return OrderDetails(
            order=order,
            customer=customer,
        )
```

- [ ] **Step 5: 运行订单查询测试**

Run:

```powershell
python -m pytest tests/application/test_customer_support_service.py -q
```

Expected: `5 passed`。

- [ ] **Step 6: 运行全量回归**

Run:

```powershell
python -m pytest -q
```

Expected: 全部 PASS，且通过数量不少于 `133`。

- [ ] **Step 7: 提交 Task 1**

```powershell
git add app/application/customer_support_service.py tests/application/test_customer_support_service.py
git commit -m "feat: add tenant-scoped order application service"
```

---

### Task 2: 实现能够区分“无订单”和“无物流”的物流查询

**独立验收产物：** 对存在物流的订单返回物流详情；对存在但尚未发货的订单返回 `NOT_CREATED`；订单不存在仍抛出 `SupportOrderNotFoundError`。

**Files:**
- Modify: `app/application/customer_support_service.py`
- Modify: `tests/application/test_customer_support_service.py`

**Interfaces:**
- Consumes: `CustomerSupportService.get_order -> OrderDetails`
- Consumes: `ShipmentStore.get_by_order_no -> Shipment`
- Produces: `LogisticsAvailability`
- Produces: `LogisticsDetails`
- Produces: `CustomerSupportService.get_logistics -> LogisticsDetails`

- [ ] **Step 1: 写“订单存在但无物流”的失败测试**

在测试文件 import 中加入：

```python
from app.application.customer_support_service import LogisticsAvailability
```

在 `tests/application/test_customer_support_service.py` 追加：

```python
def test_get_logistics_returns_not_created_for_order_without_shipment(
    tmp_path,
):
    scope = build_support_scope(tmp_path)

    result = scope.service.get_logistics(
        context=scope.context,
        order_no="ORD-DELAY-001",
    )

    assert result.order == scope.order
    assert result.customer == scope.customer
    assert result.availability is LogisticsAvailability.NOT_CREATED
    assert result.shipment is None
```

- [ ] **Step 2: 写“订单存在且有物流”的失败测试**

把以下 imports 加入测试文件：

```python
from app.shipments.base import ShipmentStatus
```

追加：

```python
def test_get_logistics_returns_existing_shipment(tmp_path):
    scope = build_support_scope(tmp_path)
    shipment = scope.shipment_store.create_shipment(
        organization_id=scope.context.organization_id,
        shipment_no="SHP-TRANSIT-001",
        order_id=scope.order.order_id,
        carrier="顺丰速运",
        tracking_no="SF-DEMO-TRANSIT-001",
        status=ShipmentStatus.IN_TRANSIT,
        last_event="快件已到达目的地分拨中心",
        shipped_at="2026-07-26T08:00:00+00:00",
        estimated_delivery_at="2026-07-30T08:00:00+00:00",
    )

    result = scope.service.get_logistics(
        context=scope.context,
        order_no="  ORD-DELAY-001  ",
    )

    assert result.availability is LogisticsAvailability.AVAILABLE
    assert result.shipment == shipment
```

- [ ] **Step 3: 写“订单不存在”的失败测试**

追加：

```python
def test_get_logistics_raises_order_error_when_order_does_not_exist(
    tmp_path,
):
    scope = build_support_scope(tmp_path)

    with pytest.raises(SupportOrderNotFoundError):
        scope.service.get_logistics(
            context=scope.context,
            order_no="ORD-MISSING",
        )
```

- [ ] **Step 4: 运行新测试并确认失败**

Run:

```powershell
python -m pytest tests/application/test_customer_support_service.py -q
```

Expected: FAIL，因为 `LogisticsAvailability`、`LogisticsDetails` 和 `get_logistics()` 尚未实现。

- [ ] **Step 5: 增加物流查询结果类型**

修改业务服务 imports：

```python
from enum import Enum

from app.shipments.base import (
    Shipment,
    ShipmentNotFoundError,
    ShipmentStore,
)
```

在 `OrderDetails` 后增加：

```python
class LogisticsAvailability(str, Enum):
    NOT_CREATED = "not_created"
    AVAILABLE = "available"


@dataclass(frozen=True)
class LogisticsDetails:
    order: Order
    customer: Customer
    availability: LogisticsAvailability
    shipment: Shipment | None
```

- [ ] **Step 6: 实现物流查询**

在 `CustomerSupportService` 中增加：

```python
    def get_logistics(
        self,
        *,
        context: TenantContext,
        order_no: str,
    ) -> LogisticsDetails:
        order_details = self.get_order(
            context=context,
            order_no=order_no,
        )
        try:
            shipment = self._shipment_store.get_by_order_no(
                organization_id=context.organization_id,
                order_no=order_details.order.order_no,
            )
        except ShipmentNotFoundError:
            return LogisticsDetails(
                order=order_details.order,
                customer=order_details.customer,
                availability=LogisticsAvailability.NOT_CREATED,
                shipment=None,
            )

        return LogisticsDetails(
            order=order_details.order,
            customer=order_details.customer,
            availability=LogisticsAvailability.AVAILABLE,
            shipment=shipment,
        )
```

- [ ] **Step 7: 运行物流专项和全量回归**

Run:

```powershell
python -m pytest tests/application/test_customer_support_service.py -q
python -m pytest -q
```

Expected: 两条命令全部 PASS，全量测试通过数量不少于 `136`。

- [ ] **Step 8: 提交 Task 2**

```powershell
git add app/application/customer_support_service.py tests/application/test_customer_support_service.py
git commit -m "feat: add deterministic logistics inquiry"
```

---

### Task 3: 实现带业务默认值和客户关系校验的工单创建

**独立验收产物：** 客服可以用订单号创建工单，也可以只用客户号创建无订单工单；Service 自动生成工单号、设置创建人、负责人和初始状态；客户与订单不匹配时拒绝创建。

**Files:**
- Modify: `app/application/customer_support_service.py`
- Modify: `tests/application/test_customer_support_service.py`

**Interfaces:**
- Consumes: `CustomerStore.get_by_no -> Customer`
- Consumes: `CustomerSupportService.get_order -> OrderDetails`
- Consumes: `TicketStore.create_ticket -> Ticket`
- Produces: `SupportCustomerNotFoundError`
- Produces: `OrderCustomerMismatchError`
- Produces: `TicketNumberGenerationError`
- Produces: `SupportOperationRejectedError`
- Produces: `CustomerSupportService.create_ticket -> Ticket`

- [ ] **Step 1: 写“根据订单创建工单”的失败测试**

在测试文件 imports 中加入：

```python
from app.tickets.base import (
    TicketCategory,
    TicketPriority,
    TicketStatus,
)
```

追加：

```python
def test_create_ticket_from_order_derives_customer_and_actor(tmp_path):
    scope = build_support_scope(tmp_path)

    ticket = scope.service.create_ticket(
        context=scope.context,
        order_no="  ORD-DELAY-001  ",
        customer_no=None,
        summary="  订单超过承诺时间仍未发货  ",
        category=TicketCategory.LOGISTICS,
        priority=TicketPriority.HIGH,
    )

    assert ticket.ticket_no == "TKT-NEW-001"
    assert ticket.organization_id == scope.context.organization_id
    assert ticket.customer_id == scope.customer.customer_id
    assert ticket.order_id == scope.order.order_id
    assert ticket.created_by_user_id == scope.context.user_id
    assert ticket.assigned_to_user_id == scope.context.user_id
    assert ticket.summary == "订单超过承诺时间仍未发货"
    assert ticket.status is TicketStatus.OPEN
```

- [ ] **Step 2: 写“只根据客户创建无订单工单”的失败测试**

追加：

```python
def test_create_ticket_for_customer_without_order(tmp_path):
    scope = build_support_scope(tmp_path)

    ticket = scope.service.create_ticket(
        context=scope.context,
        customer_no="CUST-001",
        order_no=None,
        summary="客户咨询商品使用方法",
        category=TicketCategory.PRODUCT_QUESTION,
        priority=TicketPriority.LOW,
    )

    assert ticket.customer_id == scope.customer.customer_id
    assert ticket.order_id is None
    assert ticket.category is TicketCategory.PRODUCT_QUESTION
```

- [ ] **Step 3: 写请求校验和客户订单不匹配测试**

把以下 imports 加入测试文件：

```python
from app.application.customer_support_service import (
    OrderCustomerMismatchError,
    SupportCustomerNotFoundError,
)
```

追加：

```python
@pytest.mark.parametrize(
    ("summary", "customer_no", "order_no"),
    [
        ("   ", "CUST-001", None),
        ("abcd", "CUST-001", None),
        ("x" * 501, "CUST-001", None),
        ("有效工单摘要", None, None),
    ],
)
def test_create_ticket_rejects_invalid_request(
    tmp_path,
    summary,
    customer_no,
    order_no,
):
    scope = build_support_scope(tmp_path)

    with pytest.raises(InvalidSupportRequestError):
        scope.service.create_ticket(
            context=scope.context,
            customer_no=customer_no,
            order_no=order_no,
            summary=summary,
            category=TicketCategory.OTHER,
            priority=TicketPriority.MEDIUM,
        )


def test_create_ticket_rejects_missing_customer(tmp_path):
    scope = build_support_scope(tmp_path)

    with pytest.raises(
        SupportCustomerNotFoundError,
        match="customer not found",
    ):
        scope.service.create_ticket(
            context=scope.context,
            customer_no="CUST-MISSING",
            order_no=None,
            summary="客户咨询商品使用方法",
            category=TicketCategory.PRODUCT_QUESTION,
            priority=TicketPriority.LOW,
        )


def test_create_ticket_rejects_customer_that_does_not_own_order(tmp_path):
    scope = build_support_scope(tmp_path)
    another_customer = scope.customer_store.create_customer(
        organization_id=scope.context.organization_id,
        customer_no="CUST-002",
        name="陈晨",
    )

    with pytest.raises(
        OrderCustomerMismatchError,
        match="order does not belong to customer",
    ):
        scope.service.create_ticket(
            context=scope.context,
            customer_no=another_customer.customer_no,
            order_no=scope.order.order_no,
            summary="订单与客户不匹配",
            category=TicketCategory.OTHER,
            priority=TicketPriority.MEDIUM,
        )
```

- [ ] **Step 4: 写工单号冲突重试测试**

追加：

```python
def test_create_ticket_retries_generated_number_conflict(tmp_path):
    scope = build_support_scope(
        tmp_path,
        ticket_nos=("TKT-DUPLICATE", "TKT-RETRY-001"),
    )
    scope.ticket_store.create_ticket(
        organization_id=scope.context.organization_id,
        ticket_no="TKT-DUPLICATE",
        customer_id=scope.customer.customer_id,
        order_id=scope.order.order_id,
        created_by_user_id=scope.context.user_id,
        assigned_to_user_id=scope.context.user_id,
        summary="已经存在的工单",
        category=TicketCategory.LOGISTICS,
        priority=TicketPriority.MEDIUM,
        status=TicketStatus.OPEN,
    )

    ticket = scope.service.create_ticket(
        context=scope.context,
        order_no=scope.order.order_no,
        customer_no=None,
        summary="新创建的物流工单",
        category=TicketCategory.LOGISTICS,
        priority=TicketPriority.HIGH,
    )

    assert ticket.ticket_no == "TKT-RETRY-001"
```

- [ ] **Step 5: 运行新增测试并确认失败**

Run:

```powershell
python -m pytest tests/application/test_customer_support_service.py -q
```

Expected: FAIL，因为 `create_ticket()` 及其应用层异常尚未实现。

- [ ] **Step 6: 增加工单创建常量、异常和 imports**

修改 `app/application/customer_support_service.py` imports：

```python
from app.tickets.base import (
    InvalidTicketReferenceError,
    Ticket,
    TicketAlreadyExistsError,
    TicketCategory,
    TicketPriority,
    TicketStatus,
    TicketStore,
)
```

在模块常量区域增加：

```python
MIN_TICKET_SUMMARY_LENGTH = 5
MAX_TICKET_SUMMARY_LENGTH = 500
MAX_TICKET_NUMBER_ATTEMPTS = 3
```

在现有异常后增加：

```python
class SupportCustomerNotFoundError(LookupError):
    pass


class OrderCustomerMismatchError(ValueError):
    pass


class TicketNumberGenerationError(RuntimeError):
    pass


class SupportOperationRejectedError(ValueError):
    pass
```

- [ ] **Step 7: 增加工单创建辅助方法**

在 `CustomerSupportService` 中增加：

```python
    @staticmethod
    def _validate_summary(summary: str) -> str:
        normalized = summary.strip()
        if not (
            MIN_TICKET_SUMMARY_LENGTH
            <= len(normalized)
            <= MAX_TICKET_SUMMARY_LENGTH
        ):
            raise InvalidSupportRequestError(
                "summary must contain 5-500 characters"
            )
        return normalized

    def _get_customer_by_no(
        self,
        *,
        context: TenantContext,
        customer_no: str,
    ) -> Customer:
        normalized_customer_no = self._required_text(
            value=customer_no,
            field_name="customer_no",
        )
        try:
            return self._customer_store.get_by_no(
                organization_id=context.organization_id,
                customer_no=normalized_customer_no,
            )
        except CustomerNotFoundError as exc:
            raise SupportCustomerNotFoundError(
                "customer not found"
            ) from exc

    def _new_ticket_no(self) -> str:
        try:
            ticket_no = self._ticket_no_factory()
        except Exception as exc:
            raise TicketNumberGenerationError(
                "ticket number generation failed"
            ) from exc
        if not isinstance(ticket_no, str) or not ticket_no.strip():
            raise TicketNumberGenerationError(
                "ticket number generation returned an invalid value"
            )
        return ticket_no.strip()
```

- [ ] **Step 8: 实现创建工单**

在 `CustomerSupportService` 中增加：

```python
    def create_ticket(
        self,
        *,
        context: TenantContext,
        summary: str,
        category: TicketCategory,
        priority: TicketPriority,
        customer_no: str | None = None,
        order_no: str | None = None,
    ) -> Ticket:
        normalized_summary = self._validate_summary(summary)
        if not isinstance(category, TicketCategory):
            raise InvalidSupportRequestError(
                "category must be a TicketCategory"
            )
        if not isinstance(priority, TicketPriority):
            raise InvalidSupportRequestError(
                "priority must be a TicketPriority"
            )
        if customer_no is None and order_no is None:
            raise InvalidSupportRequestError(
                "customer_no or order_no is required"
            )

        order_details: OrderDetails | None = None
        if order_no is not None:
            order_details = self.get_order(
                context=context,
                order_no=order_no,
            )

        explicit_customer: Customer | None = None
        if customer_no is not None:
            explicit_customer = self._get_customer_by_no(
                context=context,
                customer_no=customer_no,
            )

        if (
            order_details is not None
            and explicit_customer is not None
            and order_details.customer.customer_id
            != explicit_customer.customer_id
        ):
            raise OrderCustomerMismatchError(
                "order does not belong to customer"
            )

        customer = (
            explicit_customer
            if explicit_customer is not None
            else order_details.customer
        )
        order_id = (
            order_details.order.order_id
            if order_details is not None
            else None
        )

        for _ in range(MAX_TICKET_NUMBER_ATTEMPTS):
            try:
                return self._ticket_store.create_ticket(
                    organization_id=context.organization_id,
                    ticket_no=self._new_ticket_no(),
                    customer_id=customer.customer_id,
                    order_id=order_id,
                    created_by_user_id=context.user_id,
                    assigned_to_user_id=context.user_id,
                    summary=normalized_summary,
                    category=category,
                    priority=priority,
                    status=TicketStatus.OPEN,
                )
            except TicketAlreadyExistsError:
                continue
            except InvalidTicketReferenceError as exc:
                raise SupportOperationRejectedError(
                    "ticket creation was rejected"
                ) from exc

        raise TicketNumberGenerationError(
            "could not allocate a unique ticket number"
        )
```

说明：`customer` 在运行到赋值语句时一定存在，因为前面已经保证至少提供
`customer_no` 或 `order_no`。不要使用 `assert` 代替这个业务前置校验。

- [ ] **Step 9: 补充类型错误和三次冲突失败测试**

追加：

```python
@pytest.mark.parametrize(
    ("category", "priority"),
    [
        ("logistics", TicketPriority.HIGH),
        (TicketCategory.LOGISTICS, "high"),
    ],
)
def test_create_ticket_requires_typed_category_and_priority(
    tmp_path,
    category,
    priority,
):
    scope = build_support_scope(tmp_path)

    with pytest.raises(InvalidSupportRequestError):
        scope.service.create_ticket(
            context=scope.context,
            order_no=scope.order.order_no,
            customer_no=None,
            summary="订单超过承诺时间仍未发货",
            category=category,
            priority=priority,
        )
```

把 `TicketNumberGenerationError` 加入测试 imports，然后追加：

```python
def test_create_ticket_stops_after_three_number_conflicts(tmp_path):
    scope = build_support_scope(
        tmp_path,
        ticket_nos=(
            "TKT-DUPLICATE",
            "TKT-DUPLICATE",
            "TKT-DUPLICATE",
        ),
    )
    scope.ticket_store.create_ticket(
        organization_id=scope.context.organization_id,
        ticket_no="TKT-DUPLICATE",
        customer_id=scope.customer.customer_id,
        order_id=scope.order.order_id,
        created_by_user_id=scope.context.user_id,
        assigned_to_user_id=scope.context.user_id,
        summary="已经存在的工单",
        category=TicketCategory.LOGISTICS,
        priority=TicketPriority.MEDIUM,
        status=TicketStatus.OPEN,
    )

    with pytest.raises(
        TicketNumberGenerationError,
        match="could not allocate",
    ):
        scope.service.create_ticket(
            context=scope.context,
            order_no=scope.order.order_no,
            customer_no=None,
            summary="新创建的物流工单",
            category=TicketCategory.LOGISTICS,
            priority=TicketPriority.HIGH,
        )


def test_create_ticket_rejects_invalid_generated_number(tmp_path):
    scope = build_support_scope(
        tmp_path,
        ticket_nos=("",),
    )

    with pytest.raises(
        TicketNumberGenerationError,
        match="invalid value",
    ):
        scope.service.create_ticket(
            context=scope.context,
            order_no=scope.order.order_no,
            customer_no=None,
            summary="新创建的物流工单",
            category=TicketCategory.LOGISTICS,
            priority=TicketPriority.HIGH,
        )
```

- [ ] **Step 10: 运行工单创建专项和全量回归**

Run:

```powershell
python -m pytest tests/application/test_customer_support_service.py -q
python -m pytest -q
```

Expected: 两条命令全部 PASS，全量测试通过数量不少于 `149`。

- [ ] **Step 11: 提交 Task 3**

```powershell
git add app/application/customer_support_service.py tests/application/test_customer_support_service.py
git commit -m "feat: add deterministic ticket creation service"
```

---

### Task 4: 实现当前客服身份下的内部工单备注

**独立验收产物：** 客服可以使用工单号添加内部备注；Service 自动使用当前用户作为作者，固定可见性为 `INTERNAL`；不存在或属于其他企业的工单统一表现为未找到。

**Files:**
- Modify: `app/application/customer_support_service.py`
- Modify: `tests/application/test_customer_support_service.py`

**Interfaces:**
- Consumes: `TicketStore.get_by_no -> Ticket`
- Consumes: `TicketStore.add_comment -> TicketComment`
- Produces: `SupportTicketNotFoundError`
- Produces: `CustomerSupportService.add_ticket_note -> TicketComment`

- [ ] **Step 1: 写成功添加内部备注的失败测试**

把以下 imports 加入测试文件：

```python
from app.tickets.base import TicketCommentVisibility
```

追加：

```python
def test_add_ticket_note_uses_current_user_and_internal_visibility(
    tmp_path,
):
    scope = build_support_scope(tmp_path)
    ticket = scope.service.create_ticket(
        context=scope.context,
        order_no=scope.order.order_no,
        customer_no=None,
        summary="订单超过承诺时间仍未发货",
        category=TicketCategory.LOGISTICS,
        priority=TicketPriority.HIGH,
    )

    note = scope.service.add_ticket_note(
        context=scope.context,
        ticket_no=f"  {ticket.ticket_no}  ",
        content="  已联系仓库核查发货状态。  ",
    )

    assert note.ticket_id == ticket.ticket_id
    assert note.author_user_id == scope.context.user_id
    assert note.visibility is TicketCommentVisibility.INTERNAL
    assert note.content == "已联系仓库核查发货状态。"
    assert note.seq == 1
```

- [ ] **Step 2: 写内容校验失败测试**

追加：

```python
@pytest.mark.parametrize(
    "content",
    ["   ", "x" * 2001],
)
def test_add_ticket_note_rejects_invalid_content(tmp_path, content):
    scope = build_support_scope(tmp_path)
    ticket = scope.service.create_ticket(
        context=scope.context,
        order_no=scope.order.order_no,
        customer_no=None,
        summary="订单超过承诺时间仍未发货",
        category=TicketCategory.LOGISTICS,
        priority=TicketPriority.HIGH,
    )

    with pytest.raises(InvalidSupportRequestError):
        scope.service.add_ticket_note(
            context=scope.context,
            ticket_no=ticket.ticket_no,
            content=content,
        )
```

- [ ] **Step 3: 写工单未找到和跨租户隐藏测试**

把 `SupportTicketNotFoundError` 加入测试 imports，然后追加：

```python
def test_add_ticket_note_maps_missing_ticket_to_application_error(
    tmp_path,
):
    scope = build_support_scope(tmp_path)

    with pytest.raises(
        SupportTicketNotFoundError,
        match="ticket not found",
    ):
        scope.service.add_ticket_note(
            context=scope.context,
            ticket_no="TKT-MISSING",
            content="核查记录",
        )


def test_add_ticket_note_hides_ticket_from_other_tenant(tmp_path):
    scope = build_support_scope(tmp_path)
    ticket = scope.service.create_ticket(
        context=scope.context,
        order_no=scope.order.order_no,
        customer_no=None,
        summary="订单超过承诺时间仍未发货",
        category=TicketCategory.LOGISTICS,
        priority=TicketPriority.HIGH,
    )
    other_context = TenantContext(
        user_id=scope.context.user_id,
        organization_id="another-organization",
        role=MembershipRole.AGENT,
    )

    with pytest.raises(SupportTicketNotFoundError):
        scope.service.add_ticket_note(
            context=other_context,
            ticket_no=ticket.ticket_no,
            content="不应写入成功",
        )

    assert scope.ticket_store.list_comments(
        organization_id=scope.context.organization_id,
        ticket_id=ticket.ticket_id,
    ) == []
```

- [ ] **Step 4: 运行测试并确认失败**

Run:

```powershell
python -m pytest tests/application/test_customer_support_service.py -q
```

Expected: FAIL，因为 `add_ticket_note()` 和 `SupportTicketNotFoundError` 尚未实现。

- [ ] **Step 5: 增加备注常量、异常和 imports**

修改业务服务的 ticket imports：

```python
from app.tickets.base import (
    InvalidTicketCommentReferenceError,
    InvalidTicketReferenceError,
    Ticket,
    TicketAlreadyExistsError,
    TicketCategory,
    TicketComment,
    TicketCommentVisibility,
    TicketNotFoundError,
    TicketPriority,
    TicketStatus,
    TicketStore,
)
```

增加模块常量：

```python
MAX_TICKET_NOTE_LENGTH = 2000
```

增加异常：

```python
class SupportTicketNotFoundError(LookupError):
    pass
```

- [ ] **Step 6: 实现添加内部备注**

在 `CustomerSupportService` 中增加：

```python
    def add_ticket_note(
        self,
        *,
        context: TenantContext,
        ticket_no: str,
        content: str,
    ) -> TicketComment:
        normalized_ticket_no = self._required_text(
            value=ticket_no,
            field_name="ticket_no",
        )
        normalized_content = self._required_text(
            value=content,
            field_name="content",
        )
        if len(normalized_content) > MAX_TICKET_NOTE_LENGTH:
            raise InvalidSupportRequestError(
                "content must contain at most 2000 characters"
            )

        try:
            ticket = self._ticket_store.get_by_no(
                organization_id=context.organization_id,
                ticket_no=normalized_ticket_no,
            )
        except TicketNotFoundError as exc:
            raise SupportTicketNotFoundError(
                "ticket not found"
            ) from exc

        try:
            return self._ticket_store.add_comment(
                organization_id=context.organization_id,
                ticket_id=ticket.ticket_id,
                author_user_id=context.user_id,
                visibility=TicketCommentVisibility.INTERNAL,
                content=normalized_content,
            )
        except InvalidTicketCommentReferenceError as exc:
            raise SupportOperationRejectedError(
                "ticket note was rejected"
            ) from exc
```

- [ ] **Step 7: 运行备注专项和全量回归**

Run:

```powershell
python -m pytest tests/application/test_customer_support_service.py -q
python -m pytest -q
```

Expected: 两条命令全部 PASS，全量测试通过数量不少于 `154`。

- [ ] **Step 8: 提交 Task 4**

```powershell
git add app/application/customer_support_service.py tests/application/test_customer_support_service.py
git commit -m "feat: add internal ticket note service"
```

---

### Task 5: 提供无 LLM 装配工厂、黄金路径集成测试和使用文档

**独立验收产物：** 调用者只需要数据库路径就能构造 `CustomerSupportService`；集成测试使用模拟业务数据走通“查询延迟订单 → 确认无物流 → 创建工单 → 添加内部备注”黄金路径；文档明确本阶段和 Tool Gateway 的边界。

**Files:**
- Create: `app/application/customer_support_factory.py`
- Create: `tests/application/test_customer_support_factory.py`
- Create: `docs/customer-support-services.md`

**Interfaces:**
- Consumes: `SQLiteCustomerStore`
- Consumes: `SQLiteOrderStore`
- Consumes: `SQLiteShipmentStore`
- Consumes: `SQLiteTicketStore`
- Produces: `create_customer_support_service -> CustomerSupportService`

- [ ] **Step 1: 写工厂构造失败测试**

创建 `tests/application/test_customer_support_factory.py`：

```python
from datetime import datetime, timezone

from app.application.customer_support_factory import (
    create_customer_support_service,
)
from app.application.customer_support_service import (
    LogisticsAvailability,
)
from app.application.organization_service import TenantContext
from app.customers.sqlite_store import SQLiteCustomerStore
from app.demo_data.seeder import DemoDataSeeder
from app.orders.sqlite_store import SQLiteOrderStore
from app.organizations.base import MembershipRole
from app.organizations.sqlite_store import SQLiteOrganizationStore
from app.shipments.sqlite_store import SQLiteShipmentStore
from app.tickets.base import (
    TicketCategory,
    TicketCommentVisibility,
    TicketPriority,
)
from app.tickets.sqlite_store import SQLiteTicketStore
from app.users.sqlite_store import SQLiteUserStore


def test_factory_service_completes_delayed_order_golden_path(tmp_path):
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
            29,
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
    service = create_customer_support_service( 
        database_path,
        ticket_no_factory=lambda: "TKT-GOLDEN-001",
    )

    order_result = service.get_order(
        context=context,
        order_no="ORD-DELAY-001",
    )
    logistics_result = service.get_logistics(
        context=context,
        order_no="ORD-DELAY-001",
    )
    ticket = service.create_ticket(
        context=context,
        order_no=order_result.order.order_no,
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

    assert logistics_result.availability is (
        LogisticsAvailability.NOT_CREATED
    )
    assert logistics_result.shipment is None
    assert ticket.ticket_no == "TKT-GOLDEN-001"
    assert ticket.order_id == order_result.order.order_id
    assert note.ticket_id == ticket.ticket_id
    assert note.visibility is TicketCommentVisibility.INTERNAL
```

- [ ] **Step 2: 运行测试并确认失败**

Run:

```powershell
python -m pytest tests/application/test_customer_support_factory.py -q
```

Expected: collection FAIL，因为 `customer_support_factory.py` 尚不存在。

- [ ] **Step 3: 实现 SQLite 装配工厂**

创建 `app/application/customer_support_factory.py`：

```python
from pathlib import Path

from app.application.customer_support_service import (
    CustomerSupportService,
    TicketNumberFactory,
)
from app.customers.sqlite_store import SQLiteCustomerStore
from app.orders.sqlite_store import SQLiteOrderStore
from app.shipments.sqlite_store import SQLiteShipmentStore
from app.tickets.sqlite_store import SQLiteTicketStore


def create_customer_support_service(
    database_path: str | Path,
    *,
    ticket_no_factory: TicketNumberFactory | None = None,
) -> CustomerSupportService:
    return CustomerSupportService(
        customer_store=SQLiteCustomerStore(database_path),
        order_store=SQLiteOrderStore(database_path),
        shipment_store=SQLiteShipmentStore(database_path),
        ticket_store=SQLiteTicketStore(database_path),
        ticket_no_factory=ticket_no_factory,
    )
```

- [ ] **Step 4: 运行工厂集成测试**

Run:

```powershell
python -m pytest tests/application/test_customer_support_factory.py -q
```

Expected: `1 passed`。

- [ ] **Step 5: 编写直接调用文档**

创建 `docs/customer-support-services.md`，内容必须包含：

```markdown
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
```

- [ ] **Step 6: 运行第三步专项验收**

Run:

```powershell
python -m pytest `
  tests/application/test_customer_support_service.py `
  tests/application/test_customer_support_factory.py `
  tests/customers/test_customer_store.py `
  tests/orders/test_order_store.py `
  tests/shipments/test_shipment_store.py `
  tests/tickets/test_ticket_store.py `
  tests/demo_data/test_seeder.py `
  -q
```

Expected: 全部 PASS。

- [ ] **Step 7: 确认 Application Service 没有越过阶段边界**

Run:

```powershell
Select-String `
  -Path .\app\application\customer_support_*.py `
  -Pattern "openai|deepseek|fastapi|app\.agent|app\.tools" `
  -CaseSensitive:$false
```

Expected: 无输出。

再运行：

```powershell
Select-String `
  -Path .\app\application\customer_support_service.py `
  -Pattern "def get_order|def get_logistics|def create_ticket|def add_ticket_note"
```

Expected: 正好找到四个公开业务方法。

- [ ] **Step 8: 运行全量回归**

Run:

```powershell
python -m pytest -q
```

Expected: 全部 PASS，测试通过数量不少于 `155`。

- [ ] **Step 9: 提交 Task 5**

```powershell
git add app/application/customer_support_factory.py tests/application/test_customer_support_factory.py docs/customer-support-services.md
git commit -m "docs: add deterministic support service entrypoint"
```

---

## 4. 最终验收清单

实施者完成所有 Task 后逐项确认：

- [ ] `python -m pytest -q` 全部通过。
- [ ] 没有新增 migration，Alembic head 仍为 `0008_ticket_comments`。
- [ ] `CustomerSupportService` 不导入 LLM、Agent、FastAPI 或工具注册表。
- [ ] 四个公开业务方法都只通过 `TenantContext` 获得租户和操作人。
- [ ] `get_order()` 返回订单和客户。
- [ ] `get_logistics()` 能区分无订单、无物流和有物流。
- [ ] `create_ticket()` 能根据订单推导客户。
- [ ] `create_ticket()` 能创建不关联订单的客户工单。
- [ ] 客户号和订单号不匹配时不会创建工单。
- [ ] 工单创建人、负责人和初始状态由 Service 设置。
- [ ] 工单号发生冲突时最多重试三次。
- [ ] `add_ticket_note()` 固定创建内部备注。
- [ ] 备注作者只能是 `context.user_id`。
- [ ] 错误租户查询不会泄露其他企业的数据。
- [ ] `main.py` 和 `app/tools/registry.py` 没有接入这些业务方法。
- [ ] `docs/customer-support-services.md` 与最终代码签名一致。

## 5. 第三步完成后的边界

完成本计划后，可以判定最初开发顺序中的第 3 步已经完成：

```text
1. 最小多租户基础                   已完成
2. 模拟客服业务数据                 已完成
3. 不依赖 LLM 的确定性业务服务      本计划完成后即完成
4. 受控 Tool Gateway               下一步
5. Agent 选择工具并生成回复         后续
```

下一份计划应专注于第 4 步，建议名称：

```text
2026-xx-xx-customer-support-tool-gateway.md
```

下一步只允许工具传递业务参数，例如 `order_no`、`summary` 和 `content`；可信的
`organization_id` 与 `user_id` 必须由服务器侧 Tool Gateway 从请求上下文注入。
