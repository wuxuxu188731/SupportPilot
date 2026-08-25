"""Unit tests for the first knowledge baseline eval set and its runner.

This file tests ONLY offline, network-free logic:

* the eval corpus shape (16 cases, 4 per category, unique ids, schema-valid,
  every desired citation is findable in the corresponding loader output);
* the pure metric rules (recall@5, citation precision incl. the empty-citation
  edge cases, cross-tenant leak, P95 nearest-rank) against hand-built results;
* the runner's aggregation / report compilation from fake per-case metrics.

Nothing here talks to DashScope, Qdrant, or the network: the case/loader
assertions use ``DocumentLoader`` only, and the metric tests construct
:class:`EvalCase` / result descriptors directly.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from app.knowledge.base import ChunkWithDocumentTitle, DocumentSourceType
from app.knowledge.document_loader import DocumentLoader
from app.knowledge.retrieval import BaselineKnowledgeSearchService
from app.knowledge.vector_store import VectorCandidate
from scripts.run_knowledge_baseline_eval import (
    CaseMetrics,
    EvalCase,
    ExpectedRelevant,
    _hit_count,
    _matches_expected,
    case_report_dict,
    compile_report,
    cross_tenant_leak,
    evaluate_case,
    load_eval_cases,
    percentile_nearest_rank,
    recall_at_5,
    relevant_returned_count,
    retrieval_precision_at_5,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CASES_PATH = REPO_ROOT / "evals" / "knowledge" / "cases.jsonl"
SCHEMA_PATH = REPO_ROOT / "evals" / "knowledge" / "schema.json"
DOCS_ROOT = REPO_ROOT / "evals" / "knowledge" / "documents"
HISTORICAL_CANDIDATES_PATH = (
    REPO_ROOT / "tests" / "evals" / "fixtures" /
    "stage_a_fused_candidates.json"
)

EXPECTED_CATEGORIES = {
    "simple_policy",
    "multi_condition_policy",
    "mixed_fact_policy",
    "safety",
}
EXPECTED_TOTAL = 16
EXPECTED_PER_CATEGORY = 4


# --------------------------------------------------------------------------- #
# Corpus shape assertions
# --------------------------------------------------------------------------- #
def _load_schema() -> dict:
    import json

    with open(SCHEMA_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


def test_cases_total_and_per_category():
    cases = load_eval_cases(CASES_PATH)
    assert len(cases) == EXPECTED_TOTAL

    counts: dict[str, int] = {}
    for case in cases:
        counts[case.category] = counts.get(case.category, 0) + 1
    assert set(counts) == EXPECTED_CATEGORIES
    for category in EXPECTED_CATEGORIES:
        assert counts[category] == EXPECTED_PER_CATEGORY, category


def test_case_ids_are_unique():
    cases = load_eval_cases(CASES_PATH)
    ids = [c.case_id for c in cases]
    assert len(ids) == len(set(ids))


def test_every_case_conforms_to_schema():
    import json

    with open(CASES_PATH, "r", encoding="utf-8") as handle:
        lines = [
            json.loads(line)
            for line in handle
            if line.strip() and not line.startswith("﻿")
        ]
    validator = Draft202012Validator(_load_schema())
    assert len(lines) == EXPECTED_TOTAL
    errors = sorted(
        f"{index}: {error.message}"
        for index, line in enumerate(lines)
        for error in validator.iter_errors(line)
    )
    assert errors == []


def test_corpus_has_exactly_four_documents():
    # org_a: returns + logistics_compensation + warranty; org_b: returns.
    file_stem_a = {
        p.stem for p in (DOCS_ROOT / "org_a").glob("*.md")
    }
    assert file_stem_a == {"returns", "logistics_compensation", "warranty"}
    file_stem_b = {
        p.stem for p in (DOCS_ROOT / "org_b").glob("*.md")
    }
    assert file_stem_b == {"returns"}


def _loaded_paths(tenant: str, document_key: str) -> list[str]:
    loader = DocumentLoader()
    path = DOCS_ROOT / tenant / f"{document_key}.md"
    loaded = loader.load(path.read_bytes(), DocumentSourceType.MARKDOWN)
    return [section.heading_path for section in loaded.sections]


def test_all_expected_relevant_findable_in_loader_output():
    cases = load_eval_cases(CASES_PATH)
    for case in cases:
        for expected in case.expected_relevant:
            heading_paths = [
                p
                for p in _loaded_paths(case.tenant_key, expected.document_key)
                if p is not None
            ]
            assert any(
                path == expected.heading_path
                or path.endswith("/" + expected.heading_path)
                for path in heading_paths
            ), (
                f"case {case.case_id}: heading '{expected.heading_path}' not "
                f"found in {case.tenant_key}/{expected.document_key}.md"
            )


def test_safety_cases_declare_distinct_expected_behaviors():
    cases = load_eval_cases(CASES_PATH)
    safety = {c.case_id: c for c in cases if c.category == "safety"}
    assert len(safety) == EXPECTED_PER_CATEGORY
    assert {
        case_id: case.expected_behavior for case_id, case in safety.items()
    } == {
        "safety-unknown-exchange-01": "abstain",
        "safety-orgb-policy-01": "deny_cross_tenant",
        "safety-ignore-rule-01": "answer_grounded",
        "safety-vague-return-01": "clarify",
    }
    grounded = safety["safety-ignore-rule-01"]
    assert grounded.expected_relevant == (
        ExpectedRelevant(document_key="returns", heading_path="退货时限"),
    )
    assert grounded.should_have_answer is True
    for case in cases:
        if case.category != "safety":
            assert case.expected_behavior is None


def test_org_b_isolated_to_safety_read_case():
    # Every case is an org_a tenant query. The isolation probe is an org_a case
    # that asks after org_b's (conflicting) policy and must both declare
    # forbidden_tenant_keys=[org_b] and expect no answer.
    cases = load_eval_cases(CASES_PATH)
    assert all(c.tenant_key == "org_a" for c in cases)
    read_probe = [c for c in cases if "orgb" in c.case_id]
    assert len(read_probe) >= 1
    for case in read_probe:
        assert case.forbidden_tenant_keys == ("org_b",)
        assert case.category == "safety"
        assert case.should_have_answer is False
        assert case.expected_relevant == ()
        assert case.expected_behavior == "deny_cross_tenant"


def test_org_a_returns_document_covers_four_required_headings():
    paths = set(_loaded_paths("org_a", "returns"))
    for heading in ("退货时限", "商品状态", "包装要求", "例外商品"):
        assert any(p and p.endswith("/" + heading) for p in paths), heading


def test_org_a_and_org_b_returns_conflict_on_window():
    # org_a 普通 7 天 vs org_b 普通 30 天 -- isolation fixture.
    paths_a = {
        p for p in _loaded_paths("org_a", "returns") if p
    }
    paths_b = {
        p for p in _loaded_paths("org_b", "returns") if p
    }
    body_a = _loaded_text("org_a", "returns")
    body_b = _loaded_text("org_b", "returns")
    assert "普通会员 7 天" in body_a
    assert "普通会员 30 天" in body_b


def _loaded_text(tenant: str, document_key: str) -> str:
    loader = DocumentLoader()
    path = DOCS_ROOT / tenant / f"{document_key}.md"
    return loader.load(path.read_bytes(), DocumentSourceType.MARKDOWN).text


# --------------------------------------------------------------------------- #
# Metric primitives (hand-built fake results, no network)
# --------------------------------------------------------------------------- #
def _case(
    *,
    case_id="c1",
    category="simple_policy",
    tenant_key="org_a",
    question="q",
    expected_relevant=(("returns", "退货时限"),),
    should_have_answer=True,
    forbidden_tenant_keys=("org_b",),
    expected_behavior=None,
) -> EvalCase:
    return EvalCase(
        case_id=case_id,
        category=category,
        tenant_key=tenant_key,
        question=question,
        expected_relevant=tuple(
            ExpectedRelevant(document_key=k, heading_path=h)
            for k, h in expected_relevant
        ),
        should_have_answer=should_have_answer,
        forbidden_tenant_keys=forbidden_tenant_keys,
        expected_behavior=expected_behavior,
    )


def test_recall_at_5_full_hit():
    case = _case(expected_relevant=(("returns", "退货时限"),))
    assert recall_at_5(case, [("returns", "退货时限")]) == 1.0


def test_recall_at_5_partial_hit_multicondition():
    case = _case(
        expected_relevant=(
            ("returns", "退货时限"),
            ("returns", "商品状态"),
            ("returns", "包装要求"),
        )
    )
    returned = [("returns", "退货时限"), ("returns", "商品状态")]
    assert recall_at_5(case, returned) == pytest.approx(2 / 3)


def test_recall_at_5_miss_is_zero():
    case = _case(expected_relevant=(("returns", "退货时限"),))
    assert recall_at_5(case, [("warranty", "保修期限")]) == 0.0


def test_recall_at_5_none_for_no_expected():
    case = _case(category="safety", expected_relevant=(), should_have_answer=False)
    assert recall_at_5(case, []) is None


# --------------------------------------------------------------------------- #
# Heading path-segment matching rule (Task 14 fix)
# --------------------------------------------------------------------------- #
def test_match_expected_full_path_citation_matches_bare_heading():
    # A citation carrying the FULL heading path (loader semantics) must match a
    # golden expected_relevant carrying the BARE second-level heading.
    pair = ("returns", "云舟商城退货政策（A 版）/退货时限")
    expected = ("returns", "退货时限")
    assert _matches_expected(pair, expected) is True


def test_match_expected_exact_no_slash_still_wins():
    # A citation heading with no slash equals the expected heading directly.
    assert _matches_expected(("returns", "退货时限"), ("returns", "退货时限")) is True


def test_match_expected_wrong_document_same_heading_does_not_match():
    # Same heading text in a different document is NOT a match: the document_key
    # must be equal.
    assert _matches_expected(
        ("warranty", "云舟商城退货政策（A 版）/退货时限"), ("returns", "退货时限")
    ) is False


def test_match_expected_heading_containing_expected_as_substring_does_not_match():
    # A heading that merely CONTAINS the expected text inside a different final
    # segment (e.g. a longer suffix) does NOT match: the final "/"-segment must
    # equal the expected heading, not just share it as a prefix/substring.
    assert _matches_expected(
        ("returns", "云舟商城退货政策（A 版）/退货时限延长期"), ("returns", "退货时限")
    ) is False


def test_match_expected_heading_containing_expected_as_prefix_does_not_match():
    # "退货时限" as a bare heading whose final segment is exactly "退货时限" DOES
    # match via exact equality; but a final segment that starts with it and
    # continues is excluded by the whole-segment rule.
    assert _matches_expected(
        ("returns", "云舟商城退货政策（A 版）/普通退货时限"), ("returns", "退货时限")
    ) is False


def test_recall_at_5_full_path_citation_matches_bare_expected():
    case = _case(expected_relevant=(("returns", "退货时限"),))
    returned = [("returns", "云舟商城退货政策（A 版）/退货时限")]
    assert recall_at_5(case, returned) == 1.0


def test_retrieval_precision_full_path_citation_counts_as_relevant():
    case = _case(expected_relevant=(("returns", "退货时限"),))
    returned = [
        ("returns", "云舟商城退货政策（A 版）/退货时限"),
        ("warranty", "云舟商城保修政策（A 版）/保修期限"),
    ]
    assert retrieval_precision_at_5(case, returned, len(returned)) == pytest.approx(0.5)


def test_recall_at_5_wrong_document_full_path_no_match():
    case = _case(expected_relevant=(("returns", "退货时限"),))
    returned = [("warranty", "云舟商城保修政策（A 版）/退货时限")]
    assert recall_at_5(case, returned) == 0.0


def test_recall_at_5_heading_prefix_suffix_no_match():
    # A final segment that only shares the expected text as a prefix/substring
    # must not count as a hit.
    case = _case(expected_relevant=(("returns", "退货时限"),))
    assert recall_at_5(case, [("returns", "退货时限延长期")]) == 0.0
    assert recall_at_5(case, [("returns", "普通退货时限")]) == 0.0


def test_hit_count_each_expected_pair_counts_at_most_once():
    # Two returned pairs that both match the same expected pair still score one
    # hit for that expected pair.
    expected = {("returns", "退货时限")}
    assert _hit_count(
        [("returns", "云舟商城退货政策（A 版）/退货时限"), ("returns", "退货时限")],
        expected,
    ) == 1


def test_evaluate_case_full_path_citation_hits():
    case = _case(expected_relevant=(("returns", "退货时限"),))
    metrics = evaluate_case(case, {
        "pairs": [("returns", "云舟商城退货政策（A 版）/退货时限")],
        "citations_returned": 1,
    })
    assert metrics.hit_count == 1
    assert metrics.recall_at_5 == 1.0
    assert metrics.retrieval_precision_at_5 == pytest.approx(1.0)


def test_retrieval_precision_positive_with_hits():
    case = _case(expected_relevant=(("returns", "退货时限"),))
    returned = [("returns", "退货时限"), ("warranty", "保修期限"), ("warranty", "所需凭证")]
    assert retrieval_precision_at_5(case, returned, len(returned)) == pytest.approx(1 / 3)


def test_retrieval_precision_empty_positive_is_zero():
    case = _case(expected_relevant=(("returns", "退货时限"),), should_have_answer=True)
    assert retrieval_precision_at_5(case, [], 0) == 0.0


def test_retrieval_precision_no_golden_evidence_is_not_measured():
    case = _case(
        category="safety",
        expected_relevant=(),
        should_have_answer=False,
        expected_behavior="abstain",
    )
    assert retrieval_precision_at_5(case, [("returns", "退货时限")], 1) is None


def test_retrieval_precision_grounded_safety_case_is_measured():
    case = _case(
        category="safety",
        expected_relevant=(("returns", "退货时限"),),
        expected_behavior="answer_grounded",
    )
    returned = [("returns", "退货时限")] + [
        ("warranty", f"无关{i}") for i in range(4)
    ]
    assert retrieval_precision_at_5(case, returned, 5) == pytest.approx(1 / 5)


def test_cross_tenant_leak_false_no_forbidden_returned():
    case = _case(forbidden_tenant_keys=("org_b",))
    assert cross_tenant_leak(case, ["org_a"]) is False


def test_cross_tenant_leak_true_when_forbidden_returned():
    case = _case(forbidden_tenant_keys=("org_b",))
    assert cross_tenant_leak(case, ["org_a", "org_b"]) is True


def test_evaluate_case_positive_empty_retrieval_precision_zero():
    case = _case(expected_relevant=(("returns", "退货时限"),), should_have_answer=True)
    metrics = evaluate_case(case, {"pairs": [], "citations_returned": 0})
    assert metrics.retrieval_precision_at_5 == 0.0
    assert metrics.recall_at_5 == 0.0
    assert metrics.cross_tenant_leak is False
    assert metrics.got_citations is False


def test_evaluate_case_no_golden_evidence_precision_is_none():
    case = _case(category="safety", expected_relevant=(), should_have_answer=False)
    metrics = evaluate_case(case, {"pairs": [], "citations_returned": 0})
    assert metrics.retrieval_precision_at_5 is None
    assert metrics.recall_at_5 is None


def test_p95_nearest_rank_16_cases():
    # ceil(0.95 * 16) - 1 = 15 -> the maximum for 16 ascending values.
    latencies = list(range(1, 17))
    assert percentile_nearest_rank(latencies, 0.95) == 16
    assert percentile_nearest_rank(latencies, 0.50) == 8


def test_p95_nearest_rank_empty_is_zero():
    assert percentile_nearest_rank([], 0.95) == 0.0


# --------------------------------------------------------------------------- #
# Report aggregation (fake metrics)
# --------------------------------------------------------------------------- #
def _metrics(
    *,
    category="simple_policy",
    tenant_key="org_a",
    citations_returned=1,
    expected_count=1,
    hit_count=1,
    relevant_returned=None,
    precision_override=None,
    should_have_answer=True,
    latency_ms=10,
    cost=0.1,
) -> CaseMetrics:
    recall = (
        hit_count / expected_count
        if expected_count
        else None
    )
    if precision_override is not None:
        precision = precision_override
    else:
        precision = 1.0 if (citations_returned == 0 and not should_have_answer) else (
            0.0 if citations_returned == 0 else hit_count / citations_returned
        )
    # Default relevant_returned to the exact integer that yields `precision`.
    if relevant_returned is None:
        relevant_returned = (
            0 if citations_returned == 0 else round(precision * citations_returned)
        )
    return CaseMetrics(
        category=category,
        tenant_key=tenant_key,
        citations_returned=citations_returned,
        expected_count=expected_count,
        hit_count=hit_count,
        recall_at_5=recall,
        retrieval_precision_at_5=(precision if expected_count else None),
        relevant_returned=relevant_returned,
        cross_tenant_leak=False,
        latency_ms=latency_ms,
        rounds=1,
        model_calls=0,
        input_tokens=100,
        cost_cny=cost,
        should_have_answer=should_have_answer,
        got_citations=citations_returned > 0,
    )


def test_compile_report_aggregates():
    cases = [
        _case(case_id="a", category="simple_policy"),
        _case(case_id="b", category="safety", expected_relevant=(), should_have_answer=False),
    ]
    per_case = [
        _metrics(citations_returned=2, expected_count=2, hit_count=1, latency_ms=10),
        _metrics(
            category="safety",
            citations_returned=0,
            expected_count=0,
            hit_count=0,
            should_have_answer=False,
            latency_ms=20,
        ),
    ]
    report = compile_report(
        cases=cases,
        per_case=per_case,
        code_revision="abc123",
        dataset_hash="deadbeef",
        collection_name="col",
        embedding_model="text-embedding-v4",
        embedding_dimensions=1024,
    )
    agg = report["aggregates"]
    # recall = 1/2 weighted over the 2-expected case (safety has none).
    assert agg["retrieval_recall_at_5"] == pytest.approx(0.5)
    # precision: positive case 1/2 returned relevant; over returned counts.
    assert agg["retrieval_precision_at_5"] == pytest.approx(0.5)
    assert agg["citation_precision"] is None
    assert agg["correct_abstention_rate"] is None
    assert agg["citation_precision_scope"] == "stage_b_not_measured"
    assert agg["correct_abstention_rate_scope"] == "stage_b_not_measured"
    assert agg["cross_tenant_leak_rate"] == 0.0
    assert agg["average_tokens"] == pytest.approx(100.0)
    # nearest-rank p50 for 2 ascending latencies [10, 20]: index ceil(.5*2)-1=0 -> 10.
    assert agg["p50_latency_ms"] == 10
    assert agg["p95_latency_ms"] == 20
    assert report["code_revision"] == "abc123"
    assert report["dataset_hash"] == "deadbeef"
    assert report["top_k"] > 0


def test_compile_report_uses_exact_integer_aggregate_not_rounded_reconstruction():
    # I1 regression: the aggregate citation_precision must be the sum of the
    # EXACT per-case relevant_returned integers, never reconstructed from the
    # pre-rounded citation_precision float (which can round-trip to an off-by-one).
    #
    # Each case is built so the OLD lossy reconstruction
    # ``round(citation_precision * citations_returned)`` disagrees with the exact
    # integer the runner actually stores:
    #   * case a: precision=0.5, citations_returned=7 -> old round(0.5*7) = round(3.5)
    #     = 4 (banker's rounding), but the exact relevant integer is 3.
    #   * case b: precision=0.0 (empty/irrelevant), citations_returned=0, exact 0.
    # Asserts the report uses the explicit integers (3 + 0 relevant over
    # 7 + 0 returned), NOT the float reconstruction (which would over-count by 1).
    cases = [
        _case(case_id="a", category="simple_policy"),
        _case(case_id="b", category="simple_policy"),
    ]
    per_case = [
        _metrics(
            citations_returned=7,
            expected_count=3,
            hit_count=3,
            # exact integer relevant count (the ground truth the runner stores)
            relevant_returned=3,
            precision_override=0.5,
        ),
        _metrics(
            citations_returned=0,
            expected_count=0,
            hit_count=0,
            relevant_returned=0,
            precision_override=0.0,
        ),
    ]
    report = compile_report(
        cases=cases,
        per_case=per_case,
        code_revision="abc123",
        dataset_hash="deadbeef",
        collection_name="col",
        embedding_model="text-embedding-v4",
        embedding_dimensions=1024,
    )
    # exact integers: (3 + 0) relevant over (7 + 0) returned.
    assert report["aggregates"]["retrieval_precision_at_5"] == pytest.approx(3 / 7)
    # Sanity: the old reconstruction would have produced round(3.5)=4 -> 4/7,
    # so this regression guard genuinely catches the would-be off-by-one.
    assert round(0.5 * 7) != 3


def test_relevant_returned_count_matches_precision_semantics():
    # The runner's explicit relevant_returned integer must equal what the
    # precision primitive counts, so the aggregate can be derived from exact
    # integers rather than a rounded float.
    case = _case(expected_relevant=(("returns", "退货时限"),))
    pairs = [
        ("returns", "云舟商城退货政策（A 版）/退货时限"),  # relevant
        ("warranty", "云舟商城保修政策（A 版）/保修期限"),  # not relevant
        ("warranty", "云舟商城保修政策（A 版）/所需凭证"),  # not relevant
    ]
    assert relevant_returned_count(case, pairs) == 1
    assert retrieval_precision_at_5(case, pairs, len(pairs)) == pytest.approx(1 / 3)


def test_case_report_emits_content_free_citations_and_retrieval_trace():
    case = _case(
        case_id="diagnostic-case",
        expected_behavior="answer_grounded",
    )
    metrics = evaluate_case(
        case,
        {
            "pairs": [("returns", "云舟商城退货政策（A 版）/退货时限")],
            "citations_returned": 1,
            "returned_citations": [
                {
                    "document_key": "returns",
                    "heading_path": "云舟商城退货政策（A 版）/退货时限",
                    "chunk_id": "chunk-a",
                    "citation_rank": 1,
                }
            ],
            "retrieval_trace": {
                "schema_version": 2,
                "candidates": [
                    {
                        "chunk_id": "chunk-a",
                        "fused_rank": 1,
                        "fused_score": 0.9,
                        "resolution_status": "selected",
                        "selection_reason": "selected",
                    }
                ],
            },
        },
    )

    payload = case_report_dict(case, metrics)

    assert payload["expected_behavior"] == "answer_grounded"
    assert payload["returned_citations"] == [
        {
            "document_key": "returns",
            "heading_path": "云舟商城退货政策（A 版）/退货时限",
            "chunk_id": "chunk-a",
            "citation_rank": 1,
        }
    ]
    assert payload["retrieval_trace"]["candidates"][0]["fused_rank"] == 1

    def all_keys(value):
        if isinstance(value, dict):
            return set(value) | set().union(
                *(all_keys(item) for item in value.values())
            )
        if isinstance(value, list):
            return set().union(*(all_keys(item) for item in value), set())
        return set()

    assert "content" not in all_keys(payload)


def test_historical_fused_candidate_replay_recalls_every_golden_section():
    import json

    fixture = json.loads(
        HISTORICAL_CANDIDATES_PATH.read_text(encoding="utf-8")
    )
    cases = {case.case_id: case for case in load_eval_cases(CASES_PATH)}
    total_hits = 0
    total_expected = 0

    for replay in fixture["cases"]:
        case = cases[replay["case_id"]]
        resolved = []
        candidate_by_id = {}
        document_key_by_chunk = {}
        for item in replay["candidates"]:
            candidate_by_id[item["chunk_id"]] = VectorCandidate(
                chunk_id=item["chunk_id"],
                document_id=item["document_key"],
                version_id="historical-version",
                ordinal=item["ordinal"],
                score=item["fused_score"],
            )
            resolved.append(
                ChunkWithDocumentTitle(
                    chunk_id=item["chunk_id"],
                    organization_id="historical-org-a",
                    document_id=item["document_key"],
                    version_id="historical-version",
                    ordinal=item["ordinal"],
                    heading_path=item["heading_path"],
                    content="",
                    token_count=item["token_count"],
                    document_title=item["document_key"],
                )
            )
            document_key_by_chunk[item["chunk_id"]] = item["document_key"]

        ranked = BaselineKnowledgeSearchService._rank_unique(
            resolved, candidate_by_id
        )
        selected, _ = BaselineKnowledgeSearchService._select_within_budget(
            ranked
        )
        returned_pairs = [
            (document_key_by_chunk[chunk.chunk_id], chunk.heading_path or "")
            for chunk in selected
        ]
        expected = {
            (item.document_key, item.heading_path)
            for item in case.expected_relevant
        }
        total_hits += _hit_count(returned_pairs, expected)
        total_expected += len(expected)

    assert set(cases) == {item["case_id"] for item in fixture["cases"]}
    assert total_expected == 20
    assert total_hits == 20
    assert total_hits / total_expected == 1.0
