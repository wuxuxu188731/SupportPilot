import dataclasses

import pytest

from app.knowledge import base as kb


def _field_names(cls):
    return [f.name for f in dataclasses.fields(cls)]


class TestEnums:
    # 保护行为：文档来源类型必须包含 Markdown、纯文本和 Word 三种支持格式。
    def test_document_source_type_values(self):
        assert {e.value for e in kb.DocumentSourceType} == {
            "markdown",
            "text",
            "word",
        }

    def test_document_status_values(self):
        assert {e.value for e in kb.DocumentStatus} == {
            "processing",
            "active",
            "disabled",
            "failed",
        }

    def test_ingestion_status_values(self):
        assert {e.value for e in kb.IngestionStatus} == {
            "queued",
            "running",
            "succeeded",
            "failed",
        }


class TestDataclasses:
    def test_domain_types_are_frozen(self):
        for cls in (
            kb.KnowledgeDocument,
            kb.DocumentVersion,
            kb.DocumentChunk,
            kb.IngestionJob,
            kb.RetrievalEvent,
        ):
            assert dataclasses.is_dataclass(cls)
            assert cls.__dataclass_params__.frozen

    def test_knowledge_document_fields(self):
        assert _field_names(kb.KnowledgeDocument) == [
            "document_id",
            "organization_id",
            "uploaded_by_user_id",
            "title",
            "source_type",
            "status",
            "active_version_id",
            "created_at",
            "updated_at",
        ]

    def test_document_version_fields(self):
        assert _field_names(kb.DocumentVersion) == [
            "version_id",
            "organization_id",
            "document_id",
            "version_no",
            "content_hash",
            "raw_text",
            "loader_version",
            "chunker_version",
            "embedding_model",
            "embedding_dimensions",
            "created_at",
        ]

    def test_document_chunk_fields(self):
        assert _field_names(kb.DocumentChunk) == [
            "chunk_id",
            "organization_id",
            "document_id",
            "version_id",
            "ordinal",
            "heading_path",
            "content",
            "token_count",
            "start_offset",
            "end_offset",
            "created_at",
        ]

    def test_ingestion_job_fields_match_design_section_6_4(self):
        assert _field_names(kb.IngestionJob) == [
            "job_id",
            "organization_id",
            "document_id",
            "version_id",
            "status",
            "attempt_count",
            "error_code",
            "error_message",
            "started_at",
            "finished_at",
            "created_at",
        ]

    def test_retrieval_event_fields_match_design_section_6_5(self):
        assert _field_names(kb.RetrievalEvent) == [
            "event_id",
            "organization_id",
            "conversation_id",
            "strategy",
            "original_query",
            "planned_queries_json",
            "round_count",
            "candidate_json",
            "selected_chunk_ids_json",
            "outcome",
            "latency_ms",
            "model_calls",
            "estimated_tokens",
            "created_at",
        ]

    def test_document_chunk_created_at_default_is_none(self):
        chunk = kb.DocumentChunk(
            chunk_id="c1",
            organization_id="org-a",
            document_id="doc-a",
            version_id="v1",
            ordinal=0,
            heading_path=None,
            content="body",
            token_count=10,
            start_offset=0,
            end_offset=4,
        )
        assert chunk.created_at is None


def _document():
    return kb.KnowledgeDocument(
        document_id="doc-a",
        organization_id="org-a",
        uploaded_by_user_id="user-a",
        title="Policy",
        source_type=kb.DocumentSourceType.TEXT,
        status=kb.DocumentStatus.PROCESSING,
        active_version_id=None,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )


class TestErrors:
    def test_knowledge_error_exposes_code_and_safe_message(self):
        error = kb.KnowledgeError("EXAMPLE", "safe message")
        assert error.code == "EXAMPLE"
        assert error.safe_message == "safe message"

    def test_document_not_found_leaks_no_cross_tenant_detail(self):
        error = kb.DocumentNotFoundError("org-a", "doc-a")
        assert isinstance(error, kb.KnowledgeError)
        assert isinstance(error, LookupError)
        assert error.code == "DOCUMENT_NOT_FOUND"
        # Must not reveal that the id "belongs to" another organization.
        assert "org-b" not in error.safe_message

    def test_stable_error_codes(self):
        cases = [
            (kb.DocumentNotFoundError, "DOCUMENT_NOT_FOUND"),
            (kb.DocumentDisabledError, "DOCUMENT_DISABLED"),
            (kb.InvalidDocumentError, "INVALID_DOCUMENT"),
            (kb.IngestionFailedError, "INGESTION_FAILED"),
            (kb.InsufficientEvidenceError, "INSUFFICIENT_EVIDENCE"),
        ]
        for cls, expected_code in cases:
            error = cls.__new__(cls)
            assert error.code == expected_code

    def test_search_internal_error_keeps_diagnostic_out_of_safe_message(self):
        error = kb.SearchInternalError(
            internal_reason=(
                "ProviderBalanceError|status=402|code=insufficient_balance"
            )
        )
        assert error.internal_reason == (
            "ProviderBalanceError|status=402|code=insufficient_balance"
        )
        assert error.safe_message == "knowledge search could not be completed"
        assert "insufficient_balance" not in error.safe_message


