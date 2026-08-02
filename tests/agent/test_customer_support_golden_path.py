import json
from types import SimpleNamespace

from app.agent.prompts import SUPPORT_SYSTEM_PROMPT
from app.agent.support_runner import CustomerSupportAgentRunner
from app.application.chat_service import ChatService
from app.application.organization_service import TenantContext
from app.concurrency.conversation_locks import ConversationLockRegistry
from app.customers.sqlite_store import SQLiteCustomerStore
from app.orders.base import OrderStatus
from app.orders.sqlite_store import SQLiteOrderStore
from app.organizations.base import MembershipRole
from app.organizations.sqlite_store import SQLiteOrganizationStore
from app.sessions.sqlite_store import SQLiteSessionStore
from app.tickets.sqlite_store import SQLiteTicketStore
from app.tools.support_factory import create_customer_support_tool_gateway
from app.users.sqlite_store import SQLiteUserStore


def tool_call_response(call_id, name, arguments):
    tool_call = SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(
            name=name,
            arguments=json.dumps(arguments, ensure_ascii=False),
        ),
    )
    message = SimpleNamespace(
        content="",
        reasoning_content=f"调用 {name}",
        tool_calls=[tool_call],
    )
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def final_response(content):
    message = SimpleNamespace(
        content=content,
        reasoning_content="根据工具结果生成回复",
        tool_calls=None,
    )
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FakeCompletionClient:
    def __init__(self, responses):
        self._responses = iter(responses)
        self.calls = []
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self.create)
        )

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return next(self._responses)


def build_golden_path(tmp_path):
    database_path = tmp_path / "golden.db"
    users = SQLiteUserStore(database_path)
    organizations = SQLiteOrganizationStore(database_path)
    customers = SQLiteCustomerStore(database_path)
    orders = SQLiteOrderStore(database_path)
    sessions = SQLiteSessionStore(database_path)

    user = users.create_user(username="alice", password_hash="hash")
    organization = organizations.create_with_admin(
        name="Company A",
        admin_user_id=user.user_id,
    )
    customer = customers.create_customer(
        organization_id=organization.organization_id,
        customer_no="CUST-GOLDEN-001",
        name="林晓",
    )
    order = orders.create_order(
        organization_id=organization.organization_id,
        order_no="ORD-GOLDEN-001",
        customer_id=customer.customer_id,
        status=OrderStatus.PROCESSING,
        item_summary="无线耳机 x1",
        total_amount_cents=39900,
        currency="CNY",
        placed_at="2026-07-25T08:00:00+00:00",
        promised_ship_at="2026-07-28T08:00:00+00:00",
    )
    context = TenantContext(
        user_id=user.user_id,
        organization_id=organization.organization_id,
        role=MembershipRole.ADMIN,
    )
    responses = [
        tool_call_response(
            "call-order", "get_order", {"order_no": order.order_no}
        ),
        tool_call_response(
            "call-logistics",
            "get_logistics",
            {"order_no": order.order_no},
        ),
        tool_call_response(
            "call-ticket",
            "create_ticket",
            {
                "order_no": order.order_no,
                "summary": "订单超过承诺时间仍未发货",
                "category": "logistics",
                "priority": "high",
            },
        ),
        final_response(
            "已核实订单尚未产生物流记录，并已创建工单 TKT-GOLDEN-001。"
        ),
    ]
    client = FakeCompletionClient(responses)
    gateway = create_customer_support_tool_gateway(
        database_path,
        ticket_no_factory=lambda: "TKT-GOLDEN-001",
    )
    agent = CustomerSupportAgentRunner(
        client=client,
        gateway=gateway,
        model_name="fake-support-model",
    )
    chat = ChatService(
        store=sessions,
        run_agent=agent,
        locks=ConversationLockRegistry(),
        base_system_prompt=SUPPORT_SYSTEM_PROMPT,
    )
    conversation = chat.create_conversation(context=context)
    return {
        "database_path": database_path,
        "client": client,
        "chat": chat,
        "context": context,
        "conversation": conversation,
        "sessions": sessions,
        "tickets": SQLiteTicketStore(database_path),
    }


def test_delayed_order_creates_ticket_and_grounded_reply(tmp_path):
    scope = build_golden_path(tmp_path)

    result = scope["chat"].chat(
        context=scope["context"],
        conversation_id=scope["conversation"].conversation_id,
        question=(
            "订单 ORD-GOLDEN-001 已超过承诺发货时间，"
            "请查询订单和物流；如果确实还没有发货，"
            "请创建一个高优先级物流工单并告诉我工单号。"
        ),
    )

    assert result.llm_answer == (
        "已核实订单尚未产生物流记录，并已创建工单 TKT-GOLDEN-001。"
    )
    assert [
        event.tool_call_name
        for event in result.events
        if event.type == "tool_call.completed"
    ] == ["get_order", "get_logistics", "create_ticket"]
    ticket = scope["tickets"].get_by_no(
        organization_id=scope["context"].organization_id,
        ticket_no="TKT-GOLDEN-001",
    )
    assert ticket.summary == "订单超过承诺时间仍未发货"

    assert len(scope["client"].calls) == 4
    for call in scope["client"].calls:
        assert call["model"] == "fake-support-model"
        assert {
            item["function"]["name"] for item in call["tools"]
        } == {
            "get_order",
            "get_logistics",
            "create_ticket",
            "add_ticket_note",
        }

    history = scope["sessions"].load_messages(
        organization_id=scope["context"].organization_id,
        user_id=scope["context"].user_id,
        conversation_id=scope["conversation"].conversation_id,
    )
    tool_messages = [
        message for message in history if message["role"] == "tool"
    ]
    assert len(tool_messages) == 3
    assert '"availability": "not_created"' in tool_messages[1]["content"]
    assert '"ticket_no": "TKT-GOLDEN-001"' in tool_messages[2]["content"]
    assert history[-1]["content"] == result.llm_answer
