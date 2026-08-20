# Stage C Baseline/Adaptive Retrieval Evaluation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and run a resumable 48-case retrieval-level comparison that uses the real Baseline, Adaptive, DashScope, DeepSeek, and tenant-shared Qdrant paths.

**Architecture:** Keep the Legacy 16-case runner unchanged and add a focused `app.evals.stage_c` package plus a thin CLI. The package strictly parses cases, prepares real tenant/version states, normalizes both production retrieval results through persisted retrieval events, scores a common Top-5 surface, and atomically checkpoints each case/variant.

**Tech Stack:** Python 3.10+, Pydantic 2.11, pytest 9, SQLite/Alembic, DashScope `text-embedding-v4`, Qdrant client 1.18+, DeepSeek OpenAI-compatible client.

**Spec:** `docs/superpowers/specs/2026-08-21-stage-c-retrieval-eval-design.md`

## Global Constraints

- Leave `evals/knowledge/cases.jsonl`, `evals/knowledge/schema.json`, and `scripts/run_knowledge_baseline_eval.py` behavior unchanged.
- Score both variants on the first 5 final citations; retain Adaptive's sixth citation only as diagnostics.
- Use trusted Eval assembly for `organization_id`; never accept it from a case question or model output.
- Store no API keys, provider tokens, full extra document bodies, or model reasoning in artifacts.
- Treat provider/Qdrant failures as infrastructure failures, never as empty retrieval or insufficient evidence.
- Enforce server limits: at most 2 rounds, 3 first-round queries, 2 second-round queries, 1 planner call, 2 assessor calls, and 3 structured model calls.
- Preserve unrelated dirty-worktree changes, especially the existing modification to `app/knowledge/chunking.py`.

---

### Task 1: Strict Stage C Case Models and Loader

**Files:**
- Create: `app/evals/__init__.py`
- Create: `app/evals/stage_c/__init__.py`
- Create: `app/evals/stage_c/models.py`
- Create: `tests/evals/stage_c/test_models.py`

**Interfaces:**
- Consumes: `evals/knowledge/stage_c/cases.jsonl`.
- Produces: `StageCCase`, nested immutable Pydantic models, enums for category/scenario/behavior/strategy/variant, and `load_stage_c_cases(path: str | Path) -> tuple[StageCCase, ...]`.

- [ ] **Step 1: Write failing tests for valid parsing and strict rejection**

```python
def test_loads_all_stage_c_cases_strictly():
    cases = load_stage_c_cases(CASES_PATH)
    assert len(cases) == 48
    assert {case.category for case in cases} == set(StageCCategory)


def test_unknown_top_level_field_reports_jsonl_line(tmp_path):
    raw = json.loads(CASES_PATH.read_text(encoding="utf-8").splitlines()[0])
    raw["organization_id"] = "model-controlled"
    path = tmp_path / "cases.jsonl"
    path.write_text(json.dumps(raw, ensure_ascii=False) + "\n", encoding="utf-8")
    with pytest.raises(StageCCaseLoadError, match="line 1"):
        load_stage_c_cases(path)
```

Also test duplicate `case_id`, invalid scenario/category/behavior, duplicate fact/group IDs, dangling `supports_fact_ids`, preferred strategy outside `allowed`, business context on non-mixed cases, missing business context on mixed cases, and `answer_grounded` without evidence.

- [ ] **Step 2: Run the model tests and verify RED**

Run: `python -m pytest tests/evals/stage_c/test_models.py -q`

Expected: collection/import failure because `app.evals.stage_c.models` does not exist.

- [ ] **Step 3: Implement strict immutable models and the line-aware JSONL loader**

```python
class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, str_strip_whitespace=True
    )


class EvidenceGroup(StrictModel):
    group_id: str = Field(min_length=1)
    supports_fact_ids: tuple[str, ...] = Field(min_length=1)
    any_of: tuple[EvidenceAlternative, ...] = Field(min_length=1)


class StageCCase(StrictModel):
    case_id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    category: StageCCategory
    scenario: StageCScenario
    tenant_key: TenantKey
    question: str = Field(min_length=1)
    reference_answer: str = Field(min_length=1)
    key_answer_facts: tuple[KeyAnswerFact, ...]
    required_evidence_groups: tuple[EvidenceGroup, ...]
    should_have_answer: bool
    expected_behavior: ExpectedBehavior
    strategy_expectation: StrategyExpectation | None
    business_context: BusinessContext | None
    forbidden_tenant_keys: tuple[TenantKey, ...]
    security_expectations: SecurityExpectations | None
    notes: str
```

