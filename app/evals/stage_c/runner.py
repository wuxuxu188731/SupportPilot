"""Resumable orchestration for the real Stage C retrieval comparison."""

from __future__ import annotations

import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence
from uuid import uuid4

from app.core.config import MODEL_NAME, get_knowledge_settings
from app.evals.stage_c.checkpoint import (
    CheckpointResult,
    CheckpointState,
    CheckpointStore,
    RunMetadata,
    sha256_corpus,
    sha256_file,
)
from app.evals.stage_c.fixtures import (
    CorpusDocumentSpec,
    FixtureManifest,
    StageCFixtureManager,
    corpus_specs,
)
from app.evals.stage_c.models import (
    StageCCase,
    StageCVariant,
    TenantKey,
    load_stage_c_cases,
)
from app.evals.stage_c.reporting import compile_stage_c_report, normalize_result
from app.knowledge.base import RetrievalEvent, SearchInternalError
from app.knowledge.chunking import CHUNKER_VERSION, KnowledgeChunker
from app.knowledge.document_loader import LOADER_VERSION, DocumentLoader
from app.knowledge.evidence import ASSESSOR_PROMPT_VERSION
from app.knowledge.factory import KnowledgeServices, create_knowledge_services
from app.knowledge.planning import PLANNER_PROMPT_VERSION
from app.knowledge.results import (
    AdaptiveSearchResult,
    BaselineSearchResult,
    RetrievalSummary,
    RetrievalTrace,
)
from app.knowledge.retrieval import PREFETCH_LIMIT, TOKEN_BUDGET, TOP_K
from app.knowledge.service import (
    ADAPTIVE_TOP_K,
    MAX_ASSESSOR_CALLS,
    MAX_MODEL_CALLS,
    MAX_PLANNER_CALLS,
    MAX_ROUND_QUERIES,
    MAX_ROUNDS,
)


RUNNER_SCHEMA_VERSION = "stage-c-runner-v1"
TRACE_SCHEMA_VERSION = "baseline-2-adaptive-4"


class StageCInfrastructureFailure(RuntimeError):
    """A checkpointed, sanitized infrastructure stop with an incomplete report."""

    def __init__(
        self,
        *,
        case_id: str,
        variant: StageCVariant | str,
        classification: str,
        report: Mapping[str, object],
    ) -> None:
        variant_value = (
            variant.value if isinstance(variant, StageCVariant) else str(variant)
        )
        self.case_id = case_id
        self.variant = variant_value
        self.classification = classification
        self.report = dict(report)
        super().__init__("Stage C retrieval evaluation stopped after checkpointing")


def classify_infrastructure_failure(value: object) -> str:
    """Reduce a provider/vector failure to one CLI-safe classification."""

    error = getattr(value, "error", value)
    code = str(getattr(error, "code", ""))
    internal_reason = str(getattr(error, "internal_reason", "") or "")
    safe_message = str(getattr(error, "safe_message", "") or "")
    diagnostic = f"{internal_reason} {safe_message} {type(error).__name__}".lower()
    current = error
    seen: set[int] = set()
    status_codes: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        status_code = getattr(current, "status_code", None)
        if isinstance(status_code, int) and not isinstance(status_code, bool):
            status_codes.add(status_code)
        current = getattr(current, "__cause__", None)
    balance_markers = (
        "insufficient_balance",
        "insufficient balance",
        "balance",
        "quota",
        "status=402",
        "status_code=402",
    )
    if code == "EMBEDDING_UNAVAILABLE" and any(
        marker in diagnostic for marker in balance_markers
    ):
        return "dashscope_balance"
    if code == "SEARCH_INTERNAL_ERROR":
        if any(marker in diagnostic for marker in balance_markers):
            return "deepseek_balance"
        return "deepseek_provider_failure"
    if 503 in status_codes or (
        code == "VECTOR_STORE_UNAVAILABLE" and "503" in diagnostic
    ):
        return "qdrant_503"
    return "generic"


def _synthetic_failed_result(variant: StageCVariant):
    summary = RetrievalSummary(
        strategy="baseline" if variant is StageCVariant.BASELINE else "unplanned",
        round_count=0,
        evidence_status="failed",
        latency_ms=0,
    )
    error = SearchInternalError()
    if variant is StageCVariant.BASELINE:
        return BaselineSearchResult(
            ok=False,
            citations=(),
            retrieval_summary=summary,
            error=error,
            selected_chunks=(),
            retrieval_trace=RetrievalTrace.empty(),
        )
    return AdaptiveSearchResult(
        ok=False,
        citations=(),
        retrieval_summary=summary,
        error=error,
        selected_chunks=(),
        retrieval_trace={},
    )


