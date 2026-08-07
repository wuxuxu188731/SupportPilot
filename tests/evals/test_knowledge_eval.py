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

from app.knowledge.base import DocumentSourceType
from app.knowledge.document_loader import DocumentLoader
from scripts.run_knowledge_baseline_eval import (
    CaseMetrics,
    EvalCase,
    ExpectedRelevant,
    citation_precision,
    compile_report,
    cross_tenant_leak,
    evaluate_case,
    load_eval_cases,
    percentile_nearest_rank,
    recall_at_5,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CASES_PATH = REPO_ROOT / "evals" / "knowledge" / "cases.jsonl"
SCHEMA_PATH = REPO_ROOT / "evals" / "knowledge" / "schema.json"
DOCS_ROOT = REPO_ROOT / "evals" / "knowledge" / "documents"

EXPECTED_CATEGORIES = {
    "simple_policy",
    "multi_condition_policy",
    "mixed_fact_policy",
    "safety_no_answer",
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


def test_safety_cases_declare_no_answer_and_no_expected_relevant():
    cases = load_eval_cases(CASES_PATH)
    safety = [c for c in cases if c.category == "safety_no_answer"]
    assert len(safety) == EXPECTED_PER_CATEGORY
    for case in safety:
        assert case.expected_relevant == ()
        assert case.should_have_answer is False


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
        assert case.category == "safety_no_answer"
        assert case.should_have_answer is False
        assert case.expected_relevant == ()


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
    case = _case(category="safety_no_answer", expected_relevant=(), should_have_answer=False)
    assert recall_at_5(case, []) is None


def test_citation_precision_positive_with_hits():
    case = _case(expected_relevant=(("returns", "退货时限"),))
    returned = [("returns", "退货时限"), ("warranty", "保修期限"), ("warranty", "所需凭证")]
    assert citation_precision(case, returned, len(returned)) == pytest.approx(1 / 3)


def test_citation_precision_empty_positive_is_zero():
    case = _case(expected_relevant=(("returns", "退货时限"),), should_have_answer=True)
    assert citation_precision(case, [], 0) == 0.0


def test_citation_precision_empty_no_answer_is_one():
    case = _case(category="safety_no_answer", expected_relevant=(), should_have_answer=False)
    assert citation_precision(case, [], 0) == 1.0


def test_cross_tenant_leak_false_no_forbidden_returned():
    case = _case(forbidden_tenant_keys=("org_b",))
    assert cross_tenant_leak(case, ["org_a"]) is False


def test_cross_tenant_leak_true_when_forbidden_returned():
    case = _case(forbidden_tenant_keys=("org_b",))
    assert cross_tenant_leak(case, ["org_a", "org_b"]) is True


def test_evaluate_case_positive_empty_precision_zero():
    case = _case(expected_relevant=(("returns", "退货时限"),), should_have_answer=True)
    metrics = evaluate_case(case, {"pairs": [], "citations_returned": 0})
    assert metrics.citation_precision == 0.0
    assert metrics.recall_at_5 == 0.0
    assert metrics.cross_tenant_leak is False
    assert metrics.got_citations is False


def test_evaluate_case_no_answer_empty_precision_one():
    case = _case(category="safety_no_answer", expected_relevant=(), should_have_answer=False)
    metrics = evaluate_case(case, {"pairs": [], "citations_returned": 0})
    assert metrics.citation_precision == 1.0
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
    should_have_answer=True,
    latency_ms=10,
    cost=0.1,
) -> CaseMetrics:
    recall = (
        hit_count / expected_count
        if expected_count
        else None
    )
    precision = 1.0 if (citations_returned == 0 and not should_have_answer) else (
        0.0 if citations_returned == 0 else hit_count / citations_returned
    )
    return CaseMetrics(
        category=category,
        tenant_key=tenant_key,
        citations_returned=citations_returned,
        expected_count=expected_count,
        hit_count=hit_count,
        recall_at_5=recall,
        citation_precision=precision,
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
        _case(case_id="b", category="safety_no_answer", expected_relevant=(), should_have_answer=False),
    ]
    per_case = [
        _metrics(citations_returned=2, expected_count=2, hit_count=1, latency_ms=10),
        _metrics(
            category="safety_no_answer",
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
    assert agg["citation_precision"] == pytest.approx(0.5)
    assert agg["cross_tenant_leak_rate"] == 0.0
    assert agg["average_tokens"] == pytest.approx(100.0)
    # nearest-rank p50 for 2 ascending latencies [10, 20]: index ceil(.5*2)-1=0 -> 10.
    assert agg["p50_latency_ms"] == 10
    assert agg["p95_latency_ms"] == 20
    assert report["code_revision"] == "abc123"
    assert report["dataset_hash"] == "deadbeef"
    assert report["top_k"] > 0