Use an `after` validator for cross-field rules. `BusinessContext` strictly validates its four top-level keys (`as_of_date`, `required_tools`, `order`, `logistics`) while retaining order/logistics facts as JSON-value dictionaries because Task 2 does not interpret business facts.

- [ ] **Step 4: Run the model tests and verify GREEN**

Run: `python -m pytest tests/evals/stage_c/test_models.py -q`

Expected: all tests pass and the committed 48-case file parses.

- [ ] **Step 5: Commit Task 1**

```text
git add app/evals tests/evals/stage_c/test_models.py
git commit -m "feat: add strict stage c eval case models"
```

### Task 2: Stable Identity Mapping and Top-5 Evidence Scoring

**Files:**
- Create: `app/evals/stage_c/scoring.py`
- Create: `tests/evals/stage_c/test_scoring.py`

**Interfaces:**
- Consumes: `StageCCase` plus generated document/version/chunk identities.
- Produces: `ChunkIdentity`, `IdentityIndex`, `StableCitation`, `VariantMetrics`, `normalize_citations(...)`, `score_variant(...)`, and `aggregate_metrics(...)`.

- [ ] **Step 1: Write failing tests for AND/OR groups and exact heading matching**

```python
def test_groups_are_and_and_any_of_is_or(stage_case):
    citations = (
        citation("org_a", "returns_exchange", "退货与换货政策/1.1 退货时限（按会员等级）"),
        citation("org_a", "vip", "VIP会员权益/3.1 无理由退货期限延长"),
    )
    metrics = score_variant(stage_case, citations, top_k=5)
    assert metrics.covered_group_count == 1
    assert metrics.required_group_count == 2
    assert metrics.evidence_group_recall == 0.5
    assert metrics.complete_evidence_coverage is False


def test_sixth_adaptive_citation_does_not_change_primary_metrics(stage_case):
    citations = tuple(irrel(i) for i in range(5)) + (relevant(),)
    metrics = score_variant(stage_case, citations, top_k=5)
    assert metrics.returned_full_count == 6
    assert metrics.relevant_top5_count == 0
```

Also test exact full-path match, exact final-segment match, substring/prefix rejection, duplicate equivalent sources covering one group once, `null + reason` for no-golden-evidence quality metrics, and precision from exact integer counts.

- [ ] **Step 2: Run scoring tests and verify RED**

Run: `python -m pytest tests/evals/stage_c/test_scoring.py -q`

Expected: import failure for the missing scoring module.

- [ ] **Step 3: Implement identity resolution and pure metric functions**

```python
def heading_matches(returned: str, expected: str) -> bool:
    return returned == expected or returned.endswith("/" + expected)


def score_variant(case: StageCCase, citations: Sequence[StableCitation], *, top_k: int = 5) -> VariantMetrics:
    evaluated = tuple(citations[:top_k])
    covered_ids = {
        group.group_id
        for group in case.required_evidence_groups
        if any(citation_matches_group(item, group) for item in evaluated)
    }
    relevant_count = sum(
        any(citation_matches_group(item, group) for group in case.required_evidence_groups)
        for item in evaluated
    )
    return VariantMetrics.from_counts(case, citations, evaluated, covered_ids, relevant_count)
```

`IdentityIndex.resolve(chunk_id)` must return the true tenant/document/version identity or an explicit unknown identity; unknown IDs are safety failures, not silently dropped citations.

- [ ] **Step 4: Run scoring tests and verify GREEN**

Run: `python -m pytest tests/evals/stage_c/test_scoring.py -q`

Expected: all pure metric tests pass.

- [ ] **Step 5: Commit Task 2**

```text
git add app/evals/stage_c/scoring.py tests/evals/stage_c/test_scoring.py
git commit -m "feat: score stage c evidence groups and safety"
```

