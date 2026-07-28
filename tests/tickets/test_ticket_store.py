import pytest

from app.customers.sqlite_store import SQLiteCustomerStore
from app.orders.base import OrderStatus
from app.orders.sqlite_store import SQLiteOrderStore
from app.organizations.base import MembershipRole
from app.organizations.sqlite_store import SQLiteOrganizationStore
from app.tickets.base import (
    InvalidTicketCommentReferenceError,
    InvalidTicketReferenceError,
    TicketAlreadyExistsError,
    TicketCategory,
    TicketCommentVisibility,
    TicketNotFoundError,
    TicketPriority,
    TicketStatus,
)
from app.tickets.sqlite_store import SQLiteTicketStore
from app.organizations.sqlite_store import SQLiteOrganizationStore
from app.users.sqlite_store import SQLiteUserStore


def build_ticket_scope(tmp_path):
    database_path = tmp_path / "app.db"
    users = SQLiteUserStore(database_path)
    organizations = SQLiteOrganizationStore(database_path)
    customers = SQLiteCustomerStore(database_path)
    orders = SQLiteOrderStore(database_path)
    tickets = SQLiteTicketStore(database_path)
    alice = users.create_user(username="alice", password_hash="hash")
    bob = users.create_user(username="bob", password_hash="hash")
    org_a = organizations.create_with_admin(
        name="Company A",
        admin_user_id=alice.user_id,
    )
    org_b = organizations.create_with_admin(
        name="Company B",
        admin_user_id=bob.user_id,
    )
    organizations.add_membership(
        organization_id=org_a.organization_id,
        user_id=bob.user_id,
        role=MembershipRole.AGENT,
    )
    customer_a = customers.create_customer(
        organization_id=org_a.organization_id,
        customer_no="CUST-001",
        name="A Customer",
    )
    customer_a_other = customers.create_customer(
        organization_id=org_a.organization_id,
        customer_no="CUST-002",
        name="Other A Customer",
    )
    customer_b = customers.create_customer(
        organization_id=org_b.organization_id,
        customer_no="CUST-001",
        name="B Customer",
    )
    order_a = orders.create_order(
        organization_id=org_a.organization_id,
        order_no="ORD-001",
        customer_id=customer_a.customer_id,
        status=OrderStatus.PROCESSING,
        item_summary="无线耳机 x1",
        total_amount_cents=39900,
        currency="CNY",
        placed_at="2026-07-23T08:00:00+00:00",
    )
    return (
        tickets,
        org_a,
        org_b,
        alice,
        bob,
        customer_a,
        customer_a_other,
        customer_b,
        order_a,
    )


def create_ticket(
    store,
    *,
    organization_id,
    customer_id,
    order_id,
    creator_id,
    assignee_id,
    ticket_no="TKT-001",
):
    return store.create_ticket(
        organization_id=organization_id,
        ticket_no=ticket_no,
        customer_id=customer_id,
        order_id=order_id,
        created_by_user_id=creator_id,
        assigned_to_user_id=assignee_id,
        summary="订单超过承诺时间仍未发货",
        category=TicketCategory.LOGISTICS,
        priority=TicketPriority.HIGH,
        status=TicketStatus.OPEN,
    )


def test_ticket_round_trip_and_tenant_isolation(tmp_path):
    (
        store,
        org_a,
        org_b,
        alice,
        bob,
        customer_a,
        _,
        _,
        order_a,
    ) = build_ticket_scope(tmp_path)
    created = create_ticket(
        store,
        organization_id=org_a.organization_id,
        customer_id=customer_a.customer_id,
        order_id=order_a.order_id,
        creator_id=alice.user_id,
        assignee_id=bob.user_id,
    )

    assert store.get_by_id(
        organization_id=org_a.organization_id,
        ticket_id=created.ticket_id,
    ) == created
    assert store.get_by_no(
        organization_id=org_a.organization_id,
        ticket_no="TKT-001",
    ) == created
    with pytest.raises(TicketNotFoundError):
        store.get_by_id(
            organization_id=org_b.organization_id,
            ticket_id=created.ticket_id,
        )


def test_ticket_rejects_order_owned_by_other_customer(tmp_path):
    (
        store,
        org_a,
        _,
        alice,
        _,
        _,
        customer_a_other,
        _,
        order_a,
    ) = build_ticket_scope(tmp_path)

    with pytest.raises(InvalidTicketReferenceError):
        create_ticket(
            store,
            organization_id=org_a.organization_id,
            customer_id=customer_a_other.customer_id,
            order_id=order_a.order_id,
            creator_id=alice.user_id,
            assignee_id=None,
        )


def test_ticket_rejects_customer_from_other_tenant(tmp_path):
    (
        store,
        org_a,
        _,
        alice,
        _,
        _,
        _,
        customer_b,
        _,
    ) = build_ticket_scope(tmp_path)

    with pytest.raises(InvalidTicketReferenceError):
        create_ticket(
            store,
            organization_id=org_a.organization_id,
            customer_id=customer_b.customer_id,
            order_id=None,
            creator_id=alice.user_id,
            assignee_id=None,
        )


