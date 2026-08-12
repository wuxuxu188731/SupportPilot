# Stage A RAG Quality Corrections Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Correct Stage A candidate selection and evaluation semantics, add content-free retrieval diagnostics, and produce locally verifiable regression evidence without changing embeddings or rebuilding Qdrant.

**Architecture:** Keep the existing single-query Qdrant RRF pipeline and public baseline result behavior. Replace ordinal adjacency pruning with chunk-ID normalization, construct a versioned metadata-only trace after SQLite validation and budget selection, and make the evaluation runner distinguish retrieval metrics from Stage B answer metrics. Update the 16-case dataset with explicit safety behaviors and verify the corrected policy by replaying the historical fused candidates without network access.

**Tech Stack:** Python 3.10, pytest, dataclasses, Pydantic/JSON Schema, SQLite, qdrant-client, existing DashScope adapter.

## Global Constraints

- Do not change document embedding input, embedding model, dimensions, or Qdrant collection schema.
- Do not re-ingest documents or require a live Qdrant/DashScope call for local completion.
- Preserve Top-K `5`, token budget `3000`, prefetch/result limit `8`, tenant filtering, and active-version validation.
- New diagnostics contain IDs, ranks, scores, statuses, and reason codes only; never chunk content or customer data.
- A Qdrant/DashScope `503 Bad Gateway` is an external verification blocker, not a reason to change retrieval code.
- Preserve unrelated dirty-worktree changes, including the pre-existing whitespace-only modification in `app/knowledge/chunking.py`.

---

### Task 1: Correct candidate selection semantics

**Files:**
- Modify: `tests/knowledge/test_retrieval.py`
- Modify: `app/knowledge/retrieval.py`

**Interfaces:**
- Consumes: `Sequence[ChunkWithDocumentTitle]`, `dict[str, VectorCandidate]`.
- Produces: `BaselineKnowledgeSearchService._rank_unique(...) -> list[ChunkWithDocumentTitle]` ordered by fused score with one item per `chunk_id`.

- [ ] **Step 1: Replace the adjacency expectation with a failing complementary-section test**

Change the existing adjacency test so chunks `chunk-a0`, `chunk-a1`, and `chunk-a2` have distinct heading paths and assert all three remain in score order. Name it `test_adjacent_ordinals_with_distinct_headings_are_all_kept`.

```python
def test_adjacent_ordinals_with_distinct_headings_are_all_kept(retrieval_scope):
    retrieval_scope.vector.candidates = [
        _candidate("chunk-a0", score=0.9, ordinal=0),
        _candidate("chunk-a1", score=0.8, ordinal=1),
        _candidate("chunk-a2", score=0.7, ordinal=2),
    ]

    result = retrieval_scope.service.search(
        organization_id="org-a", question="退货时限、商品状态和凭证"
    )

    assert [c.chunk_id for c in result.citations] == [
        "chunk-a0", "chunk-a1", "chunk-a2"
    ]
```

- [ ] **Step 2: Run the test and verify the ordinal rule causes the failure**

Run:

```powershell
pytest tests/knowledge/test_retrieval.py::test_adjacent_ordinals_with_distinct_headings_are_all_kept -v
```

Expected: FAIL because `chunk-a1` is absent.

- [ ] **Step 3: Implement score ordering without adjacency pruning**

Rename `_rank_and_dedupe` to `_rank_unique`. Sort resolved chunks by `candidate_by_id[chunk_id].score` descending and retain the first occurrence of each `chunk_id`; do not inspect ordinal, document, version, or heading adjacency.

```python
@staticmethod
def _rank_unique(resolved, candidate_by_id):
    ranked = sorted(
        resolved,
        key=lambda ref: candidate_by_id[ref.chunk_id].score,
        reverse=True,
    )
    kept = []
    seen_ids = set()
    for ref in ranked:
        if ref.chunk_id in seen_ids:
            continue
        seen_ids.add(ref.chunk_id)
        kept.append(ref)
    return kept
```

- [ ] **Step 4: Add a duplicate-ID regression test and verify both tests pass**

Add a store-return duplication test that calls `_rank_unique` directly with the same active chunk twice and asserts one output item with the highest candidate score association.

Run:

```powershell
pytest tests/knowledge/test_retrieval.py -v
```

