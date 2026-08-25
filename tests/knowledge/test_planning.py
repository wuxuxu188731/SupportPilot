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


def test_policy_question_none_is_corrected_to_single_search():
    # 保护行为：模型错误地把政策问题判成 NONE 时，
    # Planner 应降级为 SINGLE 并使用原始问题检索，避免直接丢失证据。
    client = FakeStructuredClient(
        '{"strategy":"NONE","queries":[],"reason_code":"BUSINESS_ONLY"}'
    )
    decision = QueryPlanner(client).plan(
        question="普通会员无理由退货期是多少？", timeout_seconds=4
    )

    assert decision.plan == SearchPlan(
        strategy=SearchStrategy.SINGLE,
        queries=("普通会员无理由退货期是多少？",),
        reason_code=SearchReasonCode.SIMPLE_POLICY,
    )
    assert decision.degraded is True
    assert decision.model_calls == 1


def test_weak_policy_keyword_without_policy_hint_keeps_none():
    # 保护行为：像“查一下物流”这类纯业务查询不应被关键词误判为政策检索。
    client = FakeStructuredClient(
        '{"strategy":"NONE","queries":[],"reason_code":"BUSINESS_ONLY"}'
    )
    decision = QueryPlanner(client).plan(
        question="查一下物流", timeout_seconds=4
    )

    assert decision.plan.strategy is SearchStrategy.NONE
    assert decision.degraded is False


def test_weak_policy_keyword_with_policy_hint_is_corrected_to_single():
    # 保护行为：包含“发货 + 多久”这类政策意图时，即使模型返回 NONE 也应检索。
    client = FakeStructuredClient(
        '{"strategy":"NONE","queries":[],"reason_code":"BUSINESS_ONLY"}'
    )
    decision = QueryPlanner(client).plan(
        question="现货商品付款后多久发货？", timeout_seconds=4
    )

    assert decision.plan.strategy is SearchStrategy.SINGLE
    assert decision.plan.queries == ("现货商品付款后多久发货？",)
    assert decision.degraded is True


def test_multi_plan_with_three_queries_is_capped_to_two():
    # 保护行为：当前 15 秒总预算下，3 个首轮查询容易在 Assessor 前超时；
    # Planner 应把 MULTI 查询数限制为 2，降低 Agentic Search 整体失败率。
    client = FakeStructuredClient(
        '{"strategy":"MULTI","queries":["退货时限","运费承担","商品状态"],'
        '"reason_code":"MULTI_CONDITION"}'
    )
    decision = QueryPlanner(client).plan(
        question="退货有什么条件？", timeout_seconds=4
    )

    assert decision.plan.strategy is SearchStrategy.MULTI
    assert decision.plan.queries == ("退货时限", "运费承担")
    assert decision.degraded is True


def test_short_ambiguous_policy_question_keeps_none():
    # 保护行为：过于简短、缺少具体政策对象的歧义问题应保留 NONE，
    # 避免把“这个能不能退？”误判成明确政策检索而破坏澄清流程。
    client = FakeStructuredClient(
        '{"strategy":"NONE","queries":[],"reason_code":"BUSINESS_ONLY"}'
    )
    decision = QueryPlanner(client).plan(
        question="这个能不能退？", timeout_seconds=4
    )

    assert decision.plan.strategy is SearchStrategy.NONE
    assert decision.degraded is False
