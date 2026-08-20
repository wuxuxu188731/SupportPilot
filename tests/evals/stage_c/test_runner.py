"""Behavior tests for the resumable Stage C orchestration boundary."""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import KnowledgeSettings
from app.evals.stage_c.checkpoint import CheckpointStore, RunMetadata
from app.evals.stage_c.fixtures import FixtureDocument, FixtureManifest
from app.evals.stage_c.models import (
    EvidenceAlternative,
    EvidenceGroup,
    ExpectedBehavior,
    KeyAnswerFact,
    StageCCase,
    StageCCategory,
    StageCScenario,
    StageCVariant,
    TenantKey,
)
from app.evals.stage_c.runner import (
    StageCInfrastructureFailure,
    StageCRetrievalEvalRunner,
    classify_infrastructure_failure,
    run_stage_c_retrieval_eval,
)
from app.evals.stage_c.scoring import IdentityIndex
from app.knowledge.base import (
    EmbeddingUnavailableError,
    RetrievalEvent,
    SearchInternalError,
    VectorStoreUnavailableError,
)
from app.knowledge.results import (
    AdaptiveSearchResult,
    BaselineSearchResult,
    RetrievalSummary,
    RetrievalTrace,
)


def make_case(case_id: str = "case-1") -> StageCCase:
    return StageCCase(
        case_id=case_id,
        category=StageCCategory.SIMPLE_POLICY,
        scenario=StageCScenario.MAIN_ACTIVE,
        tenant_key=TenantKey.ORG_A,
        question="可信问题，不含租户标识",
        reference_answer="没有命中时应如实说明",
        key_answer_facts=(KeyAnswerFact(fact_id="fact", statement="事实"),),
        required_evidence_groups=(
            EvidenceGroup(
                group_id="group",
                supports_fact_ids=("fact",),
                any_of=(
                    EvidenceAlternative(
                        document_key="returns_exchange", heading_path="政策/退货"
                    ),
                ),
            ),
        ),
        should_have_answer=True,
        expected_behavior=ExpectedBehavior.ANSWER_GROUNDED,
        strategy_expectation=None,
        business_context=None,
        forbidden_tenant_keys=(TenantKey.ORG_B,),
        security_expectations=None,
        notes="",
    )


def metadata() -> RunMetadata:
    return RunMetadata(
        git_revision="abc123",
        case_dataset_sha256="cases-sha",
        corpus_sha256="corpus-sha",
        loader_version="loader-v1",
        chunker_version="chunker-v1",
        embedding_model="embedding-v1",
        embedding_dimensions=1024,
        collection_name="collection",
        trace_schema_version="baseline-2-adaptive-4",
        planner_model="planner-model",
        planner_prompt_version="planner-v1",
        assessor_model="assessor-model",
        assessor_prompt_version="assessor-v1",
        top_k=5,
        prefetch_limit=8,
        token_budget=3000,
        score_threshold=0.0,
        timeout_seconds=15.0,
        runner_schema_version="stage-c-runner-v1",
        adaptive_top_k=6,
        max_rounds=2,
        max_round_queries=(3, 2),
        max_planner_calls=1,
        max_assessor_calls=2,
        max_model_calls=3,
    )


def completed_result(variant: StageCVariant):
    summary = RetrievalSummary(
        strategy="baseline" if variant is StageCVariant.BASELINE else "single",
        round_count=1,
        evidence_status="insufficient",
        latency_ms=3,
    )
    if variant is StageCVariant.BASELINE:
        return BaselineSearchResult(
            ok=True,
            citations=(),
            retrieval_summary=summary,
            error=None,
            selected_chunks=(),
            retrieval_trace=RetrievalTrace.empty(),
        )
    return AdaptiveSearchResult(
        ok=True,
        citations=(),
        retrieval_summary=summary,
        error=None,
        selected_chunks=(),
        retrieval_trace={},
    )


