import pytest
from pydantic import ValidationError

from app.knowledge.planning import (
    QueryPlanner,
    SearchPlan,
    SearchReasonCode,
    SearchStrategy,
)
from app.knowledge.structured_llm import StructuredCompletion


class FakeStructuredClient:
    def __init__(self, raw_json):
        self.raw_json = raw_json
        self.calls = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        return StructuredCompletion(raw_json=self.raw_json, estimated_tokens=17)


@pytest.mark.parametrize(
    "payload",
    [
        {"strategy": "NONE", "queries": [], "reason_code": "BUSINESS_ONLY"},
        {
            "strategy": "SINGLE",
            "queries": ["return window"],
            "reason_code": "SIMPLE_POLICY",
        },
        {
            "strategy": "MULTI",
            "queries": ["packaging", "return window"],
            "reason_code": "MULTI_CONDITION",
        },
    ],
)
def test_search_plan_accepts_only_valid_strategy_shapes(payload):
    assert SearchPlan.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"strategy": "NONE", "queries": ["x"], "reason_code": "BUSINESS_ONLY"},
        {"strategy": "SINGLE", "queries": [], "reason_code": "SIMPLE_POLICY"},
        {"strategy": "MULTI", "queries": ["x"], "reason_code": "MULTI_CONDITION"},
        {
            "strategy": "MULTI",
            "queries": ["1", "2", "3", "4"],
            "reason_code": "MULTI_CONDITION",
        },
        {
            "strategy": "SINGLE",
            "queries": ["x"],
            "reason_code": "SIMPLE_POLICY",
            "organization_id": "org-b",
        },
        {
            "strategy": "MULTI",
            "queries": ["Returns", "returns"],
            "reason_code": "MULTI_CONDITION",
        },
    ],
)
def test_search_plan_rejects_invalid_or_model_controlled_fields(payload):
    with pytest.raises(ValidationError):
        SearchPlan.model_validate(payload)


@pytest.mark.parametrize(
    "raw_json",
    [
        "not json",
        '{"strategy":"SINGLE","queries":["x"],"reason_code":"SIMPLE_POLICY","top_k":9}',
        '{"strategy":"SINGLE","queries":["  "],"reason_code":"SIMPLE_POLICY"}',
        '{"strategy":"MULTI","queries":["1","2","3","4"],"reason_code":"MULTI_CONDITION"}',
        "",
    ],
)
def test_invalid_model_output_degrades_to_original_single_query(raw_json):
    client = FakeStructuredClient(raw_json)
    decision = QueryPlanner(client).plan(
        question="  original question  ", timeout_seconds=4
    )

    assert decision.plan == SearchPlan(
        strategy=SearchStrategy.SINGLE,
        queries=("original question",),
        reason_code=SearchReasonCode.SIMPLE_POLICY,
    )
    assert decision.degraded is True
    assert decision.model_calls == 1
    assert decision.estimated_tokens == 17


def test_planner_prompt_contains_json_examples_and_all_reason_codes():
    client = FakeStructuredClient(
        '{"strategy":"NONE","queries":[],"reason_code":"BUSINESS_ONLY"}'
    )
    QueryPlanner(client).plan(question="order status", timeout_seconds=4)
    prompt = client.calls[0]["system_prompt"]

    assert "json" in prompt
    assert '"strategy":"NONE"' in prompt
    assert '"strategy":"SINGLE"' in prompt
    assert '"strategy":"MULTI"' in prompt
    for reason in SearchReasonCode:
        assert reason.value in prompt
