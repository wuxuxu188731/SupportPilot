from dataclasses import dataclass, field

from app.agent.invocation_context import tenant_of
from app.agent.support_runner import CustomerSupportAgentRunner
from app.application.organization_service import TenantContext
from app.organizations.base import MembershipRole
from app.schemas.chat import LLMResponse


ORG_A = TenantContext(
    user_id="user-a",
    organization_id="org-a",
    role=MembershipRole.AGENT,
)
ORG_B = TenantContext(
    user_id="user-b",
    organization_id="org-b",
    role=MembershipRole.ADMIN,
)


@dataclass
class FakeGateway:
    bound_contexts: list[TenantContext] = field(default_factory=list)

    @property
    def definitions(self):
        return [
            {
                "type": "function",
                "function": {"name": "get_order"},
            }
        ]

    def bind(self, *, context):
        # 与真实客服 Gateway 一致：只读取可信租户部分（设计 12.1）。
        tenant = tenant_of(context)
        self.bound_contexts.append(tenant)

        def get_order(**arguments):
            return {
                "ok": True,
                "data": {
                    "organization": tenant.organization_id,
                    "arguments": arguments,
                },
            }

        return {"get_order": get_order}


@dataclass
class RecordingRunTurn:
    calls: list[dict] = field(default_factory=list)

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return LLMResponse(llm_answer="answer")


def test_support_runner_binds_gateway_for_each_context():
    gateway = FakeGateway()
    run_turn = RecordingRunTurn()
    runner = CustomerSupportAgentRunner(
        client=object(),
        gateway=gateway,
        model_name="test-model",
        max_tool_rounds=5,
        run_turn=run_turn,
    )

    runner(
        messages=[{"role": "user", "content": "A"}],
        context=ORG_A,
    )
    runner(
        messages=[{"role": "user", "content": "B"}],
        context=ORG_B,
    )

    # 运行器把纯 TenantContext 包装为 AgentInvocationContext 后传给
    # Gateway；Gateway 只读取租户部分（生产链路由 ChatService 提供
    # 真实会话与回合标识）。
    assert gateway.bound_contexts == [ORG_A, ORG_B]
    first_function = run_turn.calls[0]["tool_functions"]["get_order"]
    second_function = run_turn.calls[1]["tool_functions"]["get_order"]
    assert first_function(order_no="ORD-001")["data"]["organization"] == "org-a"
    assert second_function(order_no="ORD-001")["data"]["organization"] == "org-b"


def test_support_runner_forwards_gateway_and_limits():
    gateway = FakeGateway()
    run_turn = RecordingRunTurn()
    client = object()
    runner = CustomerSupportAgentRunner(
        client=client,
        gateway=gateway,
        model_name="support-model",
        max_tool_rounds=6,
        run_turn=run_turn,
    )
    messages = [{"role": "user", "content": "hello"}]

    result = runner(messages=messages, context=ORG_A)

    call = run_turn.calls[0]
    assert result.llm_answer == "answer"
    assert call["messages"] is messages
    assert call["client"] is client
    assert call["tool_definitions"] == gateway.definitions
    assert set(call["tool_functions"]) == {"get_order"}
    assert call["model_name"] == "support-model"
    assert call["max_tool_rounds"] == 6
    assert call["on_event"] is None