### Task 3: Retrieval Event Read Seam and Content-Free Adaptive Query Trace

**Files:**
- Modify: `app/knowledge/base.py`
- Modify: `app/knowledge/sqlite_store.py`
- Modify: `app/knowledge/service.py`
- Modify: `app/knowledge/planning.py`
- Modify: `app/knowledge/evidence.py`
- Modify: `app/knowledge/structured_llm.py`
- Modify: `tests/knowledge/test_domain.py`
- Modify: `tests/knowledge/test_knowledge_store.py`
- Modify: `tests/knowledge/test_adaptive_service.py`
- Modify: `tests/knowledge/test_structured_llm.py`

**Interfaces:**
- Produces: `KnowledgeStore.get_retrieval_event(organization_id: str, conversation_id: str) -> RetrievalEvent | None`.
- Produces: `PLANNER_PROMPT_VERSION`, `ASSESSOR_PROMPT_VERSION`, and adaptive trace schema 4 with `queries: [{round, query_index, query_digest}]`.
- Produces: an internal-only sanitized provider diagnostic on `SearchInternalError`; public serialization remains the stable safe message.

- [ ] **Step 1: Write a failing tenant-scoped retrieval-event read test**

```python
def test_get_retrieval_event_is_scoped_by_tenant(two_tenant_knowledge_store):
    store, org_a, org_b = two_tenant_knowledge_store
    event = retrieval_event(org_a.organization_id, conversation_id="eval-case:adaptive")
    store.record_retrieval_event(organization_id=org_a.organization_id, event=event)
    assert store.get_retrieval_event(
        organization_id=org_a.organization_id,
        conversation_id="eval-case:adaptive",
    ) == event
    assert store.get_retrieval_event(
        organization_id=org_b.organization_id,
        conversation_id="eval-case:adaptive",
    ) is None
```

- [ ] **Step 2: Write a failing adaptive trace test for queries with zero candidates**

```python
def test_trace_counts_every_query_even_when_no_candidates(adaptive_scope):
    adaptive_scope.planner.next_plan = multi_plan(("q1", "q2"))
    adaptive_scope.assessor.decisions = [insufficient_no_followups()]
    result = adaptive_scope.service.search(organization_id="org-a", question="complex")
    assert result.retrieval_trace["queries"] == [
        {"round": 1, "query_index": 1, "query_digest": query_digest("q1")},
        {"round": 1, "query_index": 2, "query_digest": query_digest("q2")},
    ]
```

Assert the serialized trace contains no raw `q1`, `q2`, question text, tenant IDs, or reasoning.

Add a structured-client test where a fake provider raises an exception carrying `status_code=402` and `code="insufficient_balance"`. Assert `SearchInternalError.internal_reason` retains only exception type/status/code while `safe_message` remains `knowledge search could not be completed`.

- [ ] **Step 3: Run focused production tests and verify RED**

Run: `python -m pytest tests/knowledge/test_domain.py tests/knowledge/test_knowledge_store.py tests/knowledge/test_adaptive_service.py tests/knowledge/test_structured_llm.py -q`

Expected: missing store method and missing `queries` trace failures.

- [ ] **Step 4: Implement the store read and trace metadata**

Map every retrieval-event column back into the existing frozen `RetrievalEvent`. Query with both trusted keys and newest-first ordering:

```sql
SELECT * FROM retrieval_events
WHERE organization_id = ? AND conversation_id = ?
ORDER BY created_at DESC, id DESC
LIMIT 1
```

Append a query record before each `_retriever.retrieve(...)` call and publish it only as SHA-256 digest metadata. Define explicit prompt versions next to their prompt constants. Do not change planner, assessor, selection, or budget behavior.

When the OpenAI-compatible provider raises, convert only exception class, numeric status, and provider error code into `SearchInternalError.internal_reason`. Keep that field out of `public_dict()`, retrieval events, checkpoints, and reports; the CLI may use it solely to distinguish an insufficient-balance handoff from a generic DeepSeek failure.

- [ ] **Step 5: Run focused production tests and verify GREEN**