def failed_result(variant: StageCVariant, error):
    summary = RetrievalSummary(
        strategy="baseline" if variant is StageCVariant.BASELINE else "unplanned",
        round_count=1,
        evidence_status="failed",
        latency_ms=3,
    )
    result_type = (
        BaselineSearchResult
        if variant is StageCVariant.BASELINE
        else AdaptiveSearchResult
    )
    kwargs = dict(
        ok=False,
        citations=(),
        retrieval_summary=summary,
        error=error,
        selected_chunks=(),
    )
    if result_type is BaselineSearchResult:
        kwargs["retrieval_trace"] = RetrievalTrace.empty()
    else:
        kwargs["retrieval_trace"] = {}
    return result_type(**kwargs)


def make_event(
    variant: StageCVariant, organization_id: str, conversation_id: str
) -> RetrievalEvent:
    if variant is StageCVariant.BASELINE:
        trace = {"schema_version": 2, "candidates": []}
        strategy = "baseline"
        model_calls = 0
    else:
        trace = {
            "schema_version": 4,
            "queries": [
                {
                    "round": 1,
                    "query_index": 1,
                    "query_digest": "sha256:content-free",
                }
            ],
            "candidates": [],
        }
        strategy = "single"
        model_calls = 2
    return RetrievalEvent(
        event_id=f"event-{conversation_id}",
        organization_id=organization_id,
        conversation_id=conversation_id,
        strategy=strategy,
        original_query="must not enter artifacts",
        planned_queries_json='["must not enter artifacts"]',
        round_count=1,
        candidate_json=json.dumps(trace),
        selected_chunk_ids_json="[]",
        outcome="failed",
        latency_ms=3,
        model_calls=model_calls,
        estimated_tokens=7,
        created_at="2026-08-21T00:00:00Z",
    )


class EventStore:
    def __init__(self) -> None:
        self.events = {}
        self.reads = []

    def get_retrieval_event(self, *, organization_id, conversation_id):
        self.reads.append((organization_id, conversation_id))
        return self.events.get((organization_id, conversation_id))


class FakeFixtureManager:
    def __init__(self) -> None:
        self.state = "active-snapshot"
        self.entries = []
        self.exits = []
        self.prepare_calls = 0
        self.restore_calls = 0
        self.prepare_error = None
        self._identity_index = IdentityIndex(())
        self._manifest = FixtureManifest(
            (
                FixtureDocument(
                    tenant_key=TenantKey.ORG_A,
                    document_key="returns_exchange",
                    title="fixture",
                    document_id="document-1",
                    active_version_id="version-1",
                ),
            )
        )

    @property
    def identity_index(self):
        return self._identity_index

    def prepare(self, *, cases, specs):
        self.prepare_calls += 1
        if self.prepare_error is not None:
            raise self.prepare_error
        return self._manifest

    def restore(self, manifest, *, specs):
        self.restore_calls += 1
        self._manifest = manifest
        return manifest

    @contextmanager
    def scenario(self, case):
        self.entries.append(case.case_id)
        self.state = "scenario-applied"
        try:
            yield
        finally:
            self.state = "restored"
            self.exits.append(case.case_id)


class RecordingCheckpointStore(CheckpointStore):
    def __init__(self, path):
        super().__init__(path)
        self.write_keys = []

    def record(self, result):
        state = super().record(result)
        self.write_keys.append(result.key)
        return state


class FakeService:
    def __init__(self, variant, store, fixture, checkpoint):
        self.variant = variant
        self.store = store
        self.fixture = fixture
        self.checkpoint = checkpoint
        self.result = completed_result(variant)
        self.record_event = True
        self.event_factory = make_event
        self.calls = []

    def search(self, *, organization_id, question, conversation_id):
        self.calls.append(
            {
                "organization_id": organization_id,
                "question": question,
                "conversation_id": conversation_id,
                "scenario_state": self.fixture.state,
                "checkpoint_keys": tuple(self.checkpoint.write_keys),
            }
        )
        if self.record_event:
            self.store.events[(organization_id, conversation_id)] = self.event_factory(
                self.variant, organization_id, conversation_id
            )
        return self.result


