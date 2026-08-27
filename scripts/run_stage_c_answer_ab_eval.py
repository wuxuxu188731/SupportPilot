"""运行 Stage C 最终答案 Baseline/Adaptive A/B 对照。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from uuid import uuid4


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

def write_report_atomic(path: str | Path, report: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(report, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, target)


def build_parser() -> argparse.ArgumentParser:
    from app.core.config import MODEL_NAME

    parser = argparse.ArgumentParser(description="运行 Stage C 最终答案 A/B 评测")
    parser.add_argument("--database", type=Path, default=Path(".artifacts/stage-c-answer-ab/state.db"))
    parser.add_argument("--cases", type=Path, default=Path("evals/knowledge/stage_c/cases.jsonl"))
    parser.add_argument("--checkpoint", type=Path, default=Path(".artifacts/stage-c-answer-ab/checkpoint.json"))
    parser.add_argument("--output", type=Path, default=Path(".artifacts/stage-c-answer-ab/report.json"))
    parser.add_argument("--model", default=MODEL_NAME)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    from app.evals.stage_c.answer_ab import run_stage_c_answer_ab_eval

    try:
        report = run_stage_c_answer_ab_eval(
            database_path=arguments.database,
            cases_path=arguments.cases,
            checkpoint_path=arguments.checkpoint,
            model_name=arguments.model,
            repo_root=REPO_ROOT,
        )
        write_report_atomic(arguments.output, report)
    except Exception as error:  # 仅输出稳定类型，避免供应商响应或密钥泄漏
        print(f"Stage C answer A/B failed: {type(error).__name__}", file=sys.stderr)
        return 1
    completion = report["completion"]
    print(
        f"completed answers: {completion['completed_answers']}/{completion['expected_answers']}; "
        f"completed pairs: {completion['completed_pairs']}/{completion['case_count']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
