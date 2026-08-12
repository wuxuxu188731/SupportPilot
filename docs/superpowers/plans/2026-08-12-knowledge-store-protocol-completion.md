# KnowledgeStore RAG Protocol Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Declare every persistence operation used by the RAG ingestion and retrieval services on `KnowledgeStore`.

**Architecture:** Keep the existing single `KnowledgeStore` protocol and add only the five missing operations already implemented by `SQLiteKnowledgeStore`. This is a contract-only change: runtime behavior and concrete persistence code remain unchanged.

**Tech Stack:** Python 3, `typing.Protocol`, pytest

## Global Constraints

- Only RAG persistence operations are in scope.
- Do not split `KnowledgeStore` or modify `SQLiteKnowledgeStore`.
- Every new operation must remain explicitly tenant-scoped through `organization_id`.
- Method signatures and return types must match `SQLiteKnowledgeStore`.

---

### Task 1: Complete and verify the RAG persistence contract

**Files:**
- Modify: `tests/knowledge/test_domain.py:178`
- Modify: `app/knowledge/base.py:242`

**Interfaces:**
- Consumes: existing `DocumentVersion`, `IngestionJob`, `KnowledgeDocument`, and `RetrievalEvent` domain models.
- Produces: `KnowledgeStore.get_version_by_id`, `KnowledgeStore.get_version_by_hash`, `KnowledgeStore.mark_job_running`, `KnowledgeStore.fail_ingestion`, and `KnowledgeStore.record_retrieval_event`.

- [ ] **Step 1: Write the failing protocol test**

Add a focused test to `TestKnowledgeStoreProtocol`:

```python
def test_rag_service_methods_raise_not_implemented(self):
    event = kb.RetrievalEvent(
        event_id="event-a",
        organization_id="org-a",
        conversation_id=None,
        strategy="baseline",
        original_query="refund policy",
        planned_queries_json='["refund policy"]',
        round_count=1,
        candidate_json="{}",
        selected_chunk_ids_json="[]",
        outcome="no_candidates",
        latency_ms=1,
        model_calls=0,
        estimated_tokens=0,
        created_at="2026-08-12T00:00:00+00:00",
    )
    calls = [
        (
            kb.KnowledgeStore.get_version_by_id,
            dict(
                organization_id="org-a",
                document_id="doc-a",
                version_id="v1",
            ),
        ),
        (
            kb.KnowledgeStore.get_version_by_hash,
            dict(
                organization_id="org-a",
                document_id="doc-a",
                content_hash="sha256:abc",
            ),
        ),
        (
            kb.KnowledgeStore.mark_job_running,
            dict(organization_id="org-a", job_id="job-a"),
        ),
        (
            kb.KnowledgeStore.fail_ingestion,
            dict(
                organization_id="org-a",
                document_id="doc-a",
                version_id="v1",
                job_id="job-a",
                error_code="INGESTION_FAILED",
                error_message="knowledge ingestion failed",
            ),
        ),
        (
            kb.KnowledgeStore.record_retrieval_event,
            dict(organization_id="org-a", event=event),
        ),
    ]
    for method, kwargs in calls:
        with pytest.raises(NotImplementedError):
            method(object(), **kwargs)
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```powershell
pytest tests/knowledge/test_domain.py::TestKnowledgeStoreProtocol::test_rag_service_methods_raise_not_implemented -v
```

Expected: FAIL while evaluating `KnowledgeStore.get_version_by_id`, because the protocol does not declare it yet.

- [ ] **Step 3: Add the five minimal protocol declarations**

Add these signatures to `KnowledgeStore`, grouped with their version, job, and retrieval operations:

```python
def get_version_by_id(
    self,
    *,
    organization_id: str,
    document_id: str,
    version_id: str,
) -> DocumentVersion:
    raise NotImplementedError

def get_version_by_hash(
    self,
    *,
    organization_id: str,
    document_id: str,
    content_hash: str,
) -> DocumentVersion:
    raise NotImplementedError

def mark_job_running(
    self,
    *,
    organization_id: str,
    job_id: str,
) -> IngestionJob | None:
    raise NotImplementedError

def fail_ingestion(
    self,
    *,
    organization_id: str,
    document_id: str,
    version_id: str,
    job_id: str,
    error_code: str,
    error_message: str,
) -> KnowledgeDocument:
    raise NotImplementedError

def record_retrieval_event(
    self,
    *,
    organization_id: str,
    event: RetrievalEvent,
) -> RetrievalEvent:
    raise NotImplementedError
```

- [ ] **Step 4: Run the focused test and verify GREEN**

Run:

```powershell
pytest tests/knowledge/test_domain.py::TestKnowledgeStoreProtocol::test_rag_service_methods_raise_not_implemented -v
```

Expected: PASS.

- [ ] **Step 5: Run RAG regression tests**

Run:

```powershell
pytest tests/knowledge -q
```

Expected: all tests pass with zero failures.

- [ ] **Step 6: Review the final diff**

Run:

```powershell
git diff --check
git diff -- app/knowledge/base.py tests/knowledge/test_domain.py
```

Confirm that only the five protocol declarations and their focused test were added.

- [ ] **Step 7: Commit the implementation**

```powershell
git add app/knowledge/base.py tests/knowledge/test_domain.py docs/superpowers/plans/2026-08-12-knowledge-store-protocol-completion.md
git commit -m "fix: complete KnowledgeStore RAG protocol"
```