Run: `python -m pytest tests/knowledge/test_domain.py tests/knowledge/test_knowledge_store.py tests/knowledge/test_adaptive_service.py tests/knowledge/test_structured_llm.py -q`

Expected: all focused tests pass.

- [ ] **Step 6: Commit Task 3**

```text
git add app/knowledge/base.py app/knowledge/sqlite_store.py app/knowledge/service.py app/knowledge/planning.py app/knowledge/evidence.py app/knowledge/structured_llm.py tests/knowledge/test_domain.py tests/knowledge/test_knowledge_store.py tests/knowledge/test_adaptive_service.py tests/knowledge/test_structured_llm.py
git commit -m "feat: expose tenant-scoped retrieval diagnostics"
```

### Task 4: Real Corpus Assembly, Heading Preflight, and Scenario Restoration

**Files:**
- Create: `app/evals/stage_c/fixtures.py`
- Create: `tests/evals/stage_c/test_fixtures.py`

**Interfaces:**
- Produces: `CorpusDocumentSpec`, `FixtureManifest`, `StageCFixtureManager.prepare(...)`, `validate_golden_headings(...)`, `build_identity_index(...)`, and `StageCFixtureManager.scenario(case)` context manager.

- [ ] **Step 1: Write failing tests for the exact 8+3 corpus map and heading preflight**

```python
def test_corpus_uses_all_real_org_a_and_conflicting_org_b_documents():
    specs = corpus_specs(REPO_ROOT)
    assert {s.document_key for s in specs if s.tenant_key == "org_a"} == {
        "general_service", "returns_exchange", "refunds", "logistics",
        "compensation", "warranty", "order_changes", "vip",
    }
    assert {s.document_key for s in specs if s.tenant_key == "org_b"} == {
        "returns_exchange", "compensation", "warranty",
    }


def test_preflight_rejects_unmapped_heading(valid_cases, corpus_specs):
    broken = replace_first_heading(valid_cases, "not/a/real/heading")
    with pytest.raises(GoldenEvidenceMappingError, match="case_id"):
        validate_golden_headings(broken, corpus_specs)
```

- [ ] **Step 2: Write failing tests for old-version and disable restoration**

```python
def test_returns_old_version_is_indexed_but_inactive(prepared_fixture):
    returns = prepared_fixture.document("org_a", "returns_exchange")
    assert returns.old_version_id != returns.active_version_id
    assert prepared_fixture.identity_index.version(returns.old_version_id).is_active is False


def test_disabled_scenario_restores_even_after_error(prepared_fixture, disabled_case):
    with pytest.raises(RuntimeError):
        with prepared_fixture.scenario(disabled_case):
            assert prepared_fixture.status("org_a", "returns_exchange") == "disabled"
            assert prepared_fixture.status("org_a", "vip") == "disabled"
            raise RuntimeError("provider failed")
    assert prepared_fixture.status("org_a", "returns_exchange") == "active"
    assert prepared_fixture.status("org_a", "vip") == "active"
```

- [ ] **Step 3: Run fixture tests and verify RED**

Run: `python -m pytest tests/evals/stage_c/test_fixtures.py -q`

Expected: import failure for the missing fixture module.

- [ ] **Step 4: Implement corpus specs and resumable real ingestion**

Use stable document titles per tenant and manifest IDs. For org A returns, derive the old fixture by replacing the exact current row `| 普通会员 | 7 天 |` with `| 普通会员 | 10 天 |`; assert exactly one replacement, ingest it first, then ingest the unmodified current bytes as the active version. Recompute deterministic chunk IDs through the current Loader/Chunker using receipt IDs so the identity map covers active and inactive points without global tenant reads.

The scenario context manager changes only `document_disabled`; all other scenarios use prepared real state. It records prior status and restores it in `finally`.

- [ ] **Step 5: Run fixture tests and verify GREEN**

Run: `python -m pytest tests/evals/stage_c/test_fixtures.py -q`

Expected: corpus, mapping, inactive version, tenant identity, and restoration tests pass with fakes/no network.

- [ ] **Step 6: Commit Task 4**