class TestKnowledgeStoreProtocol:
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
            (
                kb.KnowledgeStore.get_retrieval_event,
                dict(
                    organization_id="org-a",
                    conversation_id="eval-case:adaptive",
                ),
            ),
        ]
        for method, kwargs in calls:
            with pytest.raises(NotImplementedError):
                method(object(), **kwargs)

    def test_missing_methods_raise_not_implemented(self):
        calls = [
            (
                kb.KnowledgeStore.create_document,
                dict(
                    organization_id="org-a",
                    uploaded_by_user_id="user-a",
                    title="Policy",
                    source_type=kb.DocumentSourceType.TEXT,
                ),
            ),
            (
                kb.KnowledgeStore.create_version,
                dict(
                    organization_id="org-a",
                    document_id="doc-a",
                    content_hash="hash",
                    raw_text="body",
                    loader_version="loader-v1",
                    chunker_version="chunker-v1",
                    embedding_model="text-embedding-v4",
                    embedding_dimensions=1024,
                ),
            ),
            (
                kb.KnowledgeStore.create_job,
                dict(
                    organization_id="org-a",
                    document_id="doc-a",
                    version_id="v1",
                ),
            ),
            (
                kb.KnowledgeStore.replace_chunks,
                dict(organization_id="org-a", document_id="doc-a",
                     version_id="v1", chunks=[]),
            ),
            (
                kb.KnowledgeStore.activate_version,
                dict(
                    organization_id="org-a",
                    document_id="doc-a",
                    version_id="v1",
                    job_id="j1",
                ),
            ),
            (
                kb.KnowledgeStore.list_active_version_ids,
                dict(organization_id="org-a"),
            ),
            (
                kb.KnowledgeStore.list_active_chunks,
                dict(organization_id="org-a", candidate_ids=[]),
            ),
        ]
        for method, kwargs in calls:
            with pytest.raises(NotImplementedError):
                method(object(), **kwargs)

    def test_protocol_accepts_an_implementing_object(self):
        class Store(kb.KnowledgeStore):  # typing check only
            def create_document(self, *, organization_id, uploaded_by_user_id,
                                title, source_type):
                return _document()

            def create_version(self, *, organization_id, document_id,
                               content_hash, raw_text, loader_version,
                               chunker_version, embedding_model,
                               embedding_dimensions):
                return kb.DocumentVersion(
                    version_id="v1",
                    organization_id=organization_id,
                    document_id=document_id,
                    version_no=1,
                    content_hash=content_hash,
                    raw_text=raw_text,
                    loader_version=loader_version,
                    chunker_version=chunker_version,
                    embedding_model=embedding_model,
                    embedding_dimensions=embedding_dimensions,
                    created_at="2026-01-01T00:00:00Z",
                )

            def create_job(self, *, organization_id, document_id, version_id):
                return kb.IngestionJob(
                    job_id="j1",
                    organization_id=organization_id,
                    document_id=document_id,
                    version_id=version_id,
                    status=kb.IngestionStatus.QUEUED,
                    attempt_count=0,
                    error_code=None,
                    error_message=None,
                    started_at=None,
                    finished_at=None,
                    created_at="2026-01-01T00:00:00Z",
                )

            def replace_chunks(self, *, organization_id, document_id,
                               version_id, chunks):
                return None

            def activate_version(self, *, organization_id, document_id,
                                 version_id, job_id):
                return _document()

            def list_active_version_ids(self, *, organization_id):
                return []

            def list_active_chunks(self, *, organization_id, candidate_ids):
                return []

        store: kb.KnowledgeStore = Store()
        assert store.create_document(
            organization_id="org-a",
            uploaded_by_user_id="user-a",
            title="Policy",
            source_type=kb.DocumentSourceType.TEXT,
        ).document_id == "doc-a"
