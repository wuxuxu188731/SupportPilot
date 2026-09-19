from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.customers.base import (
    Customer,
    CustomerNotFoundError,
    CustomerStore,
)
from app.orders.base import (
    Order,
    OrderNotFoundError,
    OrderStatus,
    OrderStore,
)
from app.organizations.base import (
    MembershipNotFoundError,
    MembershipRole,
    OrganizationStore,
)
from app.shipments.base import (
    Shipment,
    ShipmentNotFoundError,
    ShipmentStatus,
    ShipmentStore,
)
from app.tickets.base import (
    Ticket,
    TicketCategory,
    TicketCommentVisibility,
    TicketNotFoundError,
    TicketPriority,
    TicketStatus,
    TicketStore,
)


# 各表本次真正新增的行数：初始化与「一键生成测试数据」都靠它区分
# 「新增」与「已存在被跳过」。键必须使用 _COUNTED_TABLES 中的固定表名。
COUNTED_TABLES: tuple[str, ...] = (
    "customers",
    "orders",
    "shipments",
    "tickets",
    "ticket_comments",
)

# 三条演示订单与两笔演示物流的编号：时间刷新只认这些固定编号，
# 体验者自己创建的订单/物流绝不会被本初始化逻辑改写。
DEMO_ORDER_NOS: tuple[str, ...] = (
    "ORD-DELAY-001",
    "ORD-TRANSIT-001",
    "ORD-DELIVERED-001",
)
DEMO_SHIPMENT_NOS: tuple[str, ...] = (
    "SHP-TRANSIT-001",
    "SHP-DELIVERED-001",
)


@dataclass(frozen=True)
class DemoSeedResult:
    organization_id: str
    customer_nos: tuple[str, ...]
    order_nos: tuple[str, ...]
    shipment_nos: tuple[str, ...]
    ticket_nos: tuple[str, ...]
    # 本次运行使用的时间基准 T（UTC ISO 文本），供体验者解释工具返回的
    # 相对时间；同时让刷新前后的结果可比对。
    reference_at: str
    # 本次运行实际新增的行数（按表统计）；首次初始化为满额，重复运行时
    # 除首次对话产生的工单外都应为 0。
    created_counts: dict[str, int]


class DemoSeedAdminRequiredError(PermissionError):
    pass


class DemoDataConflictError(ValueError):
    pass