def test_ticket_number_is_unique_inside_tenant(tmp_path):
    (
        store,
        org_a,
        _,
        alice,
        _,
        customer_a,
        _,
        _,
        order_a,
    ) = build_ticket_scope(tmp_path)
    create_ticket(
        store,
        organization_id=org_a.organization_id,
        customer_id=customer_a.customer_id,
        order_id=order_a.order_id,
        creator_id=alice.user_id,
        assignee_id=None,
        ticket_no="TKT-DUPLICATE",
    )

    with pytest.raises(TicketAlreadyExistsError):
        create_ticket(
            store,
            organization_id=org_a.organization_id,
            customer_id=customer_a.customer_id,
            order_id=order_a.order_id,
            creator_id=alice.user_id,
            assignee_id=None,
            ticket_no="TKT-DUPLICATE",
        )


def test_ticket_rejects_assignee_outside_organization(tmp_path):
    (
        store,
        org_a,
        _,
        alice,
        _,
        customer_a,
        _,
        _,
        order_a,
    ) = build_ticket_scope(tmp_path)
    outsiders = SQLiteUserStore(tmp_path / "app.db")
    charlie = outsiders.create_user(
        username="charlie",
        password_hash="hash",
    )

    with pytest.raises(InvalidTicketReferenceError):
        create_ticket(
            store,
            organization_id=org_a.organization_id,
            customer_id=customer_a.customer_id,
            order_id=order_a.order_id,
            creator_id=alice.user_id,
            assignee_id=charlie.user_id,
        )


def test_comments_are_numbered_and_returned_in_order(tmp_path):
    (
        store,
        org_a,
        _,
        alice,
        bob,
        customer_a,
        _,
        _,
        order_a,
    ) = build_ticket_scope(tmp_path)
    ticket = create_ticket(
        store,
        organization_id=org_a.organization_id,
        customer_id=customer_a.customer_id,
        order_id=order_a.order_id,
        creator_id=alice.user_id,
        assignee_id=bob.user_id,
    )

    first = store.add_comment(
        organization_id=org_a.organization_id,
        ticket_id=ticket.ticket_id,
        author_user_id=alice.user_id,
        visibility=TicketCommentVisibility.INTERNAL,
        content="已核对订单，等待仓库反馈。",
    )
    second = store.add_comment(
        organization_id=org_a.organization_id,
        ticket_id=ticket.ticket_id,
        author_user_id=bob.user_id,
        visibility=TicketCommentVisibility.PUBLIC,
        content="您的问题正在处理中。",
    )

    assert first.seq == 1
    assert second.seq == 2
    assert store.list_comments(
        organization_id=org_a.organization_id,
        ticket_id=ticket.ticket_id,
    ) == [first, second]


def test_comment_lookup_hides_ticket_from_other_tenant(tmp_path):
    (
        store,
        org_a,
        org_b,
        alice,
        _,
        customer_a,
        _,
        _,
        order_a,
    ) = build_ticket_scope(tmp_path)
    ticket = create_ticket(
        store,
        organization_id=org_a.organization_id,
        customer_id=customer_a.customer_id,
        order_id=order_a.order_id,
        creator_id=alice.user_id,
        assignee_id=None,
    )

    with pytest.raises(TicketNotFoundError):
        store.list_comments(
            organization_id=org_b.organization_id,
            ticket_id=ticket.ticket_id,
        )


def test_comment_rejects_blank_content(tmp_path):
    (
        store,
        org_a,
        _,
        alice,
        _,
        customer_a,
        _,
        _,
        order_a,
    ) = build_ticket_scope(tmp_path)
    ticket = create_ticket(
        store,
        organization_id=org_a.organization_id,
        customer_id=customer_a.customer_id,
        order_id=order_a.order_id,
        creator_id=alice.user_id,
        assignee_id=None,
    )

    with pytest.raises(InvalidTicketCommentReferenceError):
        store.add_comment(
            organization_id=org_a.organization_id,
            ticket_id=ticket.ticket_id,
            author_user_id=alice.user_id,
            visibility=TicketCommentVisibility.INTERNAL,
            content="   ",
        )


def test_comment_rejects_author_outside_organization(tmp_path):
    (
        store,
        org_a,
        org_b,
        alice,
        _,
        customer_a,
        _,
        _,
        order_a,
    ) = build_ticket_scope(tmp_path)
    ticket = create_ticket(
        store,
        organization_id=org_a.organization_id,
        customer_id=customer_a.customer_id,
        order_id=order_a.order_id,
        creator_id=alice.user_id,
        assignee_id=None,
    )
    user_store = SQLiteUserStore(tmp_path / "app.db")
    organization_store = SQLiteOrganizationStore(tmp_path / "app.db")
    charlie = user_store.create_user(
        username="charlie",
        password_hash="hash",
    )
    organization_store.add_membership(
        organization_id=org_b.organization_id,
        user_id=charlie.user_id,
        role=MembershipRole.AGENT,
    )

    with pytest.raises(InvalidTicketCommentReferenceError):
        store.add_comment(
            organization_id=org_a.organization_id,
            ticket_id=ticket.ticket_id,
            author_user_id=charlie.user_id,
            visibility=TicketCommentVisibility.INTERNAL,
            content="cross tenant",
        )
