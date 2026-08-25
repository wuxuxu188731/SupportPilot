"""Thin command line entry point for the resumable Stage C retrieval eval."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.evals.stage_c.runner import (
    StageCInfrastructureFailure,
    classify_infrastructure_failure,
    run_stage_c_retrieval_eval,
)


_FAILURE_MESSAGES = {
    "dashscope_balance": "DashScope embedding balance is insufficient.",
    "deepseek_balance": "DeepSeek balance is insufficient.",
    "deepseek_provider_failure": "DeepSeek provider failure.",
    "qdrant_503": "Qdrant unavailable (503). Stop VPN/proxy interference and retry.",
    "generic": "Stage C infrastructure failure.",
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_stage_c_retrieval_eval",
        description="Run or resume the Stage C Baseline/Adaptive retrieval comparison.",
    )
    parser.add_argument(
        "--database",
        default=".artifacts/stage-c-retrieval/state.db",
        help="SQLite fixture state path.",
    )
    parser.add_argument(
        "--cases",
        default="evals/knowledge/stage_c/cases.jsonl",
        help="Stage C cases JSONL path.",
    )
    parser.add_argument(
        "--checkpoint",
        default=".artifacts/stage-c-retrieval/checkpoint.json",
        help="Resumable checkpoint JSON path.",
    )
    parser.add_argument(
        "--output",
        default=".artifacts/stage-c-retrieval/report.json",
        help="Final or incomplete report JSON path.",
    )
    return parser


def write_report_atomic(path: str | Path, report: Mapping[str, object]) -> None:
    """Write a report through a flushed same-directory temporary file."""

    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(report, temporary, ensure_ascii=False, indent=2)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, output)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _resume_command(args: argparse.Namespace) -> str:
    values = (
        ("--database", args.database),
        ("--cases", args.cases),
        ("--checkpoint", args.checkpoint),
        ("--output", args.output),
    )
    parts = ["python", "scripts/run_stage_c_retrieval_eval.py"]
    for option, value in values:
        parts.extend((option, f'"{value}"'))
    return " ".join(parts)


def _print_failure(classification: str, args: argparse.Namespace) -> None:
    message = _FAILURE_MESSAGES.get(classification, _FAILURE_MESSAGES["generic"])
    print(f"Stage C stopped: {message}", file=sys.stderr)
    print(f"Resume: {_resume_command(args)}", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    database = Path(args.database)
    cases_path = Path(args.cases)
    checkpoint = Path(args.checkpoint)
    output = Path(args.output)
    database.parent.mkdir(parents=True, exist_ok=True)
    checkpoint.parent.mkdir(parents=True, exist_ok=True)

    try:
        report = run_stage_c_retrieval_eval(
            database_path=database,
            cases_path=cases_path,
            checkpoint_path=checkpoint,
        )
    except StageCInfrastructureFailure as failure:
        write_report_atomic(output, failure.report)
        _print_failure(failure.classification, args)
        return 1
    except Exception as error:  # noqa: BLE001 - never print provider detail
        output.unlink(missing_ok=True)
        _print_failure(classify_infrastructure_failure(error), args)
        return 1

    write_report_atomic(output, report)
    print(f"wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
