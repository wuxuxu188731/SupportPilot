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

from app.evals.stage_c.models import StageCVariant, TenantKey


_SCHEMA_VERSION = 1
_RESULT_STATUSES = {"completed", "infrastructure_failed"}
_PAYLOAD_FIELDS = {
    "case_id", "variant", "status", "attempt", "error_code", "strategy",
    "strategy_allowed", "strategy_preferred", "evidence_status", "round_count",
    "query_count_by_round", "model_calls", "tokens", "latency_ms",
    "evaluated_citation_count", "full_citation_count", "citations",
    "candidate_trace", "metrics", "safety_flags",
}
_OPTIONAL_PAYLOAD_FIELDS = {
    "error_code", "strategy", "strategy_allowed", "strategy_preferred",
}
_COMMON_PAYLOAD_FIELDS = _PAYLOAD_FIELDS - _OPTIONAL_PAYLOAD_FIELDS
_ERROR_CODES = {
    "provider_timeout", "provider_unavailable", "provider_error", "invalid_response",
    "budget_exhausted", "unexpected_error",
}
_INFRASTRUCTURE_ERROR_CODES = _ERROR_CODES - {"budget_exhausted"}
_METRIC_FIELDS = {
    "covered_group_count", "required_group_count", "evidence_group_recall",
    "retrieval_precision", "complete_evidence_coverage", "relevant_top5_count",
    "evaluated_citation_count", "returned_full_count", "cross_tenant_leak",
    "disabled_document_leak", "inactive_version_leak", "unknown_identity_count",
    "quality_scope_reason",
}
_SAFETY_FLAG_FIELDS = {
    "cross_tenant_leak", "disabled_document_leak", "inactive_version_leak",
    "unknown_identity_count", "injection_followed", "budget_exceeded",
    "round_limit_exceeded", "query_limit_exceeded", "model_call_limit_exceeded",
}
_CITATION_FIELDS = {
    "citation_id", "tenant_key", "document_key", "document_id", "version_id",
    "chunk_id", "heading_path", "rank", "score", "identity_known",
    "identity_consistent", "document_status", "active_version_id",
}
_CANDIDATE_TRACE_FIELDS = _CITATION_FIELDS | {"round", "query_index", "source"}


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


def _require_allowed_keys(
    raw: Mapping[str, object], *, allowed: set[str], name: str
) -> None:
    unexpected = sorted(set(raw) - allowed)
    if unexpected:
        raise ValueError(f"{name} has unsupported keys: {unexpected}")


def _require_nonempty_string(value: object, *, name: str) -> str:
    if type(value) is not str or not value:
        raise TypeError(f"{name} must be a non-empty string")
    return value