def make_runner(tmp_path: Path, cases=(make_case(),)):
    fixture = FakeFixtureManager()
    store = EventStore()
    checkpoint = RecordingCheckpointStore(tmp_path / "checkpoint.json")
    baseline = FakeService(StageCVariant.BASELINE, store, fixture, checkpoint)
    adaptive = FakeService(StageCVariant.ADAPTIVE, store, fixture, checkpoint)
    services = SimpleNamespace(baseline=baseline, adaptive=adaptive)
    runner = StageCRetrievalEvalRunner(
        cases=cases,
        fixture_manager=fixture,
        specs=(),
        services=services,
        store=store,
        checkpoint=checkpoint,
        metadata=metadata(),
        organization_ids={TenantKey.ORG_A: "trusted-org-a"},
    )
    return SimpleNamespace(
        runner=runner,
        fixture=fixture,
        store=store,
        checkpoint=checkpoint,
        baseline=baseline,
        adaptive=adaptive,
        services=services,
    )


def test_runner_keeps_variants_in_one_scenario_and_checkpoints_immediately(tmp_path):
    scope = make_runner(tmp_path)

    report = scope.runner.run()

    assert scope.fixture.entries == ["case-1"]
    assert scope.fixture.exits == ["case-1"]
    assert scope.baseline.calls[0]["scenario_state"] == "scenario-applied"
    assert scope.adaptive.calls[0]["scenario_state"] == "scenario-applied"
    assert scope.adaptive.calls[0]["checkpoint_keys"] == ("case-1:baseline",)
    assert scope.checkpoint.write_keys == ["case-1:baseline", "case-1:adaptive"]
    assert report["completion"]["completed_variants"] == 2


def test_fixture_infrastructure_failure_carries_initialized_incomplete_report(
    tmp_path,
):
    scope = make_runner(tmp_path)
    scope.fixture.prepare_error = EmbeddingUnavailableError(
        reason="insufficient_balance SECRET_PROVIDER_BODY"
    )

    with pytest.raises(StageCInfrastructureFailure) as captured:
        scope.runner.run()

    assert captured.value.classification == "dashscope_balance"
    assert captured.value.report["completion"] == {
        "case_count": 1,
        "expected_variants": 2,
        "completed_variants": 0,
        "infrastructure_failed_variants": 0,
        "missing_variants": 2,
        "completed_pairs": 0,
        "incomplete_pairs": [
            {
                "case_id": "case-1",
                "missing_variants": ["baseline", "adaptive"],
                "infrastructure_failed_variants": [],
            }
        ],
    }
    assert "SECRET_PROVIDER_BODY" not in json.dumps(captured.value.report)
    assert (tmp_path / "checkpoint.json").is_file()


def test_runner_correlates_exact_conversation_with_trusted_tenant(tmp_path):
    scope = make_runner(tmp_path)

    report = scope.runner.run()
    run_id = report["run"]["run_id"]

    expected = [
        (
            "trusted-org-a",
            f"stage-c:{run_id}:case-1:{variant}:attempt-1",
        )
        for variant in ("baseline", "adaptive")
    ]
    assert scope.store.reads == expected
    assert [call["organization_id"] for call in scope.baseline.calls + scope.adaptive.calls] == [
        "trusted-org-a",
        "trusted-org-a",
    ]


