"""CLI tests for atomic and sanitized Stage C report handoff."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.evals.stage_c.runner import StageCInfrastructureFailure
from scripts import run_stage_c_retrieval_eval as cli


REPO_ROOT = Path(__file__).resolve().parents[3]


def test_documented_source_tree_entrypoint_can_import_app():
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)

    completed = subprocess.run(
        [sys.executable, "scripts/run_stage_c_retrieval_eval.py", "--help"],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=90,
    )

    assert completed.returncode == 0, completed.stderr
    assert "--checkpoint" in completed.stdout


def test_cli_defaults_delegate_only_the_four_paths(monkeypatch):
    captured = {}

    def run(**kwargs):
        captured.update(kwargs)
        return {"completion": {"completed_variants": 96}}

    monkeypatch.setattr(cli, "run_stage_c_retrieval_eval", run)
    monkeypatch.setattr(cli, "write_report_atomic", lambda path, report: captured.update(output=path, report=report))

    assert cli.main([]) == 0
    assert captured["database_path"] == Path(".artifacts/stage-c-retrieval/state.db")
    assert captured["cases_path"] == Path("evals/knowledge/stage_c/cases.jsonl")
    assert captured["checkpoint_path"] == Path(".artifacts/stage-c-retrieval/checkpoint.json")
    assert captured["output"] == Path(".artifacts/stage-c-retrieval/report.json")


def test_cli_passes_explicit_arguments(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(
        cli,
        "run_stage_c_retrieval_eval",
        lambda **kwargs: captured.update(kwargs) or {"completion": {}},
    )
    monkeypatch.setattr(cli, "write_report_atomic", lambda path, report: captured.update(output=path))
    values = [tmp_path / name for name in ("state.db", "cases.jsonl", "checkpoint.json", "report.json")]

    assert cli.main([
        "--database", str(values[0]),
        "--cases", str(values[1]),
        "--checkpoint", str(values[2]),
        "--output", str(values[3]),
    ]) == 0
    assert captured["database_path"] == values[0]
    assert captured["cases_path"] == values[1]
    assert captured["checkpoint_path"] == values[2]
    assert captured["output"] == values[3]


def test_report_write_is_same_directory_flush_fsync_replace(tmp_path, monkeypatch):
    output = tmp_path / "nested" / "report.json"
    replacements = []
    fsync_calls = []
    real_replace = os.replace

    def replace(source, target):
        replacements.append((Path(source), Path(target)))
        real_replace(source, target)

    monkeypatch.setattr(cli.os, "replace", replace)
    monkeypatch.setattr(cli.os, "fsync", lambda fd: fsync_calls.append(fd))

    cli.write_report_atomic(output, {"safe": "artifact"})

    assert json.loads(output.read_text(encoding="utf-8")) == {"safe": "artifact"}
    assert fsync_calls
    assert replacements[0][0].parent == output.parent
    assert replacements[0][1] == output


@pytest.mark.parametrize(
    ("classification", "expected"),
    [
        ("dashscope_balance", "DashScope embedding balance"),
        ("deepseek_balance", "DeepSeek balance"),
        ("deepseek_provider_failure", "DeepSeek provider"),
        ("qdrant_503", "Qdrant unavailable (503)"),
        ("generic", "infrastructure failure"),
    ],
)
def test_cli_writes_incomplete_report_and_sanitized_nonzero_handoff(
    classification, expected, monkeypatch, capsys
):
    report = {
        "completion": {
            "completed_variants": 1,
            "infrastructure_failed_variants": 1,
        }
    }
    failure = StageCInfrastructureFailure(
        case_id="case-1",
        variant="adaptive",
        classification=classification,
        report=report,
    )
    written = {}

    def run(**kwargs):
        raise failure from RuntimeError(
            "SECRET_PROVIDER_BODY sk-live-key insufficient_balance 503"
        )

    monkeypatch.setattr(cli, "run_stage_c_retrieval_eval", run)
    monkeypatch.setattr(cli, "write_report_atomic", lambda path, value: written.update(path=path, report=value))

    assert cli.main([]) == 1
    stderr = capsys.readouterr().err
    assert expected in stderr
    assert "python scripts/run_stage_c_retrieval_eval.py" in stderr
    assert "SECRET_PROVIDER_BODY" not in stderr
    assert "sk-live-key" not in stderr
    assert written["report"] == report
    assert "SECRET_PROVIDER_BODY" not in json.dumps(written["report"])


def test_cli_generic_failure_removes_stale_success_report(tmp_path, monkeypatch, capsys):
    output = tmp_path / "report.json"
    output.write_text('{"completion":{"completed_variants":96}}', encoding="utf-8")

    def fail(**kwargs):
        raise RuntimeError("SECRET corrupt checkpoint")

    monkeypatch.setattr(cli, "run_stage_c_retrieval_eval", fail)

    assert cli.main(["--output", str(output)]) == 1
    assert not output.exists()
    assert "SECRET" not in capsys.readouterr().err
