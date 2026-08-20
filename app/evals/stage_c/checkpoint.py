"""Atomic, fingerprinted persistence for resumable Stage C evaluation runs."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from app.evals.stage_c.models import StageCVariant


_SCHEMA_VERSION = 1
_RESULT_STATUSES = {"completed", "infrastructure_failed"}


def _validate_json_value(value: object, *, path: str = "value") -> None:
    """Reject values that JSON cannot safely persist without coercion."""
    if value is None or type(value) in {bool, int, str}:
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{path} must not be NaN or infinite")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError(f"{path} mapping keys must be strings")
            _validate_json_value(item, path=f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _validate_json_value(item, path=f"{path}[{index}]")
        return
    raise TypeError(f"{path} is not a JSON value")


def _freeze_json(value: object) -> object:
    _validate_json_value(value)
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _require_mapping(value: object, *, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a mapping")
    if any(type(key) is not str for key in value):
        raise TypeError(f"{name} keys must be strings")
    return value


def _require_exact_keys(
    raw: Mapping[str, object], *, expected: set[str], name: str
) -> None:
    actual = set(raw)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValueError(f"{name} has invalid keys; missing={missing}, extra={extra}")


def _require_nonempty_string(value: object, *, name: str) -> str:
    if type(value) is not str or not value:
        raise TypeError(f"{name} must be a non-empty string")
    return value


def _require_positive_int(value: object, *, name: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _require_finite_number(value: object, *, name: str) -> float:
    if type(value) not in {int, float} or isinstance(value, bool):
        raise TypeError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def build_run_fingerprint(metadata_without_fingerprint: Mapping[str, object]) -> str:
    """Hash metadata using a canonical JSON representation."""
    _require_mapping(metadata_without_fingerprint, name="metadata")
    _validate_json_value(metadata_without_fingerprint, path="metadata")
    encoded = json.dumps(
        metadata_without_fingerprint,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def sha256_file(path: str | Path) -> str:
    """Return a SHA-256 digest of a file's bytes."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_corpus(paths: Iterable[str | Path]) -> str:
    """Return an order-independent digest of the corpus' file digests."""
    file_digests = sorted(sha256_file(path) for path in paths)
    return build_run_fingerprint({"file_sha256": file_digests})


