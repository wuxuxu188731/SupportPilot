# KnowledgeStore RAG Protocol Completion Design

## Goal

Complete the existing `KnowledgeStore` protocol so it declares every persistence operation currently used by the RAG ingestion and retrieval services.

## Scope

Add these five existing operations to `KnowledgeStore`:

- `get_version_by_id`
- `get_version_by_hash`
- `mark_job_running`
- `fail_ingestion`
- `record_retrieval_event`

Their signatures and return types will match `SQLiteKnowledgeStore`. Every operation remains explicitly tenant-scoped through `organization_id`.

This change will not split the protocol, change runtime behavior, modify the SQLite implementation, or touch non-RAG stores.

## Design

`KnowledgeStore` remains the single persistence boundary consumed by `KnowledgeIngestionService` and `BaselineKnowledgeSearchService`. Each new protocol method will raise `NotImplementedError`, consistent with the existing declarations.

The methods will be placed near their related operations:

- version lookups beside version creation/listing;
- job state operations beside job creation/lookups;
- retrieval-event recording beside active-chunk/citation operations.

## Testing

Extend the existing `TestKnowledgeStoreProtocol.test_missing_methods_raise_not_implemented` coverage with calls to all five methods. Follow red-green TDD:

1. Add the protocol expectations and confirm they fail because the methods are absent.
2. Add the minimal protocol declarations.
3. Run the focused domain test and then the full knowledge test suite.

## Success Criteria

- All RAG store calls in `ingestion.py` and `retrieval.py` are declared by `KnowledgeStore`.
- Protocol signatures match `SQLiteKnowledgeStore`.
- Existing runtime behavior is unchanged.
- Focused and knowledge tests pass.
