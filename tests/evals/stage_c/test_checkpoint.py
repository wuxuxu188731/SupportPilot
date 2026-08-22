"""Behavior tests for Stage C's durable, fingerprinted checkpoint."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from app.evals.stage_c.checkpoint import (
    CheckpointResult,
    CheckpointStore,
    FixtureManifestState,
    RunMetadata,
    build_run_fingerprint,
    sha256_corpus,
    sha256_file,
)
from app.evals.stage_c.models import StageCVariant


def metadata(git_revision: str, *, run_id: str | None = None) -> RunMetadata:
    return RunMetadata(
        git_revision=git_revision,
        case_dataset_sha256="a" * 64,
        corpus_sha256="b" * 64,
        loader_version="loader-v1",
        chunker_version="chunker-v1",
        embedding_model="embed-v1",
        embedding_dimensions=1024,
        collection_name="stage-c",
        trace_schema_version="trace-v1",
        planner_model="planner-v1",
        planner_prompt_version="planner-prompt-v1",
        assessor_model="assessor-v1",
        assessor_prompt_version="assessor-prompt-v1",
        top_k=5,
        prefetch_limit=20,
        token_budget=1200,
        score_threshold=0.42,
        timeout_seconds=30.0,
        runner_schema_version="stage-c-runner-v1",
        adaptive_top_k=5,
        max_rounds=2,
        max_round_queries=(2, 1),
        max_planner_calls=1,
        max_assessor_calls=1,
        max_model_calls=2,
        run_id=run_id,
        created_at="2026-08-21T00:00:00Z",
    )


def normalized_payload(
    case_id: str,
    variant: StageCVariant,
    *,
    status: str,
    attempt: int,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "case_id": case_id,
        "variant": variant.value,
        "status": status,
        "attempt": attempt,
        "evidence_status": None,
        "round_count": 0,
        "query_count_by_round": [],
        "model_calls": 0,
        "tokens": 0,
        "latency_ms": 0.0,
        "evaluated_citation_count": 0,
        "full_citation_count": 0,
        "citations": [],
        "candidate_trace": [],
        "metrics": {} if status == "completed" else None,
        "safety_flags": {},
    }
    if status == "infrastructure_failed":
        payload["error_code"] = "provider_timeout"
    return payload


def completed(case_id: str, variant: StageCVariant, *, attempt: int) -> CheckpointResult:
    return CheckpointResult(
        case_id=case_id,
        variant=variant,
        status="completed",
        attempt=attempt,
        payload=normalized_payload(
            case_id, variant, status="completed", attempt=attempt
        ),
    )


def infrastructure_failed(
    case_id: str, variant: StageCVariant, *, attempt: int
) -> CheckpointResult:
    return CheckpointResult(
        case_id=case_id,
        variant=variant,
        status="infrastructure_failed",
        attempt=attempt,
        payload=normalized_payload(
            case_id, variant, status="infrastructure_failed", attempt=attempt
        ),
    )


def test_success_is_reused_but_failed_variant_is_retried(tmp_path: Path) -> None:
    """Treating a failed attempt as complete would skip a required retry."""
    store = CheckpointStore(tmp_path / "checkpoint.json")
    state = store.initialize(metadata("fingerprint-a"))
    store.record(completed("case-1", StageCVariant.BASELINE, attempt=1))
    store.record(infrastructure_failed("case-1", StageCVariant.ADAPTIVE, attempt=1))

    resumed = store.load(metadata("fingerprint-a"))

    assert state.fingerprint == resumed.fingerprint
    assert resumed.should_run("case-1", StageCVariant.BASELINE) is False
    assert resumed.should_run("case-1", StageCVariant.ADAPTIVE) is True
    assert resumed.next_attempt("case-1", StageCVariant.ADAPTIVE) == 2


def test_fingerprint_change_invalidates_old_success(tmp_path: Path) -> None:
    """Reusing results after evaluated configuration drift would be misleading."""
    store = CheckpointStore(tmp_path / "checkpoint.json")
    old = metadata("old")
    store.initialize(old)
    store.record(completed("case-1", StageCVariant.BASELINE, attempt=1))

    state = store.initialize(metadata("new"))

    assert state.should_run("case-1", StageCVariant.BASELINE) is True
    assert state.fixture_manifest is None
    assert state.invalidated_previous_fingerprint == old.fingerprint


def test_run_id_and_created_at_do_not_change_metadata_fingerprint() -> None:
    """Volatile run bookkeeping must not invalidate equivalent evaluations."""
    first = metadata("same", run_id="run-a")
    second = metadata("same", run_id="run-b")

    assert first.fingerprint == second.fingerprint
    assert first.to_dict()["run_id"] == "run-a"


def test_build_fingerprint_uses_canonical_json_key_order() -> None:
    """A mapping insertion-order difference must not spuriously fork a run."""
    expected = hashlib.sha256(b'{"a":{"x":1},"b":2}').hexdigest()

    assert build_run_fingerprint({"b": 2, "a": {"x": 1}}) == expected


def test_corpus_hash_binds_each_file_digest_to_its_stable_path(tmp_path: Path) -> None:
    """Sorting bare digests would miss content swapped between two corpus files."""
    first = tmp_path / "tenant-a" / "returns.md"
    second = tmp_path / "tenant-b" / "returns.md"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_text("org-a policy", encoding="utf-8")
    second.write_text("org-b policy", encoding="utf-8")
    original = sha256_corpus((first, second))

    first.write_text("org-b policy", encoding="utf-8")
    second.write_text("org-a policy", encoding="utf-8")

    assert sha256_corpus((first, second)) != original


def _fixture_manifest_document() -> dict[str, object]:
    return {
        "tenant_key": "org_a",
        "document_key": "returns_exchange",
        "title": "Returns",
        "document_id": "document-1",
        "active_version_id": "version-2",
        "old_version_id": "version-1",
    }


@pytest.mark.parametrize(
    "document",
    [
        {
            "tenant_key": "org_a",
            "document_key": "returns_exchange",
            "title": "Returns",
            "document_id": "document-1",
            "active_version_id": "version-2",
        },
        {
            **_fixture_manifest_document(),
            "untrusted_extra": "must-not-be-persisted",
        },
    ],
)
def test_fixture_manifest_state_rejects_damaged_document_schema(
    document: dict[str, object],
) -> None:
    """A damaged manifest must not be reused merely because it is JSON-shaped."""
    with pytest.raises((TypeError, ValueError)):
        FixtureManifestState({"schema_version": 1, "documents": [document]})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("adaptive_top_k", 6),
        ("max_rounds", 3),
        ("max_round_queries", (2, 2)),
        ("max_planner_calls", 2),
        ("max_assessor_calls", 2),
        ("max_model_calls", 4),
    ],
)
def test_adaptive_hard_limits_change_run_fingerprint(
    field: str, value: object
) -> None:
    """Adaptive guardrail drift must invalidate completed checkpoint results."""
    raw = metadata("adaptive-limits").to_dict()
    raw.update(
        {
            "adaptive_top_k": 5,
            "max_rounds": 2,
            "max_round_queries": (2, 1),
            "max_planner_calls": 1,
            "max_assessor_calls": 1,
            "max_model_calls": 2,
        }
    )
    baseline = RunMetadata(**raw)

    assert replace(baseline, **{field: value}).fingerprint != baseline.fingerprint


@pytest.mark.parametrize(
    "sensitive_field",
    ["api_key", "raw_document", "reasoning", "unexpected"],
)
def test_checkpoint_result_rejects_sensitive_or_unknown_payload_fields(
    sensitive_field: str,
) -> None:
    """The checkpoint boundary must not persist credentials, corpus text, or CoT."""
    with pytest.raises((TypeError, ValueError)):
        CheckpointResult(
            case_id="case-1",
            variant=StageCVariant.BASELINE,
            status="completed",
            attempt=1,
            payload={sensitive_field: "must-not-be-persisted"},
        )


def test_checkpoint_result_rejects_empty_or_truncated_completed_payload() -> None:
    """A status label alone must never make a truncated success reusable."""
    with pytest.raises(ValueError, match="missing"):
        CheckpointResult(
            case_id="case-1",
            variant=StageCVariant.BASELINE,
            status="completed",
            attempt=1,
            payload={},
        )

    truncated = normalized_payload(
        "case-1", StageCVariant.BASELINE, status="completed", attempt=1
    )
    del truncated["citations"]
    with pytest.raises(ValueError, match="citations"):
        CheckpointResult(
            case_id="case-1",
            variant=StageCVariant.BASELINE,
            status="completed",
            attempt=1,
            payload=truncated,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("case_id", "other-case"),
        ("variant", "adaptive"),
        ("status", "infrastructure_failed"),
        ("attempt", 2),
        ("attempt", True),
    ],
)
def test_checkpoint_payload_identity_must_match_outer_result(
    field: str, value: object
) -> None:
    """Duplicated payload identity cannot disagree with its checkpoint envelope."""
    payload = normalized_payload(
        "case-1", StageCVariant.BASELINE, status="completed", attempt=1
    )
    payload[field] = value
    with pytest.raises(ValueError, match=field):
        CheckpointResult(
            case_id="case-1",
            variant=StageCVariant.BASELINE,
            status="completed",
            attempt=1,
            payload=payload,
        )


def test_checkpoint_payload_enforces_status_dependent_metrics_and_error() -> None:
    """Completed and infrastructure outcomes have disjoint measurement shapes."""
    completed_without_metrics = normalized_payload(
        "case-1", StageCVariant.BASELINE, status="completed", attempt=1
    )
    completed_without_metrics["metrics"] = None
    with pytest.raises(TypeError, match="metrics"):
        CheckpointResult(
            case_id="case-1",
            variant=StageCVariant.BASELINE,
            status="completed",
            attempt=1,
            payload=completed_without_metrics,
        )

    failed_with_metrics = normalized_payload(
        "case-1",
        StageCVariant.BASELINE,
        status="infrastructure_failed",
        attempt=1,
    )
    failed_with_metrics["metrics"] = {}
    with pytest.raises(ValueError, match="metrics"):
        CheckpointResult(
            case_id="case-1",
            variant=StageCVariant.BASELINE,
            status="infrastructure_failed",
            attempt=1,
            payload=failed_with_metrics,
        )

    failed_without_error = normalized_payload(
        "case-1",
        StageCVariant.BASELINE,
        status="infrastructure_failed",
        attempt=1,
    )
    del failed_without_error["error_code"]
    with pytest.raises(ValueError, match="error_code"):
        CheckpointResult(
            case_id="case-1",
            variant=StageCVariant.BASELINE,
            status="infrastructure_failed",
            attempt=1,
            payload=failed_without_error,
        )


def test_checkpoint_payload_accepts_normalized_identity_artifacts() -> None:
    """Strict payload validation retains stable evidence identities for scoring."""
    result = CheckpointResult(
        case_id="case-1",
        variant=StageCVariant.ADAPTIVE,
        status="completed",
        attempt=1,
        payload={
            "case_id": "case-1",
            "variant": "adaptive",
            "status": "completed",
            "attempt": 1,
            "strategy": "multi",
            "evidence_status": "complete",
            "round_count": 2,
            "query_count_by_round": [2, 1],
            "model_calls": 2,
            "tokens": 30,
            "latency_ms": 123.4,
            "evaluated_citation_count": 1,
            "full_citation_count": 1,
            "citations": [
                {
                    "citation_id": "C1",
                    "tenant_key": "org_a",
                    "document_key": "returns_exchange",
                    "document_id": "document-1",
                    "version_id": "version-2",
                    "chunk_id": "chunk-1",
                    "heading_path": "Returns/Window",
                    "rank": 1,
                    "score": 0.9,
                }
            ],
            "candidate_trace": [
                {
                    "round": 1,
                    "query_index": 1,
                    "rank": 1,
                    "score": 0.9,
                    "citation_id": "C1",
                    "tenant_key": "org_a",
                    "document_id": "document-1",
                    "version_id": "version-2",
                    "chunk_id": "chunk-1",
                }
            ],
            "metrics": {
                "covered_group_count": 1,
                "required_group_count": 1,
                "evidence_group_recall": 1.0,
                "retrieval_precision": 1.0,
            },
            "safety_flags": {
                "cross_tenant_leak": False,
                "disabled_document_leak": False,
                "inactive_version_leak": False,
            },
        },
    )

    assert result.payload["citations"][0]["document_id"] == "document-1"


def test_checkpoint_round_trips_completed_task6_variant_result_payload() -> None:
    """Checkpoint persistence must accept the frozen completed VariantResult shape."""
    result = CheckpointResult(
        case_id="case-1",
        variant=StageCVariant.ADAPTIVE,
        status="completed",
        attempt=2,
        payload={
            "case_id": "case-1",
            "variant": "adaptive",
            "status": "completed",
            "attempt": 2,
            "strategy": "multi",
            "strategy_allowed": True,
            "strategy_preferred": True,
            "evidence_status": "complete",
            "round_count": 2,
            "query_count_by_round": [3, 2],
            "model_calls": 3,
            "tokens": 321,
            "latency_ms": 123.4,
            "evaluated_citation_count": 5,
            "full_citation_count": 6,
            "citations": [
                {
                    "citation_id": "C1",
                    "tenant_key": "org_a",
                    "document_id": "document-1",
                    "version_id": "version-2",
                    "chunk_id": "chunk-1",
                    "rank": 1,
                }
            ],
            "candidate_trace": [],
            "metrics": {
                "covered_group_count": 1,
                "required_group_count": 1,
                "evidence_group_recall": 1.0,
            },
            "safety_flags": {"cross_tenant_leak": False},
        },
    )

    restored = CheckpointResult.from_dict(result.to_dict())

    assert restored.to_dict() == result.to_dict()


def test_checkpoint_round_trips_none_strategy_but_rejects_unknown_strategy() -> None:
    """Task 6 must persist production NONE without opening the strategy boundary."""
    payload = normalized_payload(
        "case-1", StageCVariant.ADAPTIVE, status="completed", attempt=1
    )
    payload["strategy"] = "none"
    result = CheckpointResult(
        case_id="case-1",
        variant=StageCVariant.ADAPTIVE,
        status="completed",
        attempt=1,
        payload=payload,
    )

    assert CheckpointResult.from_dict(result.to_dict()).payload["strategy"] == "none"
    unknown_payload = normalized_payload(
        "case-1", StageCVariant.ADAPTIVE, status="completed", attempt=1
    )
    unknown_payload["strategy"] = "invented"
    with pytest.raises(ValueError, match="strategy"):
        CheckpointResult(
            case_id="case-1",
            variant=StageCVariant.ADAPTIVE,
            status="completed",
            attempt=1,
            payload=unknown_payload,
        )


def test_checkpoint_round_trips_infrastructure_failed_task6_payload() -> None:
    """Infrastructure failures preserve Task 6's null metrics instead of scoring."""
    result = CheckpointResult(
        case_id="case-1",
        variant=StageCVariant.BASELINE,
        status="infrastructure_failed",
        attempt=1,
        payload={
            "case_id": "case-1",
            "variant": "baseline",
            "status": "infrastructure_failed",
            "attempt": 1,
            "error_code": "provider_timeout",
            "evidence_status": None,
            "round_count": 0,
            "query_count_by_round": [],
            "model_calls": 0,
            "tokens": 0,
            "latency_ms": 0.0,
            "evaluated_citation_count": 0,
            "full_citation_count": 0,
            "citations": [],
            "candidate_trace": [],
            "metrics": None,
            "safety_flags": {"cross_tenant_leak": False},
        },
    )

    restored = CheckpointResult.from_dict(result.to_dict())

    assert restored.to_dict() == result.to_dict()


