"""保护格式回归的真实判分和独立重跑，全部使用离线数据。"""

from types import SimpleNamespace

import pytest

from scripts import run_document_format_regression as regression


def make_case():
    """构造包含总则与辅助文档的两组证据。"""
    return SimpleNamespace(
        case_id="case", question="问题",
        required_evidence_groups=[
            SimpleNamespace(group_id="general", any_of=[SimpleNamespace(
                document_key="general_service", heading_path="总则/时限")]),
            SimpleNamespace(group_id="returns", any_of=[SimpleNamespace(
                document_key="returns_exchange", heading_path="退货/时限")]),
        ],
    )


# 保护行为：缺失的辅助证据不能自动命中，同名标题也不能冒充正确文档。
def test_scoring_requires_actual_document_and_heading():
    citation = SimpleNamespace(document_id="g", heading_path="总则/时限")
    result = regression.score_case(make_case(), [citation], {"g": "general_service"})
    assert result.group_hits == {"general": True, "returns": False}
    result = regression.score_case(make_case(), [citation], {"g": "returns_exchange"})
    assert result.hit_count == 0


# 边界情况：服务错误和空引用必须让回归失败，不能输出三格式等价的假成功。
@pytest.mark.parametrize("ok", [False, True])
def test_search_failure_or_empty_evidence_cannot_pass(ok):
    result = SimpleNamespace(ok=ok, citations=[], error=SimpleNamespace(code="EMBEDDING_UNAVAILABLE"))
    with pytest.raises(RuntimeError):
        regression.score_search_result(make_case(), result, {})


# 保护行为：命中数量相同但命中不同证据时，不得认定格式等价或标题全部命中。
def test_summary_compares_actual_groups_and_boolean_values():
    outcomes = {
        "markdown": [regression.CaseOutcome("case", ["总则/时限"], {"general": True, "returns": False})],
        "pdf": [regression.CaseOutcome("case", ["退货/时限"], {"general": False, "returns": True})],
    }
    settings = SimpleNamespace(embedding_model="fake", embedding_dimensions=3,
                               qdrant_collection="test", rerank_model="fake")
    report = regression._compile_report([make_case()], outcomes, settings, list(outcomes))
    assert report["summary"]["all_hit_counts_consistent"] is True
    assert report["summary"]["all_results_consistent"] is False
    assert report["summary"]["all_expected_headings_matched"] is False
    assert report["summary"]["comparison_complete"] is False


# 保护行为：独立重跑生成新的数据库、报告和集合，旧文件保持原样。
def test_fresh_run_preserves_existing_state(tmp_path, monkeypatch):
    database = tmp_path / "state.db"
    database.write_bytes(b"old database")
    output = tmp_path / "report.json"
    output.write_text("old report", encoding="utf-8")
    captured = {}

    async def fake_run(**kwargs):
        captured.update(kwargs)
        return {"summary": {"all_results_consistent": True}}

    monkeypatch.setattr(regression, "run_regression", fake_run)
    monkeypatch.setattr(regression, "render_markdown", lambda report: "测试报告")
    assert regression.main(["--fresh", "--database", str(database), "--output", str(output)]) == 0
    assert captured["database_path"] != database
    assert captured["database_path"].parent.parent == tmp_path
    assert captured["collection_name"].startswith("supportpilot_format_")
    assert database.read_bytes() == b"old database"
    assert output.read_text(encoding="utf-8") == "old report"
    assert (captured["database_path"].parent / "report.json").exists()