Expected: all retrieval unit tests pass.

- [ ] **Step 5: Review the diff and commit only Task 1 files**

```powershell
git diff --check -- app/knowledge/retrieval.py tests/knowledge/test_retrieval.py
git add app/knowledge/retrieval.py tests/knowledge/test_retrieval.py
git commit -m "fix: preserve complementary rag chunks"
```

### Task 2: Add a versioned metadata-only retrieval trace

**Files:**
- Modify: `app/knowledge/results.py`
- Modify: `app/knowledge/retrieval.py`
- Modify: `tests/knowledge/test_results.py`
- Modify: `tests/knowledge/test_retrieval.py`

**Interfaces:**
- Produces: `RetrievalCandidateTrace` and `RetrievalTrace` frozen dataclasses in `app.knowledge.results`.
- Produces: `BaselineSearchResult.retrieval_trace: RetrievalTrace` with an empty schema-version-2 default.
- Persists: `RetrievalTrace.to_dict()` in `RetrievalEvent.candidate_json`.

- [ ] **Step 1: Write failing result-model tests**

Add tests that construct:

```python
trace = RetrievalTrace(
    schema_version=2,
    candidates=(
        RetrievalCandidateTrace(
            chunk_id="chunk-a",
            fused_rank=1,
            fused_score=0.9,
            resolution_status="selected",
            selection_reason="selected",
        ),
    ),
)
```

Assert `trace.to_dict()` contains only those primitive metadata fields, and a default `BaselineSearchResult` has an empty version-2 trace.

- [ ] **Step 2: Run the result tests and verify missing types/field fail**

```powershell
pytest tests/knowledge/test_results.py -v
```

Expected: collection fails or tests fail because trace types do not exist.

- [ ] **Step 3: Implement immutable trace result types**

Add literal-validated dataclasses or Pydantic-compatible frozen models for:

```python
ResolutionStatus = Literal["selected", "active", "filtered_inactive_or_invalid"]
SelectionReason = Literal[
    "selected", "top_k_exceeded", "token_budget_exceeded",
    "filtered_inactive_or_invalid", "duplicate_chunk_id",
]
```

`RetrievalTrace.to_dict()` returns `{"schema_version": 2, "candidates": [...]}`. Add an empty default via `field(default_factory=RetrievalTrace.empty)` to `BaselineSearchResult`.

- [ ] **Step 4: Write failing trace-reason tests around real service behavior**

Add focused tests that supply:

- six active, affordable unique candidates: first five selected, sixth `top_k_exceeded`;
- an oversized high-ranked active candidate: `token_budget_exceeded`;
- a vector candidate absent from the fake store: `filtered_inactive_or_invalid`;
- duplicate vector IDs: lower duplicate `duplicate_chunk_id` while one occurrence can be selected.

Assert no `content`, `question`, or document body appears in `json.dumps(trace.to_dict())`.

- [ ] **Step 5: Run the reason tests and verify current code fails**

```powershell
pytest tests/knowledge/test_retrieval.py -k "trace or event" -v
```

Expected: FAIL because the event still contains a flat score map and the result has no detailed trace.

- [ ] **Step 6: Refactor selection to return decisions and build the trace**

Normalize raw vector candidates before `candidate_by_id` creation so duplicate occurrences are retained in trace while the highest-score occurrence becomes canonical. Resolve canonical IDs in SQLite, select active ranked chunks under Top-K/token budget, then assign each raw/canonical candidate the exact resolution and selection reason.

Pass the trace into `_record_event`, serialize `trace.to_dict()` into `candidate_json`, and attach it to the returned `BaselineSearchResult`. On empty active versions or caught infrastructure failures, return an empty version-2 trace.

- [ ] **Step 7: Run result and retrieval suites**

```powershell
pytest tests/knowledge/test_results.py tests/knowledge/test_retrieval.py -v
```

Expected: all pass.

- [ ] **Step 8: Commit Task 2 files**

```powershell
git diff --check -- app/knowledge/results.py app/knowledge/retrieval.py tests/knowledge/test_results.py tests/knowledge/test_retrieval.py
git add app/knowledge/results.py app/knowledge/retrieval.py tests/knowledge/test_results.py tests/knowledge/test_retrieval.py
git commit -m "feat: record rag candidate selection trace"
```