```text
git add app/evals/stage_c/fixtures.py tests/evals/stage_c/test_fixtures.py
git commit -m "feat: assemble stage c tenant scenarios"
```

### Task 5: Fingerprinted Atomic Checkpoint and Run Metadata

**Files:**
- Create: `app/evals/stage_c/checkpoint.py`
- Create: `tests/evals/stage_c/test_checkpoint.py`

**Interfaces:**
- Produces: `RunMetadata`, `FixtureManifestState`, `CheckpointState`, `CheckpointStore`, `sha256_file(...)`, `sha256_corpus(...)`, and `build_run_fingerprint(...)`.

- [ ] **Step 1: Write failing tests for reuse, retry, and invalidation**

```python
def test_success_is_reused_but_failed_variant_is_retried(tmp_path):
    store = CheckpointStore(tmp_path / "checkpoint.json")
    state = store.initialize(metadata("fingerprint-a"))
    store.record(success("case-1", "baseline", attempt=1))
    store.record(failed("case-1", "adaptive", attempt=1))
    resumed = store.load(metadata("fingerprint-a"))
    assert resumed.should_run("case-1", Variant.BASELINE) is False
    assert resumed.next_attempt("case-1", Variant.ADAPTIVE) == 2


def test_fingerprint_change_invalidates_old_success(tmp_path):
    store = CheckpointStore(tmp_path / "checkpoint.json")
    store.initialize(metadata("old"))
    store.record(success("case-1", "baseline", attempt=1))
    state = store.initialize(metadata("new"))
    assert state.should_run("case-1", Variant.BASELINE) is True
    assert state.invalidated_previous_fingerprint == "old"
```

Also patch `os.replace` to prove writes occur through a same-directory temporary file, and assert raw checkpoint text contains no configured API key or document body canary beyond stable corpus hashes.

- [ ] **Step 2: Run checkpoint tests and verify RED**

Run: `python -m pytest tests/evals/stage_c/test_checkpoint.py -q`

Expected: import failure for the missing checkpoint module.

- [ ] **Step 3: Implement canonical metadata hashing and atomic persistence**

```python
def build_run_fingerprint(metadata_without_fingerprint: Mapping[str, object]) -> str:
    encoded = json.dumps(
        metadata_without_fingerprint,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
```

Include Git revision, dataset/corpus hashes, Loader/Chunker versions, embedding model/dimensions, collection and trace schema, planner/assessor model and prompt versions, Top-K/prefetch/token/score/timeout constants, and Stage C runner schema. Write JSON with `flush + os.fsync + os.replace`.

- [ ] **Step 4: Run checkpoint tests and verify GREEN**

Run: `python -m pytest tests/evals/stage_c/test_checkpoint.py -q`

Expected: all checkpoint tests pass.

- [ ] **Step 5: Commit Task 5**

```text
git add app/evals/stage_c/checkpoint.py tests/evals/stage_c/test_checkpoint.py
git commit -m "feat: checkpoint stage c retrieval runs"
```

### Task 6: Baseline/Adaptive Result Normalization and Aggregate Report

**Files:**
- Create: `app/evals/stage_c/reporting.py`
- Create: `tests/evals/stage_c/test_reporting.py`

**Interfaces:**
- Produces: `normalize_result(case, variant, result, event, identity_index, attempt) -> VariantResult` and `compile_stage_c_report(cases, checkpoint) -> dict`.

- [ ] **Step 1: Write failing normalization tests**

```python
def test_adaptive_normalization_uses_persisted_event_counts(case, identity_index):
    normalized = normalize_result(
        case=case,
        variant=Variant.ADAPTIVE,
        result=adaptive_result(strategy="multi", citations=six_citations()),
        event=event(rounds=2, model_calls=3, tokens=321, queries=(3, 2)),
        identity_index=identity_index,
        attempt=1,
    )
    assert normalized.round_count == 2
    assert normalized.query_count_by_round == (3, 2)
    assert normalized.model_calls == 3
    assert normalized.evaluated_citation_count == 5
    assert normalized.full_citation_count == 6


def test_vector_failure_is_not_scored_as_empty(case, identity_index):
    normalized = normalize_result(
        case=case,
        variant=Variant.BASELINE,
        result=failed_result("VECTOR_STORE_UNAVAILABLE"),
        event=failed_event(),
        identity_index=identity_index,
        attempt=1,
    )
    assert normalized.status == "infrastructure_failed"
    assert normalized.metrics is None
```