@dataclass(frozen=True)
class RunMetadata:
    """All stable and volatile information needed to reproduce a Stage C run."""

    git_revision: str
    case_dataset_sha256: str
    corpus_sha256: str
    loader_version: str
    chunker_version: str
    embedding_model: str
    embedding_dimensions: int
    collection_name: str
    trace_schema_version: str
    planner_model: str
    planner_prompt_version: str
    assessor_model: str
    assessor_prompt_version: str
    top_k: int
    prefetch_limit: int
    token_budget: int
    score_threshold: float
    timeout_seconds: float
    runner_schema_version: str
    run_id: str | None = None
    created_at: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "git_revision",
            "case_dataset_sha256",
            "corpus_sha256",
            "loader_version",
            "chunker_version",
            "embedding_model",
            "collection_name",
            "trace_schema_version",
            "planner_model",
            "planner_prompt_version",
            "assessor_model",
            "assessor_prompt_version",
            "runner_schema_version",
        ):
            _require_nonempty_string(getattr(self, name), name=name)
        for name in ("embedding_dimensions", "top_k", "prefetch_limit", "token_budget"):
            _require_positive_int(getattr(self, name), name=name)
        _require_finite_number(self.score_threshold, name="score_threshold")
        if _require_finite_number(self.timeout_seconds, name="timeout_seconds") <= 0:
            raise ValueError("timeout_seconds must be positive")
        for name in ("run_id", "created_at"):
            value = getattr(self, name)
            if value is not None and (type(value) is not str or not value):
                raise TypeError(f"{name} must be a non-empty string or None")

    def fingerprint_fields(self) -> dict[str, object]:
        """Return reproducibility inputs, deliberately excluding volatile fields."""
        return {
            "git_revision": self.git_revision,
            "case_dataset_sha256": self.case_dataset_sha256,
            "corpus_sha256": self.corpus_sha256,
            "loader_version": self.loader_version,
            "chunker_version": self.chunker_version,
            "embedding_model": self.embedding_model,
            "embedding_dimensions": self.embedding_dimensions,
            "collection_name": self.collection_name,
            "trace_schema_version": self.trace_schema_version,
            "planner_model": self.planner_model,
            "planner_prompt_version": self.planner_prompt_version,
            "assessor_model": self.assessor_model,
            "assessor_prompt_version": self.assessor_prompt_version,
            "top_k": self.top_k,
            "prefetch_limit": self.prefetch_limit,
            "token_budget": self.token_budget,
            "score_threshold": self.score_threshold,
            "timeout_seconds": self.timeout_seconds,
            "runner_schema_version": self.runner_schema_version,
        }

    @property
    def fingerprint(self) -> str:
        return build_run_fingerprint(self.fingerprint_fields())

    def to_dict(self) -> dict[str, object]:
        return {
            **self.fingerprint_fields(),
            "run_id": self.run_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "RunMetadata":
        raw = _require_mapping(raw, name="metadata")
        expected = set(cls.__dataclass_fields__)
        _require_exact_keys(raw, expected=expected, name="metadata")
        return cls(**dict(raw))


@dataclass(frozen=True)
class FixtureManifestState:
    """An immutable, JSON-only fixture manifest snapshot."""

    manifest: Mapping[str, object]

    def __post_init__(self) -> None:
        manifest = _require_mapping(self.manifest, name="fixture_manifest")
        _validate_json_value(manifest, path="fixture_manifest")
        object.__setattr__(self, "manifest", _freeze_json(manifest))

    def to_dict(self) -> dict[str, object]:
        return _thaw_json(self.manifest)  # type: ignore[return-value]

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "FixtureManifestState":
        return cls(raw)


@dataclass(frozen=True)
class CheckpointResult:
    """One immutable, content-free evaluated variant result."""

    case_id: str
    variant: StageCVariant
    status: Literal["completed", "infrastructure_failed"]
    attempt: int
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        _require_nonempty_string(self.case_id, name="case_id")
        if not isinstance(self.variant, StageCVariant):
            raise TypeError("variant must be a StageCVariant")
        if self.status not in _RESULT_STATUSES:
            raise ValueError("status must be completed or infrastructure_failed")
        _require_positive_int(self.attempt, name="attempt")
        payload = _require_mapping(self.payload, name="payload")
        _validate_json_value(payload, path="payload")
        object.__setattr__(self, "payload", _freeze_json(payload))

    @property
    def key(self) -> str:
        return f"{self.case_id}:{self.variant.value}"

    def to_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "variant": self.variant.value,
            "status": self.status,
            "attempt": self.attempt,
            "payload": _thaw_json(self.payload),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "CheckpointResult":
        raw = _require_mapping(raw, name="result")
        _require_exact_keys(
            raw,
            expected={"case_id", "variant", "status", "attempt", "payload"},
            name="result",
        )
        try:
            variant = StageCVariant(raw["variant"])
        except (TypeError, ValueError) as exc:
            raise ValueError("result variant is invalid") from exc
        return cls(
            case_id=raw["case_id"],  # type: ignore[arg-type]
            variant=variant,
            status=raw["status"],  # type: ignore[arg-type]
            attempt=raw["attempt"],  # type: ignore[arg-type]
            payload=raw["payload"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class CheckpointState:
    """The complete state of one fingerprinted Stage C checkpoint generation."""

    run_id: str
    fingerprint: str
    metadata: RunMetadata
    fixture_manifest: Mapping[str, object] | None = None
    results: Mapping[str, CheckpointResult] = field(default_factory=dict)
    invalidated_previous_fingerprint: str | None = None

    def __post_init__(self) -> None:
        _require_nonempty_string(self.run_id, name="run_id")
        _require_nonempty_string(self.fingerprint, name="fingerprint")
        if not isinstance(self.metadata, RunMetadata):
            raise TypeError("metadata must be RunMetadata")
        if self.fingerprint != self.metadata.fingerprint:
            raise ValueError("checkpoint fingerprint does not match metadata")
        if self.fixture_manifest is not None:
            fixture_manifest = FixtureManifestState(self.fixture_manifest).manifest
        else:
            fixture_manifest = None
        results = _require_mapping(self.results, name="results")
        frozen_results: dict[str, CheckpointResult] = {}
        for key, result in results.items():
            if not isinstance(result, CheckpointResult):
                raise TypeError("results values must be CheckpointResult")
            if key != result.key:
                raise ValueError("results key does not match case and variant")
            frozen_results[key] = result
        if self.invalidated_previous_fingerprint is not None:
            _require_nonempty_string(
                self.invalidated_previous_fingerprint,
                name="invalidated_previous_fingerprint",
            )
        object.__setattr__(self, "fixture_manifest", fixture_manifest)
        object.__setattr__(self, "results", MappingProxyType(frozen_results))

    def should_run(self, case_id: str, variant: StageCVariant) -> bool:
        result = self.result(case_id, variant)
        return result is None or result.status == "infrastructure_failed"

    def next_attempt(self, case_id: str, variant: StageCVariant) -> int:
        result = self.result(case_id, variant)
        return 1 if result is None else result.attempt + 1

    def result(
        self, case_id: str, variant: StageCVariant
    ) -> CheckpointResult | None:
        _require_nonempty_string(case_id, name="case_id")
        if not isinstance(variant, StageCVariant):
            raise TypeError("variant must be a StageCVariant")
        return self.results.get(f"{case_id}:{variant.value}")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "run_id": self.run_id,
            "fingerprint": self.fingerprint,
            "metadata": self.metadata.to_dict(),
            "fixture_manifest": (
                None
                if self.fixture_manifest is None
                else _thaw_json(self.fixture_manifest)
            ),
            "results": {key: result.to_dict() for key, result in self.results.items()},
            "invalidated_previous_fingerprint": self.invalidated_previous_fingerprint,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "CheckpointState":
        raw = _require_mapping(raw, name="checkpoint")
        _require_exact_keys(
            raw,
            expected={
                "schema_version",
                "run_id",
                "fingerprint",
                "metadata",
                "fixture_manifest",
                "results",
                "invalidated_previous_fingerprint",
            },
            name="checkpoint",
        )
        if raw["schema_version"] != _SCHEMA_VERSION:
            raise ValueError(
                f"unsupported checkpoint schema: {raw['schema_version']!r}"
            )
        raw_results = _require_mapping(raw["results"], name="results")
        results = {
            key: CheckpointResult.from_dict(_require_mapping(value, name="result"))
            for key, value in raw_results.items()
        }
        fixture_manifest = raw["fixture_manifest"]
        if fixture_manifest is not None:
            fixture_manifest = FixtureManifestState.from_dict(
                _require_mapping(fixture_manifest, name="fixture_manifest")
            ).manifest
        invalidated = raw["invalidated_previous_fingerprint"]
        if invalidated is not None and type(invalidated) is not str:
            raise TypeError("invalidated_previous_fingerprint must be a string or None")
        return cls(
            run_id=raw["run_id"],  # type: ignore[arg-type]
            fingerprint=raw["fingerprint"],  # type: ignore[arg-type]
            metadata=RunMetadata.from_dict(
                _require_mapping(raw["metadata"], name="metadata")
            ),
            fixture_manifest=fixture_manifest,
            results=results,
            invalidated_previous_fingerprint=invalidated,
        )


class CheckpointStore:
    """Persist a single checkpoint file with atomic generation changes."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._state: CheckpointState | None = None

    def initialize(self, metadata: RunMetadata) -> CheckpointState:
        """Create, resume, or invalidate a checkpoint based on its fingerprint."""
        if not isinstance(metadata, RunMetadata):
            raise TypeError("metadata must be RunMetadata")
        if self._path.exists():
            previous = self._read_state()
            if previous.fingerprint == metadata.fingerprint:
                self._state = previous
                return previous
            state = self._new_state(
                metadata,
                invalidated_previous_fingerprint=previous.fingerprint,
            )
        else:
            state = self._new_state(metadata)
        self._state = state
        self._write_state()
        return state

    def load(self, metadata: RunMetadata) -> CheckpointState:
        """Load only a checkpoint whose persisted fingerprint exactly matches."""
        if not isinstance(metadata, RunMetadata):
            raise TypeError("metadata must be RunMetadata")
        state = self._read_state()
        if state.fingerprint != metadata.fingerprint:
            raise ValueError("checkpoint fingerprint does not match requested metadata")
        self._state = state
        return state

    def record(self, result: CheckpointResult) -> CheckpointState:
        """Persist one result immediately so an interrupted run can resume."""
        if not isinstance(result, CheckpointResult):
            raise TypeError("result must be CheckpointResult")
        state = self._require_state()
        updated = dict(state.results)
        updated[result.key] = result
        self._state = replace(state, results=updated)
        self._write_state()
        return self._state

    def set_fixture_manifest(self, mapping: Mapping[str, object]) -> CheckpointState:
        """Persist an immutable JSON fixture manifest immediately."""
        state = self._require_state()
        manifest = FixtureManifestState(mapping).manifest
        self._state = replace(state, fixture_manifest=manifest)
        self._write_state()
        return self._state

    def _new_state(
        self,
        metadata: RunMetadata,
        *,
        invalidated_previous_fingerprint: str | None = None,
    ) -> CheckpointState:
        return CheckpointState(
            run_id=metadata.run_id or uuid.uuid4().hex,
            fingerprint=metadata.fingerprint,
            metadata=metadata,
            invalidated_previous_fingerprint=invalidated_previous_fingerprint,
        )

    def _require_state(self) -> CheckpointState:
        if self._state is None:
            raise RuntimeError("checkpoint store has not been initialized or loaded")
        return self._state

    def _read_state(self) -> CheckpointState:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("checkpoint is not valid JSON") from exc
        return CheckpointState.from_dict(_require_mapping(raw, name="checkpoint"))

    def _write_state(self) -> None:
        state = self._require_state()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self._path.parent,
                prefix=f".{self._path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                json.dump(
                    state.to_dict(),
                    temporary,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, self._path)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()
