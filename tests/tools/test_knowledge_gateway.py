from types import SimpleNamespace

import pytest

from app.application.organization_service import TenantContext
from app.organizations.base import MembershipRole
from app.tools.knowledge_gateway import KnowledgeToolGateway

ORG_A = TenantContext("user-a", "org-a", MembershipRole.AGENT)


class RecordingService:
    def __init__(self):
        self.calls = []

    def search(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            public_dict=lambda: {
                "ok": True,
                "citations": [],
                "retrieval_summary": {
                    "strategy": "baseline",
                    "round_count": 1,
                    "evidence_status": "insufficient",
                    "latency_ms": 1,
                },
                "error": None,
            }
        )


# 保护行为：模型只能提供原始查询文本，租户与检索参数均由服务端控制。
def test_bound_search_uses_server_context_and_only_question():
    service = RecordingService()
    gateway = KnowledgeToolGateway(service=service)
    payload = gateway.bind(context=ORG_A)["search_knowledge"](
        question=" return window "
    )
    assert service.calls == [
        {
            "organization_id": "org-a",
            "question": "return window",
            "conversation_id": None,
        }
    ]
    assert payload["ok"] is True
    assert payload["data"]["strategy"] == "baseline"
    assert payload["data"]["result_code"] == "INSUFFICIENT_EVIDENCE"
    schema = gateway.definitions[0]["function"]["parameters"]
    assert set(schema["properties"]) == {"question"}


# 边界情况：模型不得通过额外字段控制租户、身份或检索预算。
@pytest.mark.parametrize(
    "field", ["organization_id", "user_id", "role", "top_k", "rounds"]
)
def test_knowledge_tool_rejects_model_controlled_fields(field):
    gateway = KnowledgeToolGateway(service=RecordingService())
    result = gateway.bind(context=ORG_A)["search_knowledge"](
        **{"question": "returns", field: "attacker"}
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_ARGUMENTS"


# 保护行为：复杂问题可在同一请求内使用不同 query 连续检索两到三次。
def test_bound_gateway_allows_multiple_searches_per_request():
    service = RecordingService()
    gateway = KnowledgeToolGateway(service=service)
    fn = gateway.bind(context=ORG_A)["search_knowledge"]
    assert fn(question="returns")["ok"] is True
    assert fn(question="warranty")["ok"] is True
    assert fn(question="shipping exclusions")["ok"] is True
    assert [call["question"] for call in service.calls] == [
        "returns",
        "warranty",
        "shipping exclusions",
    ]
    assert gateway.bind(context=ORG_A)["search_knowledge"](
        question="new request"
    )["ok"] is True