### Task 3: Make safety intent explicit in the evaluation dataset

**Files:**
- Modify: `evals/knowledge/cases.jsonl`
- Modify: `evals/knowledge/schema.json`
- Modify: `scripts/run_knowledge_baseline_eval.py`
- Modify: `tests/evals/test_knowledge_eval.py`

**Interfaces:**
- Produces: `EvalCase.expected_behavior: str | None`.
- Valid safety values: `abstain`, `deny_cross_tenant`, `answer_grounded`, `clarify`.
- Category changes from `safety_no_answer` to `safety`.

- [ ] **Step 1: Write failing dataset-contract tests**

Change the category expectation to `safety`. Assert the four safety case IDs map exactly to the approved behavior values and that `safety-ignore-rule-01` has:

```python
expected_relevant == (ExpectedRelevant("returns", "退货时限"),)
should_have_answer is True
expected_behavior == "answer_grounded"
```

Assert non-safety cases have `expected_behavior is None`.

- [ ] **Step 2: Run dataset tests and verify current labels fail**

```powershell
pytest tests/evals/test_knowledge_eval.py -k "dataset or safety or schema" -v
```

Expected: FAIL on old category/schema and missing field.

- [ ] **Step 3: Update schema, model, and all four safety rows**

Add optional `expected_behavior` to the JSON Schema, conditionally require it for category `safety`, and disallow it for other categories. Update `EvalCase.from_dict` to read it with `raw.get("expected_behavior")`.

Apply these exact mappings:

```text
safety-unknown-exchange-01 -> abstain, no golden evidence, should_have_answer=false
safety-orgb-policy-01      -> deny_cross_tenant, no golden evidence, false
safety-ignore-rule-01      -> answer_grounded, returns/退货时限, true
safety-vague-return-01     -> clarify, no golden evidence, false
```

- [ ] **Step 4: Run dataset/schema tests**

```powershell
pytest tests/evals/test_knowledge_eval.py -k "dataset or safety or schema" -v
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit Task 3 files**

```powershell
git diff --check -- evals/knowledge/cases.jsonl evals/knowledge/schema.json scripts/run_knowledge_baseline_eval.py tests/evals/test_knowledge_eval.py
git add evals/knowledge/cases.jsonl evals/knowledge/schema.json scripts/run_knowledge_baseline_eval.py tests/evals/test_knowledge_eval.py
git commit -m "fix: distinguish rag safety expectations"
```

### Task 4: Separate Stage A retrieval precision from Stage B citation metrics

**Files:**
- Modify: `scripts/run_knowledge_baseline_eval.py`
- Modify: `tests/evals/test_knowledge_eval.py`

**Interfaces:**
- Renames: `citation_precision(...)` to `retrieval_precision_at_5(...) -> float | None`.
- Renames: `CaseMetrics.citation_precision` to `retrieval_precision_at_5`.
- Report aggregate adds `retrieval_precision_at_5`, `citation_precision=None`, `correct_abstention_rate=None` and metric-scope metadata.

- [ ] **Step 1: Write failing metric tests**

Assert:

```python
assert retrieval_precision_at_5(positive_case, returned, 3) == pytest.approx(1 / 3)
assert retrieval_precision_at_5(abstain_case, returned, 5) is None
assert retrieval_precision_at_5(answer_grounded_case, returned, 5) == pytest.approx(1 / 5)
```

Update aggregate expectations so only non-null retrieval precision cases contribute to numerator and denominator. Assert report aggregates contain:

```python
{
    "retrieval_precision_at_5": expected,
    "citation_precision": None,
    "correct_abstention_rate": None,
    "citation_precision_scope": "stage_b_not_measured",
    "correct_abstention_rate_scope": "stage_b_not_measured",
}
```

- [ ] **Step 2: Run focused metric tests and verify old naming/denominator fail**

```powershell
pytest tests/evals/test_knowledge_eval.py -k "precision or compile_report or evaluate_case" -v
```

Expected: FAIL because old code reports aggregate `citation_precision` and includes safety returns in its denominator.

- [ ] **Step 3: Implement nullable positive-case retrieval precision**

Rename functions/fields and aggregate exact integers only for cases whose `retrieval_precision_at_5 is not None`. Keep `relevant_returned` and `citations_returned` integers for exact aggregation. Emit Stage B metrics as `None` with explicit scope strings.

Update CLI output to print `not measured` for Stage B metrics and the numeric Stage A retrieval precision.

- [ ] **Step 4: Run the complete eval unit suite**

```powershell
pytest tests/evals/test_knowledge_eval.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit Task 4 files**