class DemoDataSeeder:
    def __init__(
        self,
        *,
        organization_store: OrganizationStore,
        customer_store: CustomerStore,
        order_store: OrderStore,
        shipment_store: ShipmentStore,
        ticket_store: TicketStore,
    ):
        self._organization_store = organization_store
        self._customer_store = customer_store
        self._order_store = order_store
        self._shipment_store = shipment_store
        self._ticket_store = ticket_store

    def _require_admin(
        self,
        *,
        organization_id: str,
        actor_user_id: str,
    ) -> None:
        try:
            membership = self._organization_store.get_membership(
                organization_id=organization_id,
                user_id=actor_user_id,
            )
        except MembershipNotFoundError as exc:
            raise DemoSeedAdminRequiredError(
                "organization admin required"
            ) from exc
        if membership.role is not MembershipRole.ADMIN:
            raise DemoSeedAdminRequiredError(
                "organization admin required"
            )

    def _ensure_customer(
        self,
        *,
        organization_id: str,
        customer_no: str,
        name: str,
        email: str,
        phone: str,
        counts: dict[str, int],
    ) -> Customer:
        try:
            customer = self._customer_store.get_by_no(
                organization_id=organization_id,
                customer_no=customer_no,
            )
        except CustomerNotFoundError:
            customer = self._customer_store.create_customer(
                organization_id=organization_id,
                customer_no=customer_no,
                name=name,
                email=email,
                phone=phone,
            )
            counts["customers"] += 1
            return customer
        if (
            customer.name != name
            or customer.email != email
            or customer.phone != phone
        ):
            raise DemoDataConflictError(
                f"{customer_no} already exists with different data"
            )
        return customer

    def _ensure_order(
        self,
        *,
        organization_id: str,
        order_no: str,
        customer: Customer,
        status: OrderStatus,
        item_summary: str,
        total_amount_cents: int,
        placed_at: str,
        promised_ship_at: str | None,
        counts: dict[str, int],
        timing: dict[str, dict[str, str | None]],
    ) -> Order:
        # 「当前时间基准下应有的时间列」记录在 timing 里：即使订单已存在
        # 且业务字段一致（幂等跳过），也仍可按 refresh_times 重新对齐时间。
        timing[order_no] = {
            "placed_at": placed_at,
            "promised_ship_at": promised_ship_at,
        }
        try:
            order = self._order_store.get_by_no(
                organization_id=organization_id,
                order_no=order_no,
            )
        except OrderNotFoundError:
            order = self._order_store.create_order(
                organization_id=organization_id,
                order_no=order_no,
                customer_id=customer.customer_id,
                status=status,
                item_summary=item_summary,
                total_amount_cents=total_amount_cents,
                currency="CNY",
                placed_at=placed_at,
                promised_ship_at=promised_ship_at,
            )
            counts["orders"] += 1
            return order
        if (
            order.customer_id != customer.customer_id
            or order.status is not status
            or order.item_summary != item_summary
            or order.total_amount_cents != total_amount_cents
            or order.currency != "CNY"
        ):
            raise DemoDataConflictError(
                f"{order_no} already exists with different data"
            )
        return order

    def _ensure_shipment(
        self,
        *,
        organization_id: str,
        shipment_no: str,
        order: Order,
        carrier: str,
        tracking_no: str,
        status: ShipmentStatus,
        last_event: str,
        shipped_at: str,
        estimated_delivery_at: str,
        delivered_at: str | None,
        counts: dict[str, int],
        timing: dict[str, dict[str, str | None]],
    ) -> Shipment:
        timing[shipment_no] = {
            "shipped_at": shipped_at,
            "estimated_delivery_at": estimated_delivery_at,
            "delivered_at": delivered_at,
        }
        try:
            shipment = self._shipment_store.get_by_order_no(
                organization_id=organization_id,
                order_no=order.order_no,
            )
        except ShipmentNotFoundError:
            shipment = self._shipment_store.create_shipment(
                organization_id=organization_id,
                shipment_no=shipment_no,
                order_id=order.order_id,
                carrier=carrier,
                tracking_no=tracking_no,
                status=status,
                last_event=last_event,
                shipped_at=shipped_at,
                estimated_delivery_at=estimated_delivery_at,
                delivered_at=delivered_at,
            )
            counts["shipments"] += 1
            return shipment
        if (
            shipment.shipment_no != shipment_no
            or shipment.status is not status
            or shipment.order_id != order.order_id
            or shipment.carrier != carrier
            or shipment.tracking_no != tracking_no
        ):
            raise DemoDataConflictError(
                f"{shipment_no} conflicts with existing shipment"
            )
        return shipment

    def _ensure_ticket(
        self,
        *,
        organization_id: str,
        ticket_no: str,
        customer: Customer,
        order: Order,
        actor_user_id: str,
        summary: str,
        category: TicketCategory,
        priority: TicketPriority,
        status: TicketStatus,
        counts: dict[str, int],
    ) -> Ticket:
        try:
            ticket = self._ticket_store.get_by_no(
                organization_id=organization_id,
                ticket_no=ticket_no,
            )
        except TicketNotFoundError:
            ticket = self._ticket_store.create_ticket(
                organization_id=organization_id,
                ticket_no=ticket_no,
                customer_id=customer.customer_id,
                order_id=order.order_id,
                created_by_user_id=actor_user_id,
                assigned_to_user_id=actor_user_id,
                summary=summary,
                category=category,
                priority=priority,
                status=status,
            )
            counts["tickets"] += 1
            return ticket
        if (
            ticket.customer_id != customer.customer_id
            or ticket.order_id != order.order_id
            or ticket.category is not category
            or ticket.priority is not priority
            or ticket.status is not status
            or ticket.summary != summary
        ):
            raise DemoDataConflictError(
                f"{ticket_no} already exists with different data"
            )
        return ticket

    def _ensure_comment(
        self,
        *,
        organization_id: str,
        ticket: Ticket,
        actor_user_id: str,
        visibility: TicketCommentVisibility,
        content: str,
        counts: dict[str, int],
    ) -> None:
        existing = self._ticket_store.list_comments(
            organization_id=organization_id,
            ticket_id=ticket.ticket_id,
        )
        if any(
            item.visibility is visibility and item.content == content
            for item in existing
        ):
            return
        self._ticket_store.add_comment(
            organization_id=organization_id,
            ticket_id=ticket.ticket_id,
            author_user_id=actor_user_id,
            visibility=visibility,
            content=content,
        )
        counts["ticket_comments"] += 1

    def _apply_times(
        self,
        *,
        organization_id: str,
        timing: dict[str, dict[str, str | None]],
    ) -> None:
        """把时间列重新对齐到本次运行的时间基准 T。

        只更新时间列，不碰状态、金额、客户等业务字段——幂等语义（重复初始化
        不覆盖体验者修改）保持不变，被刷新的只有「会随时间过期」的日期。
        只作用于固定演示编号，体验者自建数据不在范围内。
        """
        for order_no in DEMO_ORDER_NOS:
            order_times = timing.get(order_no)
            if order_times is None:
                continue
            self._order_store.update_demo_times(
                organization_id=organization_id,
                order_no=order_no,
                placed_at=order_times["placed_at"],
                promised_ship_at=order_times["promised_ship_at"],
            )
        for shipment_no in DEMO_SHIPMENT_NOS:
            shipment_times = timing.get(shipment_no)
            if shipment_times is None:
                continue
            self._shipment_store.update_demo_times(
                organization_id=organization_id,
                shipment_no=shipment_no,
                shipped_at=shipment_times["shipped_at"],
                estimated_delivery_at=shipment_times["estimated_delivery_at"],
                delivered_at=shipment_times["delivered_at"],
            )

    def seed(
        self,
        *,
        organization_id: str,
        actor_user_id: str,
        reference_time: datetime | None = None,
        refresh_times: bool = False,
    ) -> DemoSeedResult:
        """为指定企业幂等地初始化演示业务数据。

        ``refresh_times=True`` 时，会把演示订单与物流的**时间列**重新对齐到
        本次的 ``reference_time``（缺省即当前时刻），解决「演示数据放几天后
        承诺发货时间/预计送达时间过期」的问题；业务字段（状态、金额、客户、
        摘要）仍然保持幂等，不会被覆盖。默认 False 保持原有幂等语义。
        """
        self._require_admin(
            organization_id=organization_id,
            actor_user_id=actor_user_id,
        )
        anchor = reference_time or datetime.now(timezone.utc)
        if anchor.tzinfo is None or anchor.utcoffset() is None:
            raise ValueError("reference_time must be timezone-aware")
        anchor = anchor.astimezone(timezone.utc)

        counts = {table: 0 for table in COUNTED_TABLES}
        # 本次时间基准下各演示记录应有的时间列，供 refresh_times 使用。
        timing: dict[str, dict[str, str | None]] = {}

        customer_one = self._ensure_customer(
            organization_id=organization_id,
            customer_no="CUST-001",
            name="林晓",
            email="linxiao@example.test",
            phone="+86-000-0000-0001",
            counts=counts,
        )
        customer_two = self._ensure_customer(
            organization_id=organization_id,
            customer_no="CUST-002",
            name="陈晨",
            email="chenchen@example.test",
            phone="+86-000-0000-0002",
            counts=counts,
        )

        delayed_order = self._ensure_order(
            organization_id=organization_id,
            order_no="ORD-DELAY-001",
            customer=customer_one,
            status=OrderStatus.PROCESSING,
            item_summary="无线耳机 x1",
            total_amount_cents=39900,
            placed_at=(anchor - timedelta(days=5)).isoformat(),
            promised_ship_at=(anchor - timedelta(days=3)).isoformat(),
            counts=counts,
            timing=timing,
        )
        transit_order = self._ensure_order(
            organization_id=organization_id,
            order_no="ORD-TRANSIT-001",
            customer=customer_one,
            status=OrderStatus.SHIPPED,
            item_summary="机械键盘 x1",
            total_amount_cents=69900,
            placed_at=(anchor - timedelta(days=3)).isoformat(),
            promised_ship_at=(anchor - timedelta(days=2)).isoformat(),
            counts=counts,
            timing=timing,
        )
        delivered_order = self._ensure_order(
            organization_id=organization_id,
            order_no="ORD-DELIVERED-001",
            customer=customer_two,
            status=OrderStatus.DELIVERED,
            item_summary="保温杯 x2",
            total_amount_cents=15800,
            placed_at=(anchor - timedelta(days=10)).isoformat(),
            promised_ship_at=(anchor - timedelta(days=9)).isoformat(),
            counts=counts,
            timing=timing,
        )

        try:
            self._shipment_store.get_by_order_no(
                organization_id=organization_id,
                order_no=delayed_order.order_no,
            )
        except ShipmentNotFoundError:
            pass
        else:
            raise DemoDataConflictError(
                "ORD-DELAY-001 must not have a shipment"
            )

        transit_shipment = self._ensure_shipment(
            organization_id=organization_id,
            shipment_no="SHP-TRANSIT-001",
            order=transit_order,
            carrier="顺丰速运",
            tracking_no="SF-DEMO-TRANSIT-001",
            status=ShipmentStatus.IN_TRANSIT,
            last_event="快件已到达目的地分拨中心",
            shipped_at=(anchor - timedelta(days=2)).isoformat(),
            estimated_delivery_at=(anchor + timedelta(days=1)).isoformat(),
            delivered_at=None,
            counts=counts,
            timing=timing,
        )
        delivered_shipment = self._ensure_shipment(
            organization_id=organization_id,
            shipment_no="SHP-DELIVERED-001",
            order=delivered_order,
            carrier="京东物流",
            tracking_no="JD-DEMO-DELIVERED-001",
            status=ShipmentStatus.DELIVERED,
            last_event="已签收",
            shipped_at=(anchor - timedelta(days=9)).isoformat(),
            estimated_delivery_at=(anchor - timedelta(days=7)).isoformat(),
            delivered_at=(anchor - timedelta(days=7)).isoformat(),
            counts=counts,
            timing=timing,
        )

        delay_ticket = self._ensure_ticket(
            organization_id=organization_id,
            ticket_no="TKT-DELAY-001",
            customer=customer_one,
            order=delayed_order,
            actor_user_id=actor_user_id,
            summary="订单超过承诺发货时间且暂无物流记录",
            category=TicketCategory.LOGISTICS,
            priority=TicketPriority.HIGH,
            status=TicketStatus.OPEN,
            counts=counts,
        )
        damage_ticket = self._ensure_ticket(
            organization_id=organization_id,
            ticket_no="TKT-DAMAGE-001",
            customer=customer_two,
            order=delivered_order,
            actor_user_id=actor_user_id,
            summary="客户反馈签收后发现商品破损",
            category=TicketCategory.DAMAGED_ITEM,
            priority=TicketPriority.MEDIUM,
            status=TicketStatus.PENDING_CUSTOMER,
            counts=counts,
        )

        self._ensure_comment(
            organization_id=organization_id,
            ticket=delay_ticket,
            actor_user_id=actor_user_id,
            visibility=TicketCommentVisibility.INTERNAL,
            content="已核对订单，超过承诺发货时间且暂无物流记录。",
            counts=counts,
        )
        self._ensure_comment(
            organization_id=organization_id,
            ticket=delay_ticket,
            actor_user_id=actor_user_id,
            visibility=TicketCommentVisibility.PUBLIC,
            content="已为您创建加急工单，我们会尽快核实发货情况。",
            counts=counts,
        )
        self._ensure_comment(
            organization_id=organization_id,
            ticket=damage_ticket,
            actor_user_id=actor_user_id,
            visibility=TicketCommentVisibility.PUBLIC,
            content="请补充商品破损部位和外包装照片。",
            counts=counts,
        )

        # 业务字段全部确认一致后才刷新时间：若上面的幂等校验抛冲突，
        # 演示数据保持原样，不会出现「一半新一半旧」的中间状态。
        if refresh_times:
            self._apply_times(
                organization_id=organization_id,
                timing=timing,
            )

        return DemoSeedResult(
            organization_id=organization_id,
            customer_nos=(
                customer_one.customer_no,
                customer_two.customer_no,
            ),
            order_nos=(
                delayed_order.order_no,
                transit_order.order_no,
                delivered_order.order_no,
            ),
            shipment_nos=(
                transit_shipment.shipment_no,
                delivered_shipment.shipment_no,
            ),
            ticket_nos=(
                delay_ticket.ticket_no,
                damage_ticket.ticket_no,
            ),
            reference_at=anchor.isoformat(),
            created_counts=counts,
        )
