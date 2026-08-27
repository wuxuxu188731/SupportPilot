"""Stage C 最终答案 A/B Runner 的无网络行为测试。"""

from __future__ import annotations

from types import SimpleNamespace

from app.evals.stage_c.answer_ab import (
    AnswerCheckpoint,
    AnswerRunConfig,
    EvaluationKnowledgeToolGateway,
    FixedBusinessToolGateway,
    StageCAnswerABRunner,
    normalize_knowledge_result,
    parse_judge_json,
)
from app.evals.stage_c.models import (
    EvidenceAlternative,
    EvidenceGroup,
    ExpectedBehavior,
    KeyAnswerFact,
    StageCCase,
    StageCCategory,
    StageCScenario,
    StageCVariant,
    TenantKey,
)
from app.knowledge.results import (
    AdaptiveSearchResult,
    BaselineSearchResult,
    Citation,
    RetrievalSummary,
    RetrievalTrace,
)


def make_case() -> StageCCase:
    return StageCCase(
        case_id="answer-case-1",
        category=StageCCategory.SIMPLE_POLICY,
        scenario=StageCScenario.MAIN_ACTIVE,
        tenant_key=TenantKey.ORG_A,
        question="普通会员退货期是多少？",
        reference_answer="普通会员退货期是7天。",
        key_answer_facts=(KeyAnswerFact(fact_id="window", statement="退货期7天"),),
        required_evidence_groups=(
            EvidenceGroup(
                group_id="window",
                supports_fact_ids=("window",),
                any_of=(
                    EvidenceAlternative(
                        document_key="returns_exchange", heading_path="退货/期限"
                    ),
                ),
            ),
        ),
        should_have_answer=True,
        expected_behavior=ExpectedBehavior.ANSWER_GROUNDED,
        strategy_expectation=None,
        business_context=None,
        forbidden_tenant_keys=(TenantKey.ORG_B,),
        security_expectations=None,
        notes="",
    )


def search_result(variant: StageCVariant):
    citation = Citation(
        citation_id="C1",
        document_id="doc-1",
        version_id="version-1",
        chunk_id="chunk-1",
        title="退货政策",
        heading_path="退货/期限",
        content="普通会员退货期为7天。",
    )
    summary = RetrievalSummary(
        strategy="single", round_count=1, evidence_status="sufficient", latency_ms=3
    )
    common = dict(
        ok=True,
        citations=(citation,),
        retrieval_summary=summary,
        error=None,
        selected_chunks=(),
    )
    if variant is StageCVariant.BASELINE:
        return BaselineSearchResult(**common, retrieval_trace=RetrievalTrace.empty())
    return AdaptiveSearchResult(**common, retrieval_trace={})


class FakeService:
    def __init__(self, variant: StageCVariant) -> None:
        self.variant = variant  # 当前假服务对应的检索变体
        self.calls: list[dict] = []  # 收到的租户、问题和会话参数

    def search(self, **kwargs):
        self.calls.append(kwargs)
        return search_result(self.variant)


class FakeCompletions:
    def __init__(self) -> None:
        self.answer_round = 0  # 已处理的最终回答模型轮次数
        self.answer_requests: list[dict] = []  # 两个变体的回答请求，用于公平性断言

    def create(self, **kwargs):
        if "response_format" in kwargs:
            message = SimpleNamespace(
                content=(
                    '{"winner":"tie","scores":{"A":{"factuality":4,'
                    '"completeness":4,"groundedness":4,"behavior":4},'
                    '"B":{"factuality":4,"completeness":4,'
                    '"groundedness":4,"behavior":4}},"reason_codes":["equal"]}'
                )
            )
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

        self.answer_requests.append(kwargs)
        self.answer_round += 1
        if self.answer_round % 2 == 1:
            tool_call = SimpleNamespace(
                id=f"call-{self.answer_round}",
                function=SimpleNamespace(
                    name="search_knowledge",
                    arguments='{"question":"普通会员退货期是多少？"}',
                ),
            )
            message = SimpleNamespace(
                content="", reasoning_content="不应进入产物", tool_calls=[tool_call]
            )
        else:
            message = SimpleNamespace(
                content="普通会员退货期为7天 [C1]。",
                reasoning_content="不应进入产物",
                tool_calls=None,
            )
        usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


def fake_client():
    completions = FakeCompletions()
    return SimpleNamespace(
        chat=SimpleNamespace(completions=completions), completions=completions
    )