class StageCRetrievalEvalRunner:
    """Run Baseline then Adaptive per case, persisting every variant immediately."""

    def __init__(
        self,
        *,
        cases: Sequence[StageCCase],
        fixture_manager: StageCFixtureManager,
        specs: Sequence[CorpusDocumentSpec],
        services: KnowledgeServices,
        store,
        checkpoint: CheckpointStore,
        metadata: RunMetadata,
        organization_ids: Mapping[TenantKey, str],
    ) -> None:
        self._cases = tuple(cases)
        self._fixture_manager = fixture_manager
        self._specs = tuple(specs)
        self._services = services
        self._store = store
        self._checkpoint = checkpoint
        self._metadata = metadata
        self._organization_ids = dict(organization_ids)

    def run(self) -> dict[str, object]:
        state = self._checkpoint.initialize(self._metadata)
        setup_failure: str | None = None
        try:
            state = self._prepare_or_restore_fixture(state)
        except Exception as error:  # noqa: BLE001 - preserve initialized checkpoint
            setup_failure = classify_infrastructure_failure(error)
        if setup_failure is not None:
            raise StageCInfrastructureFailure(
                case_id="fixture-setup",
                variant="fixture",
                classification=setup_failure,
                report=self._compile_report(state),
            )
        variants = (StageCVariant.BASELINE, StageCVariant.ADAPTIVE)

        for case in self._cases:
            organization_id = self._organization_ids[case.tenant_key]
            with self._fixture_manager.scenario(case):
                identity_index = self._fixture_manager.identity_index
                for variant in variants:
                    if not state.should_run(case.case_id, variant):
                        continue
                    attempt = state.next_attempt(case.case_id, variant)
                    conversation_id = (
                        f"stage-c:{state.run_id}:{case.case_id}:"
                        f"{variant.value}:attempt-{attempt}"
                    )
                    result, raised_classification = self._call_service(
                        variant=variant,
                        organization_id=organization_id,
                        question=case.question,
                        conversation_id=conversation_id,
                    )
                    event: RetrievalEvent | None = self._store.get_retrieval_event(
                        organization_id=organization_id,
                        conversation_id=conversation_id,
                    )
                    if event is not None and (
                        event.organization_id != organization_id
                        or event.conversation_id != conversation_id
                    ):
                        event = None
                    normalized = normalize_result(
                        case=case,
                        variant=variant,
                        result=result,
                        event=event,
                        identity_index=identity_index,
                        attempt=attempt,
                    )
                    record = CheckpointResult(
                        case_id=case.case_id,
                        variant=variant,
                        status=normalized.status,
                        attempt=attempt,
                        payload=normalized.to_payload(),
                    )
                    state = self._checkpoint.record(record)
                    if normalized.status == "infrastructure_failed":
                        classification = raised_classification or (
                            classify_infrastructure_failure(result)
                            if event is not None
                            else "generic"
                        )
                        raise StageCInfrastructureFailure(
                            case_id=case.case_id,
                            variant=variant,
                            classification=classification,
                            report=self._compile_report(state),
                        )

        return self._compile_report(state)

    def _prepare_or_restore_fixture(
        self, state: CheckpointState
    ) -> CheckpointState:
        if state.fixture_manifest is None:
            manifest = self._fixture_manager.prepare(
                cases=self._cases,
                specs=self._specs,
            )
            return self._checkpoint.set_fixture_manifest(manifest.to_dict())

        raw_manifest = state.to_dict()["fixture_manifest"]
        if not isinstance(raw_manifest, Mapping):
            raise ValueError("checkpoint fixture manifest is invalid")
        manifest = FixtureManifest.from_dict(raw_manifest)
        self._fixture_manager.restore(manifest, specs=self._specs)
        return state

    def _call_service(
        self,
        *,
        variant: StageCVariant,
        organization_id: str,
        question: str,
        conversation_id: str,
    ) -> tuple[BaselineSearchResult | AdaptiveSearchResult, str | None]:
        service = (
            self._services.baseline
            if variant is StageCVariant.BASELINE
            else self._services.adaptive
        )
        raised_classification: str | None = None
        try:
            result = service.search(
                organization_id=organization_id,
                question=question,
                conversation_id=conversation_id,
            )
        except Exception as error:  # noqa: BLE001 - provider boundary is checkpointed
            raised_classification = classify_infrastructure_failure(error)
        if raised_classification is not None:
            return _synthetic_failed_result(variant), raised_classification
        return result, None

    def _compile_report(self, state: CheckpointState) -> dict[str, object]:
        report = compile_stage_c_report(self._cases, state)
        return {
            "run": {
                "run_id": state.run_id,
                "fingerprint": state.fingerprint,
                "metadata": state.metadata.to_dict(),
            },
            **report,
        }


