"""Contract tests for strict Stage C eval case loading."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from app.evals.stage_c.models import (
    ExpectedBehavior,
    StageCCaseLoadError,
    StageCCategory,
    StageCVariant,
    load_stage_c_cases,
)


REPO_ROOT = Path(__file__).resolve().parents[3]
CASES_PATH = REPO_ROOT / "evals" / "knowledge" / "stage_c" / "cases.jsonl"


def _first_case() -> dict:
    return json.loads(CASES_PATH.read_text(encoding="utf-8").splitlines()[0])


def _write_cases(path: Path, *records: dict) -> None:
    path.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
        encoding="utf-8",
    )


def test_loads_all_stage_c_cases_strictly():
    cases = load_stage_c_cases(CASES_PATH)

    assert len(cases) == 48
    assert {case.category for case in cases} == set(StageCCategory)
    assert cases[0].case_id == "simple-general-service-response-01"


def test_stage_c_variants_are_baseline_and_adaptive():
    assert set(StageCVariant) == {
        StageCVariant.BASELINE,
        StageCVariant.ADAPTIVE,
    }


def test_unknown_top_level_field_reports_jsonl_line(tmp_path):
    raw = _first_case()
    raw["organization_id"] = "model-controlled"
    path = tmp_path / "cases.jsonl"
    _write_cases(path, raw)

    with pytest.raises(StageCCaseLoadError, match="line 1"):
        load_stage_c_cases(path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("category", "unknown_category"),
        ("scenario", "unknown_scenario"),
        ("expected_behavior", "unknown_behavior"),
    ],
)
def test_rejects_unknown_enum_values_with_jsonl_line(tmp_path, field, value):
    raw = _first_case()
    raw[field] = value
    path = tmp_path / "cases.jsonl"
    _write_cases(path, raw)

    with pytest.raises(StageCCaseLoadError, match="line 1"):
        load_stage_c_cases(path)


def test_rejects_duplicate_case_id_with_second_jsonl_line(tmp_path):
    raw = _first_case()
    duplicate = deepcopy(raw)
    duplicate["question"] = "A distinct question cannot reuse an id."
    path = tmp_path / "cases.jsonl"
    _write_cases(path, raw, duplicate)

    with pytest.raises(StageCCaseLoadError, match="line 2"):
        load_stage_c_cases(path)


@pytest.mark.parametrize(
    ("field", "duplicate"),
    [
        ("key_answer_facts", {"fact_id": "review_response_deadline", "statement": "duplicate"}),
        (
            "required_evidence_groups",
            {
                "group_id": "review_response",
                "supports_fact_ids": ["review_response_deadline"],
                "any_of": [
                    {
                        "document_key": "general_service",
                        "heading_path": "policy/duplicate",
                    }
                ],
            },
        ),
    ],
)
def test_rejects_duplicate_nested_ids(tmp_path, field, duplicate):
    raw = _first_case()
    raw[field].append(duplicate)
    path = tmp_path / "cases.jsonl"
    _write_cases(path, raw)

    with pytest.raises(StageCCaseLoadError, match="line 1"):
        load_stage_c_cases(path)


def test_rejects_evidence_group_with_dangling_fact_id(tmp_path):
    raw = _first_case()
    raw["required_evidence_groups"][0]["supports_fact_ids"] = ["not-a-fact"]
    path = tmp_path / "cases.jsonl"
    _write_cases(path, raw)

    with pytest.raises(StageCCaseLoadError, match="line 1"):
        load_stage_c_cases(path)


def test_rejects_preferred_strategy_outside_allowed_strategies(tmp_path):
    raw = _first_case()
    raw["strategy_expectation"]["allowed"] = ["multi"]
    path = tmp_path / "cases.jsonl"
    _write_cases(path, raw)

    with pytest.raises(StageCCaseLoadError, match="line 1"):
        load_stage_c_cases(path)


def test_rejects_business_context_on_non_mixed_case(tmp_path):
    raw = _first_case()
    raw["business_context"] = {
        "as_of_date": "2026-08-20T12:00:00+08:00",
        "required_tools": ["get_order"],
        "order": {"order_id": "ORD-test"},
        "logistics": None,
    }
    path = tmp_path / "cases.jsonl"
    _write_cases(path, raw)

    with pytest.raises(StageCCaseLoadError, match="line 1"):
        load_stage_c_cases(path)


def test_rejects_missing_business_context_on_mixed_case(tmp_path):
    raw = next(
        json.loads(line)
        for line in CASES_PATH.read_text(encoding="utf-8").splitlines()
        if '"category": "mixed_fact_policy"' in line
    )
    raw["business_context"] = None
    path = tmp_path / "cases.jsonl"
    _write_cases(path, raw)

    with pytest.raises(StageCCaseLoadError, match="line 1"):
        load_stage_c_cases(path)


def test_rejects_grounded_answer_without_evidence(tmp_path):
    raw = _first_case()
    raw["expected_behavior"] = ExpectedBehavior.ANSWER_GROUNDED.value
    raw["required_evidence_groups"] = []
    path = tmp_path / "cases.jsonl"
    _write_cases(path, raw)

    with pytest.raises(StageCCaseLoadError, match="line 1"):
        load_stage_c_cases(path)