# 保护行为：Baseline 与 Adaptive 必须暴露相同的知识工具结果结构。
def test_normalizes_both_retrieval_variants_to_one_contract():
    baseline = normalize_knowledge_result(search_result(StageCVariant.BASELINE))
    adaptive = normalize_knowledge_result(search_result(StageCVariant.ADAPTIVE))

    assert baseline == adaptive
    assert baseline["data"]["citations"][0]["citation_id"] == "C1"


# 边界情况：供应商给 JSON 包裹代码围栏或简短前缀时仍应稳定解析。
def test_parses_fenced_or_prefixed_judge_json():
    assert parse_judge_json('```json\n{"winner":"tie"}\n```') == {"winner": "tie"}
    assert parse_judge_json('评审结果：{"winner":"A"}') == {"winner": "A"}
    assert parse_judge_json('{"winner":"B"}\n{"extra":true}') == {"winner": "B"}


# 保护行为：知识工具每个答案最多调用一次，并透传固定会话标识。
def test_evaluation_knowledge_gateway_enforces_one_call_budget():
    service = FakeService(StageCVariant.BASELINE)
    gateway = EvaluationKnowledgeToolGateway(service=service, conversation_id="answer-run")
    context = SimpleNamespace(organization_id="trusted-org")
    function = gateway.bind(context=context)["search_knowledge"]

    assert function(question="退货") ["ok"] is True
    assert function(question="再次搜索")["error"]["code"] == "SEARCH_BUDGET_EXCEEDED"
    assert service.calls[0]["conversation_id"] == "answer-run"


# 边界情况：非混合用例的只读业务工具不得伪造订单或物流事实。
def test_fixed_business_gateway_returns_not_found_without_fixture():
    functions = FixedBusinessToolGateway(make_case()).bind(context=SimpleNamespace())

    assert functions["get_order"](order_no="ORD-X")["error"]["code"] == "ORDER_NOT_FOUND"
    assert functions["get_logistics"](order_no="ORD-X")["error"]["code"] == "ORDER_NOT_FOUND"


# 保护行为：同一 Prompt、模型和工具定义生成两组答案，并逐项写入断点与盲评结果。
def test_runner_generates_fair_pair_and_checkpoints_without_reasoning(tmp_path):
    baseline = FakeService(StageCVariant.BASELINE)
    adaptive = FakeService(StageCVariant.ADAPTIVE)
    client = fake_client()
    checkpoint = AnswerCheckpoint(tmp_path / "answers.json", {"fixture": "same"})
    runner = StageCAnswerABRunner(
        cases=(make_case(),),
        services=SimpleNamespace(baseline=baseline, adaptive=adaptive),
        client=client,
        checkpoint=checkpoint,
        config=AnswerRunConfig(model_name="fixed-answer-model"),
        organization_ids={TenantKey.ORG_A: "trusted-org"},
    )

    report = runner.run()

    assert report["completion"] == {
        "case_count": 1,
        "expected_answers": 2,
        "completed_answers": 2,
        "completed_pairs": 1,
    }
    assert report["pairwise"] == {
        "baseline_wins": 0,
        "adaptive_wins": 0,
        "ties": 1,
        "by_category": {
            "simple_policy": {"baseline_wins": 0, "adaptive_wins": 0, "ties": 1}
        },
    }
    assert [request["model"] for request in client.completions.answer_requests] == [
        "fixed-answer-model",
        "fixed-answer-model",
        "fixed-answer-model",
        "fixed-answer-model",
    ]
    assert (
        client.completions.answer_requests[0]["tools"]
        == client.completions.answer_requests[2]["tools"]
    )
    assert "不应进入产物" not in (tmp_path / "answers.json").read_text(encoding="utf-8")
    assert report["variants"]["baseline"]["answers_with_valid_citations"] == 1
    assert report["variants"]["adaptive"]["answers_with_valid_citations"] == 1


# 保护行为：重复运行必须直接复用两个答案和评审，不产生新的模型调用。
def test_runner_resumes_completed_pair(tmp_path):
    baseline = FakeService(StageCVariant.BASELINE)
    adaptive = FakeService(StageCVariant.ADAPTIVE)
    client = fake_client()
    checkpoint = AnswerCheckpoint(tmp_path / "answers.json", {"fixture": "same"})
    runner = StageCAnswerABRunner(
        cases=(make_case(),),
        services=SimpleNamespace(baseline=baseline, adaptive=adaptive),
        client=client,
        checkpoint=checkpoint,
        config=AnswerRunConfig(model_name="fixed-answer-model"),
    )
    runner.run()
    calls_after_first_run = len(client.completions.answer_requests)

    runner.run()

    assert len(client.completions.answer_requests) == calls_after_first_run
    assert len(baseline.calls) == 1
    assert len(adaptive.calls) == 1