def _git_revision(repo_root: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    revision = completed.stdout.strip()
    if not revision:
        raise RuntimeError("git revision is unavailable")
    return revision


def _ensure_fixture_tenants(database_path: str | Path) -> None:
    """Create the two fixed, trusted fixture tenants after migrations run."""

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT OR IGNORE INTO users(id, username, password_hash) VALUES (?, ?, ?)",
            ("stage-c-fixture", "stage-c-fixture", "not-used"),
        )
        connection.executemany(
            "INSERT OR IGNORE INTO organizations(id, name) VALUES (?, ?)",
            (("org_a", "Stage C Org A"), ("org_b", "Stage C Org B")),
        )
        connection.executemany(
            """INSERT OR IGNORE INTO memberships(organization_id, user_id, role)
               VALUES (?, 'stage-c-fixture', 'admin')""",
            (("org_a",), ("org_b",)),
        )


def _build_metadata(
    *,
    repo_root: Path,
    cases_path: Path,
    specs: Sequence[CorpusDocumentSpec],
    settings,
) -> RunMetadata:
    return RunMetadata(
        git_revision=_git_revision(repo_root),
        case_dataset_sha256=sha256_file(cases_path),
        corpus_sha256=sha256_corpus(spec.path for spec in specs),
        loader_version=LOADER_VERSION,
        chunker_version=CHUNKER_VERSION,
        embedding_model=settings.embedding_model,
        embedding_dimensions=settings.embedding_dimensions,
        collection_name=settings.qdrant_collection,
        trace_schema_version=TRACE_SCHEMA_VERSION,
        planner_model=MODEL_NAME,
        planner_prompt_version=PLANNER_PROMPT_VERSION,
        assessor_model=MODEL_NAME,
        assessor_prompt_version=ASSESSOR_PROMPT_VERSION,
        top_k=TOP_K,
        prefetch_limit=PREFETCH_LIMIT,
        token_budget=TOKEN_BUDGET,
        score_threshold=settings.min_fused_score,
        timeout_seconds=settings.search_timeout_seconds,
        runner_schema_version=RUNNER_SCHEMA_VERSION,
        adaptive_top_k=ADAPTIVE_TOP_K,
        max_rounds=MAX_ROUNDS,
        max_round_queries=MAX_ROUND_QUERIES,
        max_planner_calls=MAX_PLANNER_CALLS,
        max_assessor_calls=MAX_ASSESSOR_CALLS,
        max_model_calls=MAX_MODEL_CALLS,
        run_id=uuid4().hex,
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def run_stage_c_retrieval_eval(
    *,
    database_path: str | Path,
    cases_path: str | Path,
    checkpoint_path: str | Path,
    repo_root: str | Path | None = None,
) -> dict[str, object]:
    """Assemble and run Stage C through the production knowledge service graph."""

    root = (
        Path(repo_root).resolve()
        if repo_root is not None
        else Path(__file__).resolve().parents[3]
    )
    database = Path(database_path)
    cases_file = Path(cases_path).resolve()
    settings = get_knowledge_settings()
    cases = load_stage_c_cases(cases_file)
    specs = corpus_specs(root)
    services = create_knowledge_services(
        database,
        settings,
        model_name=MODEL_NAME,
    )
    _ensure_fixture_tenants(database)
    loader = DocumentLoader()
    chunker = KnowledgeChunker()
    fixture_manager = StageCFixtureManager(
        store=services.store,
        ingestion=services.ingestion,
        loader=loader,
        chunker=chunker,
        vector_store=services.vector_store,
    )
    run_metadata = _build_metadata(
        repo_root=root,
        cases_path=cases_file,
        specs=specs,
        settings=settings,
    )
    runner = StageCRetrievalEvalRunner(
        cases=cases,
        fixture_manager=fixture_manager,
        specs=specs,
        services=services,
        store=services.store,
        checkpoint=CheckpointStore(checkpoint_path),
        metadata=run_metadata,
        organization_ids={tenant: tenant.value for tenant in TenantKey},
    )
    return runner.run()
