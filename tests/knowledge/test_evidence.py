import json

import pytest
from pydantic import ValidationError

from app.knowledge.base import ChunkWithDocumentTitle
from app.knowledge.evidence import (
    EvidenceAssessment,
    EvidenceAssessor,
    EvidenceStatus,
    RoundEvidence,
)
from app.knowledge.planning import SearchPlan, SearchReasonCode, SearchStrategy
from app.knowledge.structured_llm import StructuredCompletion


class FakeStructuredClient:
    def __init__(self, raw_json=""):
        self.raw_json = raw_json
        self.calls = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        return StructuredCompletion(raw_json=self.raw_json, estimated_tokens=11)


def multi_plan(*queries):
    return SearchPlan(
        strategy=SearchStrategy.MULTI,
        queries=queries,
        reason_code=SearchReasonCode.MULTI_CONDITION,
    )


def evidence(score=0.8, matched=(0, 1)):
    content = "seven day return window"
    return RoundEvidence(
        chunk=ChunkWithDocumentTitle(
            chunk_id="chunk-1",
            organization_id="org-a",
            document_id="doc-1",
            version_id="version-1",
            ordinal=0,
            heading_path="/returns",
            content=content,
            token_count=5,
            document_title="Returns",
            # 区间与 content 自洽（该断言不需要真实正文，只需偏移可携带）。
            start_offset=0,
            end_offset=len(content),
        ),
        fused_score=score,
        matched_query_indexes=matched,
        first_round=1,
        first_query_index=0,
    )


def test_empty_or_non_positive_evidence_is_insufficient_without_model():
    client = FakeStructuredClient()
    assessor = EvidenceAssessor(client=client, min_fused_score=0.0)
    decision = assessor.assess(
        plan=multi_plan("packaging", "window"),
        evidence=[],
        round_number=1,
        timeout_seconds=5,
    )
    assert decision.assessment.status is EvidenceStatus.INSUFFICIENT
    assert decision.assessment.follow_up_queries == ()
    assert decision.model_calls == 0
    assert client.calls == []

    zero_score = assessor.assess(
        plan=multi_plan("packaging", "window"),
        evidence=[evidence(score=0.0)],
        round_number=1,
        timeout_seconds=5,
    )
    assert zero_score.assessment.status is EvidenceStatus.INSUFFICIENT
    assert zero_score.model_calls == 0
    assert client.calls == []


def test_low_score_uses_deterministic_gate():
    client = FakeStructuredClient()
    assessor = EvidenceAssessor(client=client, min_fused_score=0.5)
    low = assessor.assess(
        plan=multi_plan("packaging", "window"),
        evidence=[evidence(score=0.4)],
        round_number=1,
        timeout_seconds=5,
    )
    assert low.model_calls == 0
    assert low.assessment.status is EvidenceStatus.INSUFFICIENT


def test_uncovered_multi_query_still_calls_model_and_enforces_coverage():
    # 保护行为：MULTI 的某个计划子查询没有命中时，不应直接短路；
    # 仍应让模型有机会提出补充查询，但即使模型返回 SUFFICIENT，
    # 也必须强制降级为 INSUFFICIENT，避免未覆盖的方面被误判为充分。
    client = FakeStructuredClient(
        json.dumps(
            {
                "status": "SUFFICIENT",
                "covered_aspects": [1],
                "missing_aspects": [],
                "follow_up_queries": [],
            }
        )
    )
    decision = EvidenceAssessor(client=client, min_fused_score=0.0).assess(
        plan=multi_plan("packaging", "window"),
        evidence=[evidence(score=0.8, matched=(0,))],
        round_number=1,
        timeout_seconds=5,
    )
    assert decision.model_calls == 1
    assert decision.assessment.status is EvidenceStatus.INSUFFICIENT
    assert decision.assessment.missing_aspects == ("query_2",)


@pytest.mark.parametrize(
    "payload",
    [
        {
            "status": "SUFFICIENT",
            "covered_aspects": [1, 2],
            "missing_aspects": [],
            "follow_up_queries": [],
        },
        {
            "status": "INSUFFICIENT",
            "covered_aspects": [1],
            "missing_aspects": ["conditions"],
            "follow_up_queries": ["exceptions"],
        },
    ],
)
def test_assessment_accepts_valid_shapes(payload):
    assert EvidenceAssessment.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "SUFFICIENT", "covered_aspects": [0], "missing_aspects": [], "follow_up_queries": []},
        {"status": "SUFFICIENT", "covered_aspects": [1], "missing_aspects": [], "follow_up_queries": ["x"]},
        {"status": "INSUFFICIENT", "covered_aspects": [], "missing_aspects": [], "follow_up_queries": ["1", "2", "3"]},
        {"status": "INSUFFICIENT", "covered_aspects": [], "missing_aspects": ["x"], "follow_up_queries": [], "answer": "invented"},
    ],
)
def test_assessment_rejects_invalid_or_model_controlled_fields(payload):
    with pytest.raises(ValidationError):
        EvidenceAssessment.model_validate(payload)


def test_model_followups_are_stripped_and_deduplicated():
    client = FakeStructuredClient(
        json.dumps(
            {
                "status": "INSUFFICIENT",
                "covered_aspects": [1, 2],
                "missing_aspects": ["exceptions"],
                "follow_up_queries": [" Packaging ", "EXCEPTIONS"],
            }
        )
    )
    decision = EvidenceAssessor(client=client, min_fused_score=0.0).assess(
        plan=multi_plan("packaging", "window"),
        evidence=[evidence()],
        round_number=1,
        timeout_seconds=5,
    )
    assert decision.assessment.follow_up_queries == ("EXCEPTIONS",)
    prompt = client.calls[0]["system_prompt"]
    assert "json" in prompt
    assert "Do not generate the final answer" in prompt
    assert '"status":"SUFFICIENT"' in prompt
    assert '"status":"INSUFFICIENT"' in prompt


def test_model_followups_remove_same_batch_casefold_duplicates():
    client = FakeStructuredClient(
        json.dumps(
            {
                "status": "INSUFFICIENT",
                "covered_aspects": [1, 2],
                "missing_aspects": ["exceptions"],
                "follow_up_queries": ["EXCEPTIONS", "exceptions"],
            }
        )
    )
    decision = EvidenceAssessor(client=client, min_fused_score=0.0).assess(
        plan=multi_plan("packaging", "window"),
        evidence=[evidence()],
        round_number=1,
        timeout_seconds=5,
    )
    assert decision.assessment.follow_up_queries == ("EXCEPTIONS",)


def test_invalid_assessment_degrades_to_insufficient_without_followup():
    decision = EvidenceAssessor(
        client=FakeStructuredClient("not json"), min_fused_score=0.0
    ).assess(
        plan=multi_plan("packaging", "window"),
        evidence=[evidence()],
        round_number=1,
        timeout_seconds=5,
    )
    assert decision.assessment.status is EvidenceStatus.INSUFFICIENT
    assert decision.assessment.missing_aspects == ("assessment_invalid",)
    assert decision.assessment.follow_up_queries == ()
    assert decision.degraded is True