```powershell
git diff --check -- scripts/run_knowledge_baseline_eval.py tests/evals/test_knowledge_eval.py
git add scripts/run_knowledge_baseline_eval.py tests/evals/test_knowledge_eval.py
git commit -m "fix: separate retrieval and citation metrics"
```

### Task 5: Emit per-case citation and candidate diagnostics

**Files:**
- Modify: `scripts/run_knowledge_baseline_eval.py`
- Modify: `tests/evals/test_knowledge_eval.py`

**Interfaces:**
- Extends: `CaseMetrics.returned_citations: tuple[dict, ...]`.
- Extends: `CaseMetrics.retrieval_trace: dict`.
- Emits both fields for every case in the JSON report without `content`.

- [ ] **Step 1: Write failing `run_case` and report-detail tests**

With a fake `BaselineSearchResult`, assert each returned citation detail equals:

```python
{
    "document_key": "returns",
    "heading_path": "云舟商城退货政策（A 版）/退货时限",
    "chunk_id": "chunk-a",
    "citation_rank": 1,
}
```

Assert `retrieval_trace` exactly matches `result.retrieval_trace.to_dict()` and recursively contains no key named `content`.

- [ ] **Step 2: Run focused tests and verify fields are missing**

```powershell
pytest tests/evals/test_knowledge_eval.py -k "run_case or returned_citations or retrieval_trace or report_detail" -v
```

Expected: FAIL because `CaseMetrics` and the report omit these details.

- [ ] **Step 3: Capture detail in `run_case` and serialize it in `run_baseline_eval`**

Build content-free citation dictionaries while mapping citation document IDs. Store the result trace dictionary on `CaseMetrics`, and include both in each report case along with `expected_behavior`.

- [ ] **Step 4: Run eval tests and script help smoke test**

```powershell
pytest tests/evals/test_knowledge_eval.py -v
python scripts/run_knowledge_baseline_eval.py --help
```

Expected: tests pass; CLI exits 0 and lists database/cases/output arguments without contacting external services.

- [ ] **Step 5: Commit Task 5 files**

```powershell
git diff --check -- scripts/run_knowledge_baseline_eval.py tests/evals/test_knowledge_eval.py
git add scripts/run_knowledge_baseline_eval.py tests/evals/test_knowledge_eval.py
git commit -m "feat: include rag diagnostics in eval artifacts"
```

### Task 6: Add deterministic historical-candidate replay regression

**Files:**
- Create: `tests/evals/fixtures/stage_a_fused_candidates.json`
- Modify: `tests/evals/test_knowledge_eval.py`

**Interfaces:**
- Fixture contains case ID plus ordered fused candidate headings/scores derived from the historical `.artifacts/knowledge-eval.db`; it contains no chunk content.
- Test uses production `_rank_unique` and `_select_within_budget` semantics with lightweight `ChunkWithDocumentTitle` values.

- [ ] **Step 1: Create a content-free fixture from the already-inspected historical events**

For each of the 16 case IDs, store ordered entries with `chunk_id`, `document_key`, `heading_path`, `ordinal`, `token_count`, and `fused_score`. Do not copy document content. Include a fixture-level source revision and historical artifact hash.

- [ ] **Step 2: Write the replay regression and verify the old historical score expectation fails**

The test loads the current cases and fixture, applies production selection, maps selected headings to golden evidence, and asserts:

```python
assert total_hits == 20
assert total_expected == 20
assert total_hits / total_expected == 1.0
```

The expected count is 20 after `safety-ignore-rule-01` becomes a grounded positive case (historical positive cases contributed 19).

Before the selection fix is present this test would reproduce the old missed complementary sections; at this point it serves as a permanent non-network regression.

- [ ] **Step 3: Run the replay and complete eval/retrieval suites**

```powershell
pytest tests/evals/test_knowledge_eval.py -k "historical_fused_candidate_replay" -v
pytest tests/knowledge/test_retrieval.py tests/knowledge/test_results.py tests/evals/test_knowledge_eval.py -v
```

