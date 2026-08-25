import json
from copy import deepcopy
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
from app.tools.knowledge_gateway import KnowledgeToolGateway
from app.tools.composite_gateway import CompositeToolGateway
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


def grounded_ticket_response(**kwargs):
    tool_message = next(
        message
        for message in reversed(kwargs["messages"])
        if message["role"] == "tool"
    )
    tool_result = json.loads(tool_message["content"])
    assert tool_result["ok"] is True
    ticket_no = tool_result["data"]["ticket_no"]
    return final_response(
        f"已核实订单尚未产生物流记录，并已创建工单 {ticket_no}。"
    )


def failed_ticket_response(**kwargs):
    tool_results = [
        json.loads(message["content"])
        for message in kwargs["messages"]
        if message["role"] == "tool"
    ]
    assert [result["ok"] for result in tool_results] == [False, False]
    assert [result["error"]["code"] for result in tool_results] == [
        "INVALID_ARGUMENTS",
        "CUSTOMER_NOT_FOUND",
    ]
    return final_response("无法创建工单：没有找到对应客户。")


def order_query_responses(order_no, final_text):
    return [
        tool_call_response(
            "call-order",
            "get_order",
            {"order_no": order_no},
        ),
        final_response(final_text),
    ]


class FakeCompletionClient:
    def __init__(self, responses):
        self._responses = iter(responses)
        self.calls = []
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self.create)
        )

    def create(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        response = next(self._responses)
        if callable(response):
            return response(**kwargs)
        return response


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
        grounded_ticket_response,
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
    assert [
        len(call["messages"]) for call in scope["client"].calls
    ] == [2, 4, 6, 8]
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


def test_same_agent_runner_rebinds_between_tenants(tmp_path):
    database_path = tmp_path / "tenants.db"
    users = SQLiteUserStore(database_path)
    organizations = SQLiteOrganizationStore(database_path)
    customers = SQLiteCustomerStore(database_path)
    orders = SQLiteOrderStore(database_path)

    contexts = []
    for username, company, item_summary in (
        ("alice", "Company A", "企业A耳机 x1"),
        ("bob", "Company B", "企业B键盘 x1"),
    ):
        user = users.create_user(
            username=username,
            password_hash="hash",
        )
        organization = organizations.create_with_admin(
            name=company,
            admin_user_id=user.user_id,
        )
        customer = customers.create_customer(
            organization_id=organization.organization_id,
            customer_no="CUST-001",
            name=f"{company} Customer",
        )
        orders.create_order(
            organization_id=organization.organization_id,
            order_no="ORD-SHARED-001",
            customer_id=customer.customer_id,
            status=OrderStatus.PROCESSING,
            item_summary=item_summary,
            total_amount_cents=10000,
            currency="CNY",
            placed_at="2026-07-25T08:00:00+00:00",
        )
        contexts.append(
            TenantContext(
                user_id=user.user_id,
                organization_id=organization.organization_id,
                role=MembershipRole.ADMIN,
            )
        )

    client = FakeCompletionClient(
        order_query_responses("ORD-SHARED-001", "企业A查询完成")
        + order_query_responses("ORD-SHARED-001", "企业B查询完成")
    )
    gateway = create_customer_support_tool_gateway(database_path)
    agent = CustomerSupportAgentRunner(
        client=client,
        gateway=gateway,
        model_name="fake-model",
    )

    first_messages = [{"role": "user", "content": "查询订单"}]
    second_messages = [{"role": "user", "content": "查询订单"}]
    agent(messages=first_messages, context=contexts[0])
    agent(messages=second_messages, context=contexts[1])

    assert "企业A耳机 x1" in first_messages[2]["content"]
    assert "企业B键盘 x1" in second_messages[2]["content"]
    assert "企业B键盘 x1" not in first_messages[2]["content"]
    assert "企业A耳机 x1" not in second_messages[2]["content"]


def test_business_and_knowledge_tools_share_one_grounded_turn(tmp_path):
    scope = build_golden_path(tmp_path)

    class FakeAdaptiveService:
        calls = []

        def search(self, **kwargs):
            self.calls.append(kwargs)
            payload = {
                "ok": True,
                "data": {
                    "result_code": "KNOWLEDGE_FOUND",
                    "strategy": "multi",
                    "evidence_status": "sufficient",
                    "citations": [{
                        "citation_id": "C1",
                        "document_id": "doc-a",
                        "version_id": "version-a",
                        "chunk_id": "chunk-a",
                        "title": "Delay compensation",
                        "heading_path": "/eligibility",
                        "content": "Compensation applies after the promised date.",
                    }],
                    "retrieval_summary": {
                        "strategy": "multi",
                        "round_count": 1,
                        "evidence_status": "sufficient",
                        "latency_ms": 2,
                    },
                },
            }
            return SimpleNamespace(public_dict=lambda: payload)

    adaptive = FakeAdaptiveService()
    composite = CompositeToolGateway([
        create_customer_support_tool_gateway(scope["database_path"]),
        KnowledgeToolGateway(service=adaptive),
    ])
    client = FakeCompletionClient([
        tool_call_response("call-order", "get_order", {"order_no": "ORD-GOLDEN-001"}),
        tool_call_response("call-logistics", "get_logistics", {"order_no": "ORD-GOLDEN-001"}),
        tool_call_response(
            "call-knowledge", "search_knowledge",
            {"question": "delay compensation eligibility and exclusions"},
        ),
        final_response("The delay compensation policy may apply [C1]."),
    ])
    agent = CustomerSupportAgentRunner(
        client=client, gateway=composite, model_name="fake-model"
    )

    result = agent(
        messages=[{"role": "user", "content": "Does this delayed order qualify?"}],
        context=scope["context"],
    )

    assert [
        event.tool_call_name
        for event in result.events
        if event.type == "tool_call.completed"
    ] == ["get_order", "get_logistics", "search_knowledge"]
    assert [item.citation_id for item in result.citations] == ["C1"]
    assert result.retrieval_summary.strategy == "multi"
    assert result.answer_incomplete is False
    assert adaptive.calls[0]["organization_id"] == scope["context"].organization_id


def test_agent_can_retry_after_gateway_validation_failure(tmp_path):
    database_path = tmp_path / "retry.db"
    users = SQLiteUserStore(database_path)
    organizations = SQLiteOrganizationStore(database_path)
    user = users.create_user(
        username="alice",
        password_hash="hash",
    )
    organization = organizations.create_with_admin(
        name="Company A",
        admin_user_id=user.user_id,
    )
    context = TenantContext(
        user_id=user.user_id,
        organization_id=organization.organization_id,
        role=MembershipRole.ADMIN,
    )
    client = FakeCompletionClient(
        [
            tool_call_response(
                "bad-ticket",
                "create_ticket",
                {
                    "summary": "客户要求创建工单",
                    "category": "other",
                    "priority": "medium",
                },
            ),
            tool_call_response(
                "good-ticket",
                "create_ticket",
                {
                    "customer_no": "CUST-MISSING",
                    "summary": "客户要求创建工单",
                    "category": "other",
                    "priority": "medium",
                },
            ),
            failed_ticket_response,
        ]
    )
    gateway = create_customer_support_tool_gateway(database_path)
    agent = CustomerSupportAgentRunner(
        client=client,
        gateway=gateway,
        model_name="fake-model",
    )
    messages = [{"role": "user", "content": "请创建工单"}]

    result = agent(messages=messages, context=context)

    assert messages[2]["role"] == "tool"
    assert messages[4]["role"] == "tool"
    first_tool_result = json.loads(messages[2]["content"])
    second_tool_result = json.loads(messages[4]["content"])
    assert first_tool_result["ok"] is False
    assert first_tool_result["error"]["code"] == "INVALID_ARGUMENTS"
    assert second_tool_result["ok"] is False
    assert second_tool_result["error"]["code"] == "CUSTOMER_NOT_FOUND"
    assert "无法创建工单" in result.llm_answer
    assert "创建成功" not in result.llm_answer