def test_record_writes_through_a_same_directory_temporary_file(
    tmp_path: Path,
) -> None:
    """Replacing from another filesystem could make the checkpoint non-atomic."""
    checkpoint = tmp_path / "checkpoint.json"
    store = CheckpointStore(checkpoint)
    observed: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def recording_replace(source: str | Path, destination: str | Path) -> None:
        observed.append((Path(source), Path(destination)))
        real_replace(source, destination)

    with patch("app.evals.stage_c.checkpoint.os.replace", side_effect=recording_replace):
        store.initialize(metadata("atomic"))
        store.set_fixture_manifest({"schema_version": 1, "documents": []})
        store.record(completed("case-1", StageCVariant.BASELINE, attempt=1))

    assert len(observed) == 3
    assert all(source.parent == checkpoint.parent for source, _ in observed)
    assert all(destination == checkpoint for _, destination in observed)
    assert checkpoint.is_file()
    store.load(metadata("atomic"))
    assert json.loads(checkpoint.read_text(encoding="utf-8"))["fixture_manifest"] == {
        "schema_version": 1,
        "documents": [],
    }


def test_checkpoint_stores_hashes_not_document_or_api_secret(tmp_path: Path) -> None:
    """A durable run file must never become a copy of corpus or credentials."""
    document = tmp_path / "source.md"
    document_canary = "DOCUMENT-BODY-CANARY"
    api_key = "sk-stage-c-secret"
    document.write_text(document_canary, encoding="utf-8")
    corpus_hash = sha256_corpus((document,))
    run_metadata = replace(metadata("privacy"), corpus_sha256=corpus_hash)
    store = CheckpointStore(tmp_path / "checkpoint.json")

    store.initialize(run_metadata)
    store.record(completed("case-1", StageCVariant.BASELINE, attempt=1))

    raw = (tmp_path / "checkpoint.json").read_text(encoding="utf-8")
    assert corpus_hash in raw
    assert sha256_file(document) == hashlib.sha256(document_canary.encode()).hexdigest()
    assert document_canary not in raw
    assert api_key not in raw


