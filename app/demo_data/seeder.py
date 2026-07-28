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


@dataclass(frozen=True)
class DemoSeedResult:
    organization_id: str
    customer_nos: tuple[str, ...]
    order_nos: tuple[str, ...]
    shipment_nos: tuple[str, ...]
    ticket_nos: tuple[str, ...]


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
    ) -> Customer:
        try:
            customer = self._customer_store.get_by_no(
                organization_id=organization_id,
                customer_no=customer_no,
            )
        except CustomerNotFoundError:
            return self._customer_store.create_customer(
                organization_id=organization_id,
                customer_no=customer_no,
                name=name,
                email=email,
                phone=phone,
            )
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
    ) -> Order:
        try:
            order = self._order_store.get_by_no(
                organization_id=organization_id,
                order_no=order_no,
            )
        except OrderNotFoundError:
            return self._order_store.create_order(
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
    ) -> Shipment:
        try:
            shipment = self._shipment_store.get_by_order_no(
                organization_id=organization_id,
                order_no=order.order_no,
            )
        except ShipmentNotFoundError:
            return self._shipment_store.create_shipment(
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
    ) -> Ticket:
        try:
            ticket = self._ticket_store.get_by_no(
                organization_id=organization_id,
                ticket_no=ticket_no,
            )
        except TicketNotFoundError:
            return self._ticket_store.create_ticket(
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

    def seed(
        self,
        *,
        organization_id: str,
        actor_user_id: str,
        reference_time: datetime | None = None,
    ) -> DemoSeedResult:
        self._require_admin(
            organization_id=organization_id,
            actor_user_id=actor_user_id,
        )
        anchor = reference_time or datetime.now(timezone.utc)
        if anchor.tzinfo is None or anchor.utcoffset() is None:
            raise ValueError("reference_time must be timezone-aware")
        anchor = anchor.astimezone(timezone.utc)

        customer_one = self._ensure_customer(
            organization_id=organization_id,
            customer_no="CUST-001",
            name="林晓",
            email="linxiao@example.test",
            phone="+86-000-0000-0001",
        )
        customer_two = self._ensure_customer(
            organization_id=organization_id,
            customer_no="CUST-002",
            name="陈晨",
            email="chenchen@example.test",
            phone="+86-000-0000-0002",
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
        )

        self._ensure_comment(
            organization_id=organization_id,
            ticket=delay_ticket,
            actor_user_id=actor_user_id,
            visibility=TicketCommentVisibility.INTERNAL,
            content="已核对订单，超过承诺发货时间且暂无物流记录。",
        )
        self._ensure_comment(
            organization_id=organization_id,
            ticket=delay_ticket,
            actor_user_id=actor_user_id,
            visibility=TicketCommentVisibility.PUBLIC,
            content="已为您创建加急工单，我们会尽快核实发货情况。",
        )
        self._ensure_comment(
            organization_id=organization_id,
            ticket=damage_ticket,
            actor_user_id=actor_user_id,
            visibility=TicketCommentVisibility.PUBLIC,
            content="请补充商品破损部位和外包装照片。",
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
        )