def test_resume_restores_fixture_skips_success_and_retries_failure(tmp_path):
    scope = make_runner(tmp_path)
    scope.adaptive.result = failed_result(
        StageCVariant.ADAPTIVE,
        SearchInternalError(internal_reason="ProviderError;status=500;code=server_error"),
    )
    with pytest.raises(StageCInfrastructureFailure):
        scope.runner.run()

    scope.baseline.calls.clear()
    scope.adaptive.calls.clear()
    scope.adaptive.result = completed_result(StageCVariant.ADAPTIVE)
    report = scope.runner.run()

    assert scope.fixture.prepare_calls == 1
    assert scope.fixture.restore_calls == 1
    assert scope.baseline.calls == []
    assert scope.adaptive.calls[0]["conversation_id"].endswith(
        ":case-1:adaptive:attempt-2"
    )
    assert report["completion"]["completed_variants"] == 2


def test_missing_event_is_checkpointed_then_stops_and_restores_scenario(tmp_path):
    scope = make_runner(tmp_path)
    scope.baseline.record_event = False

    with pytest.raises(StageCInfrastructureFailure) as captured:
        scope.runner.run()

    state = scope.checkpoint.load(metadata())
    failed = state.result("case-1", StageCVariant.BASELINE)
    assert failed is not None and failed.status == "infrastructure_failed"
    assert failed.payload["error_code"] == "unexpected_error"
    assert scope.adaptive.calls == []
    assert scope.fixture.state == "restored"
    assert captured.value.report["completion"]["infrastructure_failed_variants"] == 1


def test_corrupt_event_is_checkpointed_with_same_infrastructure_semantics(tmp_path):
    scope = make_runner(tmp_path)
    scope.baseline.event_factory = lambda variant, organization_id, conversation_id: replace(
        make_event(variant, organization_id, conversation_id),
        candidate_json="{provider body is not valid JSON",
    )

    with pytest.raises(StageCInfrastructureFailure):
        scope.runner.run()

    state = scope.checkpoint.load(metadata())
    failed = state.result("case-1", StageCVariant.BASELINE)
    assert failed is not None and failed.status == "infrastructure_failed"
    assert failed.payload["error_code"] == "unexpected_error"
    assert "provider body" not in json.dumps(state.to_dict())


def test_event_with_wrong_correlation_is_treated_as_missing(tmp_path):
    scope = make_runner(tmp_path)
    scope.baseline.event_factory = lambda variant, organization_id, conversation_id: replace(
        make_event(variant, organization_id, conversation_id),
        organization_id="untrusted-org-from-event",
        conversation_id="wrong-conversation",
    )

    with pytest.raises(StageCInfrastructureFailure):
        scope.runner.run()

    state = scope.checkpoint.load(metadata())
    failed = state.result("case-1", StageCVariant.BASELINE)
    assert failed is not None and failed.status == "infrastructure_failed"


@pytest.mark.parametrize(
    ("variant", "error", "classification"),
    [
        (
            StageCVariant.BASELINE,
            VectorStoreUnavailableError(reason="503 raw gateway body SECRET"),
            "qdrant_503",
        ),
        (
            StageCVariant.ADAPTIVE,
            SearchInternalError(
                internal_reason="ProviderError;status=402;code=insufficient_balance"
            ),
            "deepseek_balance",
        ),
    ],
)
def test_provider_or_vector_failure_stops_after_its_checkpoint(
    tmp_path, variant, error, classification
):
    scope = make_runner(tmp_path, cases=(make_case("case-1"), make_case("case-2")))
    service = scope.baseline if variant is StageCVariant.BASELINE else scope.adaptive
    service.result = failed_result(variant, error)

    with pytest.raises(StageCInfrastructureFailure) as captured:
        scope.runner.run()

    state = scope.checkpoint.load(metadata())
    failed = state.result("case-1", variant)
    assert failed is not None and failed.status == "infrastructure_failed"
    assert captured.value.classification == classification
    assert all("case-2" not in call["conversation_id"] for call in scope.baseline.calls)
    assert all("case-2" not in call["conversation_id"] for call in scope.adaptive.calls)
    assert "SECRET" not in json.dumps(state.to_dict())


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            EmbeddingUnavailableError(reason="insufficient_balance SECRET"),
            "dashscope_balance",
        ),
        (
            SearchInternalError(
                internal_reason="ProviderError;status=402;code=insufficient_balance"
            ),
            "deepseek_balance",
        ),
        (SearchInternalError(internal_reason="ProviderError;status=500"), "deepseek_provider_failure"),
        (VectorStoreUnavailableError(reason="503 SECRET response"), "qdrant_503"),
        (RuntimeError("SECRET"), "generic"),
    ],
)
def test_failure_classification_exposes_only_a_stable_label(error, expected):
    classification = classify_infrastructure_failure(error)

    assert classification == expected
    assert "SECRET" not in classification