@pytest.mark.parametrize(
    "fixture_manifest",
    [[], "not-a-mapping", {"documents": object()}],
)
def test_fixture_manifest_state_rejects_non_json_mapping(
    fixture_manifest: object,
) -> None:
    """Damaged fixture metadata must not be silently accepted on resume."""
    with pytest.raises((TypeError, ValueError)):
        FixtureManifestState(fixture_manifest)


def test_load_rejects_damaged_checkpoint_structure(tmp_path: Path) -> None:
    """A malformed persisted result must not be mistaken for a reusable success."""
    checkpoint = tmp_path / "checkpoint.json"
    store = CheckpointStore(checkpoint)
    state = store.initialize(metadata("damaged"))
    raw = json.loads(checkpoint.read_text(encoding="utf-8"))
    raw["results"] = {
        "case-1:baseline": {
            "case_id": "case-1",
            "variant": "baseline",
            "status": "completed",
            "attempt": 0,
            "payload": {},
        }
    }
    checkpoint.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises((TypeError, ValueError), match="attempt"):
        store.load(metadata("damaged"))

    assert state.fingerprint == metadata("damaged").fingerprint


def test_load_rejects_truncated_success_before_should_run_can_skip_it(
    tmp_path: Path,
) -> None:
    """A persisted completed label cannot bypass common-payload validation."""
    checkpoint = tmp_path / "checkpoint.json"
    run_metadata = metadata("truncated-success")
    CheckpointStore(checkpoint).initialize(run_metadata)
    raw = json.loads(checkpoint.read_text(encoding="utf-8"))
    payload = normalized_payload(
        "case-1", StageCVariant.BASELINE, status="completed", attempt=1
    )
    del payload["candidate_trace"]
    raw["results"] = {
        "case-1:baseline": {
            "case_id": "case-1",
            "variant": "baseline",
            "status": "completed",
            "attempt": 1,
            "payload": payload,
        }
    }
    checkpoint.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="candidate_trace"):
        CheckpointStore(checkpoint).load(run_metadata)