def _require_positive_int(value: object, *, name: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _require_nonnegative_int(value: object, *, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _require_finite_number(value: object, *, name: str) -> float:
    if type(value) not in {int, float} or isinstance(value, bool):
        raise TypeError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _validate_identity_fields(raw: Mapping[str, object], *, name: str) -> None:
    for field in (
        "citation_id", "document_key", "document_id", "version_id", "chunk_id",
        "heading_path", "active_version_id",
    ):
        if field in raw and raw[field] is not None:
            _require_nonempty_string(raw[field], name=f"{name}.{field}")
    if "tenant_key" in raw and raw["tenant_key"] is not None:
        try:
            TenantKey(raw["tenant_key"])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name}.tenant_key is invalid") from exc
    if "document_status" in raw and raw["document_status"] is not None:
        if raw["document_status"] not in {"active", "disabled"}:
            raise ValueError(f"{name}.document_status is invalid")


def _validate_citation_artifacts(
    value: object, *, allowed: set[str], name: str
) -> None:
    if not isinstance(value, list):
        raise TypeError(f"{name} must be a JSON list")
    for index, item in enumerate(value):
        raw = _require_mapping(item, name=f"{name}[{index}]")
        _require_allowed_keys(raw, allowed=allowed, name=f"{name}[{index}]")
        _validate_identity_fields(raw, name=f"{name}[{index}]")
        for field in ("rank", "round", "query_index"):
            if field in raw:
                _require_positive_int(raw[field], name=f"{name}[{index}].{field}")
        if "score" in raw:
            _require_finite_number(raw["score"], name=f"{name}[{index}].score")
        for field in ("identity_known", "identity_consistent"):
            if field in raw and type(raw[field]) is not bool:
                raise TypeError(f"{name}[{index}].{field} must be a boolean")
        if "source" in raw and raw["source"] not in {"dense", "sparse", "fused"}:
            raise ValueError(f"{name}[{index}].source is invalid")


def _validate_checkpoint_payload(
    payload: Mapping[str, object],
    *,
    case_id: str,
    variant: StageCVariant,
    status: str,
    attempt: int,
) -> None:
    _require_allowed_keys(payload, allowed=_PAYLOAD_FIELDS, name="payload")
    missing = sorted(_COMMON_PAYLOAD_FIELDS - set(payload))
    if missing:
        raise ValueError(f"payload is missing required fields: {missing}")
    _require_nonempty_string(payload["case_id"], name="payload.case_id")
    _require_nonempty_string(payload["variant"], name="payload.variant")
    _require_nonempty_string(payload["status"], name="payload.status")
    _require_positive_int(payload["attempt"], name="payload.attempt")
    if payload["case_id"] != case_id:
        raise ValueError("payload.case_id must match the checkpoint result")
    if payload["variant"] != variant.value:
        raise ValueError("payload.variant must match the checkpoint result")
    if payload["status"] != status:
        raise ValueError("payload.status must match the checkpoint result")
    if payload["attempt"] != attempt:
        raise ValueError("payload.attempt must match the checkpoint result")
    if "error_code" in payload and payload["error_code"] not in _ERROR_CODES:
        raise ValueError("payload.error_code is invalid")
    if "strategy" in payload and payload["strategy"] not in {
        "single",
        "multi",
        "none",
        None,
    }:
        raise ValueError("payload.strategy is invalid")
    if "evidence_status" in payload and payload["evidence_status"] not in {
        "complete", "partial", "missing", "not_applicable", None,
    }:
        raise ValueError("payload.evidence_status is invalid")
    for field in (
        "round_count",
        "model_calls",
        "tokens",
        "evaluated_citation_count",
        "full_citation_count",
    ):
        if field in payload:
            _require_nonnegative_int(payload[field], name=f"payload.{field}")
    for field in ("strategy_allowed", "strategy_preferred"):
        if field in payload and type(payload[field]) is not bool:
            raise TypeError(f"payload.{field} must be a boolean")
    if "query_count_by_round" in payload:
        query_counts = payload["query_count_by_round"]
        if not isinstance(query_counts, list):
            raise TypeError("payload.query_count_by_round must be a JSON list")
        for index, count in enumerate(query_counts):
            _require_nonnegative_int(
                count, name=f"payload.query_count_by_round[{index}]"
            )
    if "latency_ms" in payload:
        if _require_finite_number(payload["latency_ms"], name="payload.latency_ms") < 0:
            raise ValueError("payload.latency_ms must be non-negative")
    if "citations" in payload:
        _validate_citation_artifacts(
            payload["citations"], allowed=_CITATION_FIELDS, name="payload.citations"
        )
    if "candidate_trace" in payload:
        _validate_citation_artifacts(
            payload["candidate_trace"],
            allowed=_CANDIDATE_TRACE_FIELDS,
            name="payload.candidate_trace",
        )
    metrics_value = payload["metrics"]
    if status == "completed":
        metrics = _require_mapping(metrics_value, name="payload.metrics")
        _require_allowed_keys(metrics, allowed=_METRIC_FIELDS, name="payload.metrics")
        for field in (
            "covered_group_count", "required_group_count", "relevant_top5_count",
            "evaluated_citation_count", "returned_full_count", "unknown_identity_count",
        ):
            if field in metrics:
                _require_nonnegative_int(metrics[field], name=f"payload.metrics.{field}")
        for field in ("evidence_group_recall", "retrieval_precision"):
            if field in metrics and metrics[field] is not None:
                _require_finite_number(metrics[field], name=f"payload.metrics.{field}")
        if "complete_evidence_coverage" in metrics and type(
            metrics["complete_evidence_coverage"]
        ) not in {bool, type(None)}:
            raise TypeError("payload.metrics.complete_evidence_coverage must be boolean or null")
        for field in (
            "cross_tenant_leak", "disabled_document_leak", "inactive_version_leak",
        ):
            if field in metrics and type(metrics[field]) is not bool:
                raise TypeError(f"payload.metrics.{field} must be a boolean")
        if "quality_scope_reason" in metrics and metrics["quality_scope_reason"] not in {
            "no_required_evidence_groups", "no_measurable_cases", None,
        }:
            raise ValueError("payload.metrics.quality_scope_reason is invalid")
    else:
        if metrics_value is not None:
            raise ValueError("infrastructure_failed payload.metrics must be null")
        if "error_code" not in payload:
            raise ValueError("infrastructure_failed payload requires error_code")
        if payload["error_code"] not in _INFRASTRUCTURE_ERROR_CODES:
            raise ValueError("infrastructure_failed payload.error_code is invalid")
    if "safety_flags" in payload:
        flags = _require_mapping(payload["safety_flags"], name="payload.safety_flags")
        _require_allowed_keys(flags, allowed=_SAFETY_FLAG_FIELDS, name="payload.safety_flags")
        for field, value in flags.items():
            if field == "unknown_identity_count":
                _require_nonnegative_int(value, name=f"payload.safety_flags.{field}")
            elif type(value) is not bool:
                raise TypeError(f"payload.safety_flags.{field} must be a boolean")


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
    """Return an order-independent digest bound to each corpus file identity."""
    resolved_paths = tuple(Path(path).resolve() for path in paths)
    if not resolved_paths:
        return build_run_fingerprint({"files": []})
    common_parent = Path(
        os.path.commonpath([str(path.parent) for path in resolved_paths])
    )
    files = [
        {
            "path": (
                path.name
                if len(resolved_paths) == 1
                else path.relative_to(common_parent).as_posix()
            ),
            "sha256": sha256_file(path),
        }
        for path in resolved_paths
    ]
    if len({entry["path"] for entry in files}) != len(files):
        raise ValueError("corpus paths must be unique")
    return build_run_fingerprint({"files": sorted(files, key=lambda entry: entry["path"])})


def _validate_fixture_manifest(manifest: Mapping[str, object]) -> None:
    _require_exact_keys(
        manifest,
        expected={"schema_version", "documents"},
        name="fixture_manifest",
    )
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
        raise ValueError("fixture_manifest schema_version must be 1")
    documents = manifest["documents"]
    if not isinstance(documents, list):
        raise TypeError("fixture_manifest documents must be a list")
    expected_document_keys = {
        "tenant_key",
        "document_key",
        "title",
        "document_id",
        "active_version_id",
        "old_version_id",
    }
    known_documents: set[tuple[TenantKey, str]] = set()
    for index, document in enumerate(documents):
        raw_document = _require_mapping(document, name=f"fixture_manifest.documents[{index}]")
        _require_exact_keys(
            raw_document,
            expected=expected_document_keys,
            name=f"fixture_manifest.documents[{index}]",
        )
        try:
            tenant_key = TenantKey(raw_document["tenant_key"])
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"fixture_manifest.documents[{index}].tenant_key is invalid"
            ) from exc
        for key in (
            "document_key",
            "title",
            "document_id",
            "active_version_id",
        ):
            _require_nonempty_string(
                raw_document[key], name=f"fixture_manifest.documents[{index}].{key}"
            )
        old_version_id = raw_document["old_version_id"]
        if old_version_id is not None:
            _require_nonempty_string(
                old_version_id,
                name=f"fixture_manifest.documents[{index}].old_version_id",
            )
        document_key = raw_document["document_key"]
        identity = (tenant_key, document_key)
        if identity in known_documents:
            raise ValueError("fixture_manifest contains duplicate tenant/document keys")
        known_documents.add(identity)


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
    adaptive_top_k: int
    max_rounds: int
    max_round_queries: tuple[int, ...]
    max_planner_calls: int
    max_assessor_calls: int
    max_model_calls: int
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
        _require_positive_int(self.adaptive_top_k, name="adaptive_top_k")
        _require_positive_int(self.max_rounds, name="max_rounds")
        if type(self.max_round_queries) is not tuple:
            raise TypeError("max_round_queries must be an immutable tuple")
        if not self.max_round_queries:
            raise ValueError("max_round_queries must not be empty")
        for index, value in enumerate(self.max_round_queries):
            _require_positive_int(value, name=f"max_round_queries[{index}]")
        for name in (
            "max_planner_calls",
            "max_assessor_calls",
            "max_model_calls",
        ):
            _require_nonnegative_int(getattr(self, name), name=name)
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
            "adaptive_top_k": self.adaptive_top_k,
            "max_rounds": self.max_rounds,
            "max_round_queries": list(self.max_round_queries),
            "max_planner_calls": self.max_planner_calls,
            "max_assessor_calls": self.max_assessor_calls,
            "max_model_calls": self.max_model_calls,
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
        parsed = dict(raw)
        max_round_queries = parsed["max_round_queries"]
        if not isinstance(max_round_queries, list):
            raise TypeError("metadata max_round_queries must be a JSON list")
        parsed["max_round_queries"] = tuple(max_round_queries)
        return cls(**parsed)


@dataclass(frozen=True)
class FixtureManifestState:
    """An immutable, JSON-only fixture manifest snapshot."""

    manifest: Mapping[str, object]

    def __post_init__(self) -> None:
        manifest = _require_mapping(self.manifest, name="fixture_manifest")
        _validate_fixture_manifest(manifest)
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
        _validate_checkpoint_payload(
            payload,
            case_id=self.case_id,
            variant=self.variant,
            status=self.status,
            attempt=self.attempt,
        )
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
            fixture_manifest = FixtureManifestState(
                _thaw_json(self.fixture_manifest)  # type: ignore[arg-type]
            ).manifest
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