def test_production_builder_uses_real_settings_constants_and_service_graph(
    tmp_path, monkeypatch
):
    from app.evals.stage_c import runner as runner_module

    cases_path = tmp_path / "cases.jsonl"
    cases_path.write_text("{}\n", encoding="utf-8")
    corpus_path = tmp_path / "document.md"
    corpus_path.write_text("# fixture", encoding="utf-8")
    case = make_case()
    spec = SimpleNamespace(path=corpus_path)
    settings = KnowledgeSettings(
        dashscope_api_key="not-persisted",
        dashscope_base_url="https://dashscope.invalid",
        qdrant_url="http://qdrant.invalid",
        qdrant_collection="real-collection",
        embedding_model="text-embedding-v4",
        embedding_dimensions=1024,
        min_fused_score=0.25,
        search_timeout_seconds=12.0,
    )
    services = SimpleNamespace(
        store=object(), ingestion=object(), baseline=object(), adaptive=object()
    )
    captured = {}

    monkeypatch.setattr(runner_module, "get_knowledge_settings", lambda: settings)
    monkeypatch.setattr(runner_module, "load_stage_c_cases", lambda path: (case,))
    monkeypatch.setattr(runner_module, "corpus_specs", lambda root: (spec,))
    monkeypatch.setattr(runner_module, "_git_revision", lambda root: "real-head")
    monkeypatch.setattr(
        runner_module,
        "_ensure_fixture_tenants",
        lambda path: captured.setdefault("tenant_database", Path(path)),
    )

    def create_services(database_path, received_settings, *, model_name):
        captured["factory"] = (Path(database_path), received_settings, model_name)
        return services

    monkeypatch.setattr(runner_module, "create_knowledge_services", create_services)
    monkeypatch.setattr(
        runner_module,
        "StageCFixtureManager",
        lambda **kwargs: captured.setdefault("fixture_kwargs", kwargs) or object(),
    )

    class CapturingRunner:
        def __init__(self, **kwargs):
            captured["runner"] = kwargs

        def run(self):
            return {"assembled": True}

    monkeypatch.setattr(runner_module, "StageCRetrievalEvalRunner", CapturingRunner)

    report = run_stage_c_retrieval_eval(
        database_path=tmp_path / "state.db",
        cases_path=cases_path,
        checkpoint_path=tmp_path / "checkpoint.json",
        repo_root=tmp_path,
    )

    assert report == {"assembled": True}
    assert captured["factory"] == (
        tmp_path / "state.db",
        settings,
        runner_module.MODEL_NAME,
    )
    assert captured["tenant_database"] == tmp_path / "state.db"
    run_metadata = captured["runner"]["metadata"]
    assert run_metadata.git_revision == "real-head"
    assert run_metadata.collection_name == "real-collection"
    assert run_metadata.embedding_model == "text-embedding-v4"
    assert run_metadata.planner_prompt_version == runner_module.PLANNER_PROMPT_VERSION
    assert run_metadata.assessor_prompt_version == runner_module.ASSESSOR_PROMPT_VERSION
    assert run_metadata.max_round_queries == runner_module.MAX_ROUND_QUERIES
    assert run_metadata.run_id
    assert run_metadata.created_at
    assert "not-persisted" not in json.dumps(run_metadata.to_dict())
