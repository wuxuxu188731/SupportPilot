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
                "data": {
                    "result_code": "SEARCH_NOT_NEEDED",
                    "strategy": "none",
                    "evidence_status": "not_needed",
                    "citations": [],
                    "retrieval_summary": {
                        "strategy": "none",
                        "round_count": 0,
                        "evidence_status": "not_needed",
                        "latency_ms": 1,
                    },
                },
            }
        )


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
    schema = gateway.definitions[0]["function"]["parameters"]
    assert set(schema["properties"]) == {"question"}


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


def test_bound_gateway_allows_one_search_per_request():
    service = RecordingService()
    gateway = KnowledgeToolGateway(service=service)
    fn = gateway.bind(context=ORG_A)["search_knowledge"]
    assert fn(question="returns")["ok"] is True
    second = fn(question="warranty")
    assert second["error"]["code"] == "SEARCH_BUDGET_EXCEEDED"
    assert len(service.calls) == 1
    assert gateway.bind(context=ORG_A)["search_knowledge"](
        question="new request"
    )["ok"] is True