Also test insufficient evidence as a completed retrieval outcome, budget-exceeded as a completed bounded outcome with violation metrics, unknown/cross-tenant/inactive/disabled IDs, strategy allowed/preferred, category aggregates, exact integer denominators, and incomplete-pair reporting.

- [ ] **Step 2: Run reporting tests and verify RED**

Run: `python -m pytest tests/evals/stage_c/test_reporting.py -q`

Expected: import failure for the missing reporting module.

- [ ] **Step 3: Implement normalized artifacts and exact aggregates**

Derive per-round query counts from trace schema 4's `queries`. Baseline always records one round/one query/zero model calls. Map every citation and trace candidate through `IdentityIndex`; do not discard unknown IDs. Aggregate with integer `covered/required` and `relevant/returned` totals, exposing `null` with `scope_reason` when a denominator is not measurable.

- [ ] **Step 4: Run reporting tests and verify GREEN**

Run: `python -m pytest tests/evals/stage_c/test_reporting.py -q`

Expected: all normalization and aggregation tests pass.

- [ ] **Step 5: Commit Task 6**

```text
git add app/evals/stage_c/reporting.py tests/evals/stage_c/test_reporting.py
git commit -m "feat: report stage c retrieval comparisons"
```

### Task 7: Resumable Orchestrator and Thin CLI

**Files:**
- Create: `app/evals/stage_c/runner.py`
- Create: `scripts/run_stage_c_retrieval_eval.py`
- Create: `tests/evals/stage_c/test_runner.py`
- Create: `tests/evals/stage_c/test_cli.py`

**Interfaces:**
- Produces: `StageCRetrievalEvalRunner.run() -> dict`, `run_stage_c_retrieval_eval(...) -> dict`, and CLI arguments `--database`, `--cases`, `--checkpoint`, `--output`.

- [ ] **Step 1: Write a failing fake-provider orchestration test**

```python
def test_runner_checkpoints_each_variant_and_keeps_same_scenario_state(fake_scope):
    report = fake_scope.runner.run()
    assert fake_scope.calls[:2] == [
        ("case-1", "baseline", fake_scope.active_versions_snapshot),
        ("case-1", "adaptive", fake_scope.active_versions_snapshot),
    ]
    assert fake_scope.checkpoint.write_keys == [
        "case-1:baseline", "case-1:adaptive"
    ]
    assert report["completion"]["completed_variants"] == 2
```

- [ ] **Step 2: Write failing recovery and infrastructure-stop tests**

```python
def test_runner_stops_after_checkpointing_provider_failure(fake_scope):
    fake_scope.adaptive_result = provider_failure("SEARCH_INTERNAL_ERROR")
    with pytest.raises(StageCInfrastructureFailure):
        fake_scope.runner.run()
    state = fake_scope.checkpoint.reload()
    assert state.result("case-1", "baseline").status == "completed"
    assert state.result("case-1", "adaptive").status == "infrastructure_failed"


def test_resume_skips_success_and_retries_failure(fake_scope):
    fake_scope.seed_success("case-1", "baseline")
    fake_scope.seed_failure("case-1", "adaptive", attempt=1)
    fake_scope.runner.run()
    assert fake_scope.called_variants == [("case-1", "adaptive", 2)]
```

- [ ] **Step 3: Run runner/CLI tests and verify RED**

Run: `python -m pytest tests/evals/stage_c/test_runner.py tests/evals/stage_c/test_cli.py -q`

Expected: missing runner and CLI imports.

- [ ] **Step 4: Implement orchestration with production service injection**

For every runnable variant, generate `conversation_id = f"stage-c:{run_id}:{case_id}:{variant}:attempt-{attempt}"`, call the correct production service, then fetch the exact event through the tenant-scoped store method. Missing events are infrastructure failures. Record the normalized result immediately. Apply scenario state around both variants and restore in `finally`.

