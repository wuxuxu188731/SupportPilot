# Stage A RAG Quality Corrections Design

## 1. Status and scope

- Date: 2026-08-12
- Status: approved for implementation
- Scope: correct the confirmed Stage A retrieval and evaluation defects without changing document embeddings or rebuilding the Qdrant collection.
- External verification: a real DashScope/Qdrant rerun is deferred to the user when the local proxy/VPN is not causing Qdrant `503 Bad Gateway` responses.

This change addresses four related problems:

1. project-side ordinal adjacency pruning removes complementary policy sections;
2. the final-answer `citation_precision >= 0.95` threshold is incorrectly applied to a fixed Top-K retriever;
3. the current `safety_no_answer` label mixes abstention, cross-tenant denial, grounded prompt-injection handling, and clarification;
4. the evaluation artifact does not contain the candidate details claimed by its report, making retrieval failures difficult to localize.

The following remain out of scope:

- changing the document embedding text to include title or heading metadata;
- re-ingesting documents or rebuilding the Qdrant collection;
- QueryPlanner, EvidenceAssessor, answer generation, or Stage B behavior;
- expanding the evaluation set from 16 to 48 cases;
- diagnosing proxy/VPN or external `503` failures.

## 2. Retrieval selection semantics

### 2.1 Remove ordinal adjacency pruning

`BaselineKnowledgeSearchService` must not treat adjacent ordinals as duplicates. Ordinal describes physical order in a document, not semantic equivalence or text overlap. Adjacent chunks with different headings frequently contain complementary evidence required by multi-condition questions.

After SQLite active-version validation, candidates are sorted by fused score descending. Candidate identity is `chunk_id`; duplicate IDs are collapsed by retaining their highest-scored occurrence. No candidate is dropped merely because another chunk from the same document/version has ordinal difference one.

The existing constraints remain unchanged:

- no more than five final chunks;
- no more than 3,000 total chunk tokens;
- final citation order follows fused candidate score;
- tenant and active-version validation occurs before selection.

Adjacent merging is not added in Stage A. It changes citation boundaries and token accounting and belongs in a separately evaluated retrieval policy.

### 2.2 Regression behavior

Tests must prove all of the following:

- adjacent ordinals under different headings are both retained;
- duplicate candidate IDs are returned at most once;
- score ordering, Top-K, token budget, tenant validation, and active-version validation still hold;
- the saved Stage A fused candidates replay to 19/19 golden-section hits when the corrected selection policy is applied.

## 3. Evaluation metric contract

### 3.1 Retrieval metrics

Stage A measures retrieval output, not citations actually used by an answer generator. The metric currently named `citation_precision` is renamed to `retrieval_precision_at_5`.

For positive cases with non-empty `expected_relevant`:

```text
retrieval_precision_at_5 = relevant returned chunks / returned chunks
```

No-answer and other safety cases do not contribute to aggregate retrieval precision because their correctness depends on routing, evidence assessment, or clarification behavior that Stage A does not implement. Their per-case retrieval precision is `null`.

`retrieval_recall_at_5` remains:

```text
unique expected sections returned / unique expected sections
```

and is aggregated by total golden-section count.

The generated report must explicitly declare:

- `citation_precision: null` with scope `stage_b_not_measured`;
- `correct_abstention_rate: null` with scope `stage_b_not_measured`;
- the MVP `citation_precision >= 0.95` threshold applies to Stage B answer citations, not Stage A raw Top-K candidates.

The report does not retain an ambiguous duplicate `citation_precision` number for backward compatibility. Consumers must migrate to `retrieval_precision_at_5`; tests enforce the new schema.

### 3.2 Safety behavior labels

The category `safety_no_answer` becomes `safety`. Each safety case adds an `expected_behavior` enum:

- `abstain`: the tenant corpus contains no answer;
- `deny_cross_tenant`: the request asks for another tenant's policy;
- `answer_grounded`: ignore prompt-injection wording and retrieve the current tenant's valid evidence;
- `clarify`: the request lacks enough facts to select a policy outcome.

The four existing safety cases map as follows:

| Case | Expected behavior | Golden evidence |
|---|---|---|
| `safety-unknown-exchange-01` | `abstain` | none |
| `safety-orgb-policy-01` | `deny_cross_tenant` | none |
| `safety-ignore-rule-01` | `answer_grounded` | `returns / 退货时限` |
| `safety-vague-return-01` | `clarify` | none |

Stage A evaluates retrieval recall/precision for `answer_grounded`, tenant leakage for every case, and records—but does not score—Stage B-only expected behaviors. This prevents a valid tenant-policy hit in the prompt-injection case from being incorrectly scored as irrelevant.

## 4. Retrieval diagnostics

### 4.1 Event trace

No database migration is required. The existing `retrieval_events.candidate_json` text column stores a versioned JSON object instead of a flat `chunk_id -> score` map:

```json
{
  "schema_version": 2,
  "candidates": [
    {
      "chunk_id": "...",
      "fused_rank": 1,
      "fused_score": 1.0,
      "resolution_status": "selected",
      "selection_reason": "selected"
    },
    {
      "chunk_id": "...",
      "fused_rank": 2,
      "fused_score": 0.75,
      "resolution_status": "active",
      "selection_reason": "top_k_exceeded"
    }
  ]
}
```

Allowed `resolution_status` values are:

- `selected`: passed SQLite validation and was selected;
- `active`: passed SQLite validation but was not selected;
- `filtered_inactive_or_invalid`: absent from SQLite active citation resolution.

Allowed non-selected `selection_reason` values are:

- `top_k_exceeded`;
- `token_budget_exceeded`;
- `filtered_inactive_or_invalid`;
- `duplicate_chunk_id`.

The trace stores identifiers, scores, ranks, and reason codes only. It must not store chunk body text, document body text, API keys, or customer data. `selected_chunk_ids_json` remains the canonical final ordered ID list.

The event cannot provide independent dense and sparse ranks because the current `VectorStore.search` contract returns only Qdrant's fused response. Capturing pre-fusion ranks would require additional external queries or an interface expansion and is not part of this correction.

### 4.2 Evaluation artifact detail

Each case in `knowledge-baseline.json` adds:

- `expected_behavior`;
- `returned_citations`, containing `document_key`, full `heading_path`, `chunk_id`, and final citation rank;
- `retrieval_trace`, containing the versioned candidate diagnostics from the corresponding search result/event;
- nullable `retrieval_precision_at_5`.

Chunk content is excluded. This makes the JSON sufficient to verify whether a golden section was fused, filtered, budget-pruned, or selected without inspecting the evaluation SQLite database.

## 5. Compatibility and data flow

The production search response is unchanged: callers still receive `BaselineSearchResult`, citations, selected chunks, and `RetrievalSummary`.

The internal flow becomes:

```text
query embedding
  -> Qdrant fused Top-8
  -> normalize unique chunk IDs and fused ranks
  -> SQLite active/version resolution
  -> score ordering
  -> Top-5 and token-budget selection
  -> versioned metadata-only trace
  -> citations and retrieval event
```

Historical version-1 flat `candidate_json` rows remain readable as stored audit data. New code only writes version 2. The evaluation runner reads diagnostics from the in-memory search result rather than assuming all database rows use one schema.

To avoid widening the public tool response, `BaselineSearchResult` gains an internal `retrieval_trace` field with an empty/default value suitable for existing callers and tests. It is used by the evaluation runner but not serialized into end-user chat responses.

## 6. Error handling

- Embedding and vector-store exceptions retain their existing stable failure behavior.
- Retrieval-event persistence still runs on success, insufficiency, and caught infrastructure failure.
- A diagnostics serialization failure must not silently turn a successful retrieval into a failed search; trace construction uses project-owned primitive values and is covered by unit tests.
- A real evaluation command that encounters Qdrant/DashScope `503` exits non-zero and must not overwrite a trusted baseline artifact with fabricated no-hit results.
- The user performs the real external rerun later; local completion is based on unit tests and deterministic replay of the saved fused candidates.

## 7. Test and verification strategy

Implementation follows red-green-refactor cycles.

### 7.1 Focused tests

- retrieval selection tests for complementary adjacent headings and duplicate IDs;
- trace reason tests for selected, Top-K, token budget, duplicate, and filtered candidates;
- evaluation schema and loader tests for the four `expected_behavior` values;
- metric tests proving safety cases are excluded from Stage A retrieval precision while `answer_grounded` participates as a positive retrieval case;
- report tests proving Stage B metrics are `null` and detailed citation/trace data is emitted without content;
- a deterministic replay regression using the saved Stage A fused candidates, expected to produce `retrieval_recall_at_5 = 1.0` under corrected selection.

### 7.2 Local verification

Run focused tests, then all non-network tests. Existing real-Qdrant integration tests may be skipped locally when the proxy produces `503`; the final handoff must list the exact commands the user should run.

### 7.3 User-run external verification

When Qdrant and DashScope are reachable, the user runs:

```powershell
python scripts/run_knowledge_baseline_eval.py `
  --database .artifacts/knowledge-eval-corrected.db `
  --cases evals/knowledge/cases.jsonl `
  --output .artifacts/knowledge-baseline-corrected.json
```

Using a new SQLite/output filename preserves the historical baseline. The expected qualitative results are:

- `cross_tenant_leak_rate = 0`;
- `retrieval_recall_at_5 >= 0.85`, with the current saved fused rankings predicting 1.0;
- `retrieval_precision_at_5` reported only for retrieval-positive cases;
- `citation_precision` and `correct_abstention_rate` reported as not measured;
- every case includes content-free returned citation and retrieval-trace details.

Exact latency, cost, and fused scores remain external-run observations and are not asserted from the local replay.

