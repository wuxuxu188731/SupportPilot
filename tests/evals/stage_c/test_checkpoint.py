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
        run_id=run_id,
        created_at="2026-08-21T00:00:00Z",
    )


def completed(case_id: str, variant: StageCVariant, *, attempt: int) -> CheckpointResult:
    return CheckpointResult(
        case_id=case_id,
        variant=variant,
        status="completed",
        attempt=attempt,
        payload={"outcome": "content-free"},
    )


def infrastructure_failed(
    case_id: str, variant: StageCVariant, *, attempt: int
) -> CheckpointResult:
    return CheckpointResult(
        case_id=case_id,
        variant=variant,
        status="infrastructure_failed",
        attempt=attempt,
        payload={"reason": "provider_timeout"},
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