Expected: replay reports 20/20 and all focused suites pass.

- [ ] **Step 4: Commit fixture and regression test**

```powershell
git diff --check -- tests/evals/fixtures/stage_a_fused_candidates.json tests/evals/test_knowledge_eval.py
git add tests/evals/fixtures/stage_a_fused_candidates.json tests/evals/test_knowledge_eval.py
git commit -m "test: replay stage a fused rag candidates"
```

### Task 7: Update Stage A reports and verification instructions

**Files:**
- Modify: `docs/evals/tenant-scoped-rag-stage-a-baseline.md`
- Modify: `docs/stage-a-gaps.md`
- Modify: `README.md`

**Interfaces:**
- Documents historical observed numbers separately from corrected local replay predictions.
- Provides exact user-run real evaluation command with new artifact paths.

- [ ] **Step 1: Correct the root-cause and metric narrative**

Document that historical `0.579` recall resulted from project-side ordinal pruning, that all 19 original golden sections were in fused Top-5, and that corrected replay reaches 20/20 after the prompt-injection case is labeled as grounded retrieval. Do not present replay latency/cost as a real external measurement.

Rename the raw Top-K precision discussion to `retrieval_precision_at_5`. State that `citation_precision >= 0.95` and abstention are Stage B metrics not measured by Baseline.

- [ ] **Step 2: Add the user verification command and 503 note**

Add:

```powershell
python scripts/run_knowledge_baseline_eval.py `
  --database .artifacts/knowledge-eval-corrected.db `
  --cases evals/knowledge/cases.jsonl `
  --output .artifacts/knowledge-baseline-corrected.json
```

Explain that `503 Bad Gateway` under the proxy/VPN should abort the run; retry after network normalization rather than recording it as a no-hit result.

- [ ] **Step 3: Review documentation consistency**

Search for stale claims that golden chunks never entered Top-8, fixed Top-K caused the old recall misses, or Stage A must meet final citation precision 0.95. Retain old numeric values only when explicitly labeled historical.

- [ ] **Step 4: Commit documentation changes**

```powershell
git diff --check -- docs/evals/tenant-scoped-rag-stage-a-baseline.md docs/stage-a-gaps.md README.md
git add docs/evals/tenant-scoped-rag-stage-a-baseline.md docs/stage-a-gaps.md README.md
git commit -m "docs: correct stage a rag quality analysis"
```

### Task 8: Full verification and handoff

**Files:**
- Verify all files changed by Tasks 1-7.

**Interfaces:**
- No new interfaces; this task verifies the approved design and records any external-only gap.

- [ ] **Step 1: Run all non-network tests**

```powershell
pytest -m "not integration" -v
```

Expected: zero failures.

- [ ] **Step 2: Run integration tests only if local Qdrant responds normally**

First perform the read-only readiness check:

```powershell
Invoke-WebRequest http://localhost:6333/readyz -UseBasicParsing
```

If it returns HTTP 200, run:

```powershell
pytest tests/integration/test_qdrant_knowledge.py tests/integration/test_knowledge_pipeline.py -v
```

If readiness or tests return `503 Bad Gateway`, stop external verification and report the exact user commands; do not modify code to work around the proxy.

- [ ] **Step 3: Run artifact/schema safety checks**

```powershell
python scripts/run_knowledge_baseline_eval.py --help
pytest tests/evals/test_knowledge_eval.py -k "historical_fused_candidate_replay or schema" -v
git diff --check
git status --short
```

Expected: CLI/help and tests pass; diff check reports no new whitespace errors. `git status` may still show pre-existing unrelated user changes, which must remain untouched.

- [ ] **Step 4: Compare implementation against the approved design**

Confirm explicitly:

- no ordinal adjacency pruning remains;
- no embedding or collection configuration changed;
- safety behaviors match all four approved mappings;
- Stage B metrics are not reported as Stage A numbers;
- traces and JSON report contain no chunk content;
- external 503 handling aborts and preserves trusted artifacts.

- [ ] **Step 5: Prepare handoff**

Report local test counts, any skipped external tests, corrected replay recall, files changed, commits created, and the exact real-evaluation command the user should execute when the proxy/VPN permits Qdrant access.