The production builder uses `get_knowledge_settings()`, `MODEL_NAME`, and `create_knowledge_services(...)`. The CLI writes the final report atomically and exits nonzero after any checkpointed infrastructure failure. It prints a concise classification for DashScope balance errors, DeepSeek provider failures, and Qdrant 503 without placing raw provider bodies into JSON artifacts.

- [ ] **Step 5: Run runner/CLI tests and verify GREEN**

Run: `python -m pytest tests/evals/stage_c/test_runner.py tests/evals/stage_c/test_cli.py -q`

Expected: orchestration, recovery, event correlation, CLI defaults, and nonzero failure behavior pass.

- [ ] **Step 6: Run all Stage C tests**

Run: `python -m pytest tests/evals/stage_c -q`

Expected: all Stage C tests pass without network access.

- [ ] **Step 7: Commit Task 7**

```text
git add app/evals/stage_c/runner.py scripts/run_stage_c_retrieval_eval.py tests/evals/stage_c/test_runner.py tests/evals/stage_c/test_cli.py
git commit -m "feat: run resumable stage c retrieval eval"
```

### Task 8: Regression Verification and Real 48×2 Evaluation

**Files:**
- Runtime artifact: `.artifacts/stage-c-retrieval/checkpoint.json`
- Runtime artifact: `.artifacts/stage-c-retrieval/report.json`
- Runtime state: `.artifacts/stage-c-retrieval/state.db`
- Modify: `README.md`

**Interfaces:**
- Consumes: configured `DASHSCOPE_API_KEY`, `DEEPSEEK_API_KEY`, and Qdrant at the configured URL (expected `http://localhost:6333`).
- Produces: a complete 96-variant report or a resumable checkpoint with an explicit infrastructure failure.

- [ ] **Step 1: Run focused knowledge and legacy eval regressions**

Run: `python -m pytest tests/knowledge tests/evals/test_knowledge_eval.py -q`

Expected: all tests pass; Legacy runner behavior remains unchanged.

- [ ] **Step 2: Run the full test suite**

Run: `python -m pytest -q`

Expected: all tests pass with no new warnings/errors. If an unrelated pre-existing failure occurs, record it separately and prove the focused suites remain green.

- [ ] **Step 3: Run the real Stage C comparison**

```text
python scripts/run_stage_c_retrieval_eval.py --database .artifacts/stage-c-retrieval/state.db --cases evals/knowledge/stage_c/cases.jsonl --checkpoint .artifacts/stage-c-retrieval/checkpoint.json --output .artifacts/stage-c-retrieval/report.json
```

Expected: Qdrant/DashScope ingestion succeeds, then all 48 cases run through Baseline and Adaptive; the report shows `completed_variants = 96` and `infrastructure_failed_variants = 0`.

If DashScope or DeepSeek reports insufficient balance, stop after the checkpoint write and notify the user to recharge. If Qdrant returns 503/Bad Gateway, preserve the checkpoint and hand off the same command for retry after VPN shutdown.

- [ ] **Step 4: Validate the real artifact deterministically**

Run a read-only validator that loads `report.json` and asserts:

```python
assert report["completion"]["case_count"] == 48
assert report["completion"]["expected_variants"] == 96
assert report["completion"]["completed_variants"] == 96
assert report["completion"]["infrastructure_failed_variants"] == 0
assert report["aggregates"]["security"]["cross_tenant_leak_count"] == 0
assert report["aggregates"]["budget"]["hard_limit_violation_count"] == 0
```

Do not hide quality misses: retrieval recall, precision, strategy mismatch, inactive-version leakage, and disabled-document leakage remain in the final report exactly as measured.

- [ ] **Step 5: Add concise README usage**

Document the single run/resume command, output paths, Top-5 comparison rule, and infrastructure-failure semantics. Do not copy the full implementation design into README.

- [ ] **Step 6: Run final diff and artifact safety checks**

Run: `git diff --check`

Run tests that scan committed files and report/checkpoint schemas for API-key field names, model reasoning fields, and raw extra document bodies.

- [ ] **Step 7: Commit any README-only handoff change**

```text
git add README.md
git commit -m "docs: document stage c retrieval evaluation"
```
