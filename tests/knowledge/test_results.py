"""Serialization tests for result objects.

The BaselineSearchResult must serializable for HTTP/report output WITHOUT ever
exposing the internal ``selected_chunks`` (which carry full body text). The
other result dataclasses serialize to their stable public shape.
"""

from app.knowledge.base import VectorStoreUnavailableError
from app.knowledge.results import (
    BaselineSearchResult,
    Citation,
    RetrievalCandidateTrace,
    RetrievalSummary,
    RetrievalTrace,
)


def _citation(citation_id="C1", chunk_id="chunk-1"):
    return Citation(
        citation_id=citation_id,
        document_id="doc-1",
        version_id="version-1",
        chunk_id=chunk_id,
        title="七天无理由退货政策",
        heading_path="/退货政策/时限",
        content="退货期限为收货后七天内。",
    )


def _summary(evidence_status="sufficient"):
    return RetrievalSummary(
        strategy="baseline",
        round_count=1,
        evidence_status=evidence_status,
        latency_ms=12,
    )


def test_citation_serializes_public_fields():
    payload = _citation().public_dict()
    assert payload == {
        "citation_id": "C1",
        "document_id": "doc-1",
        "version_id": "version-1",
        "chunk_id": "chunk-1",
        "title": "七天无理由退货政策",
        "heading_path": "/退货政策/时限",
        "content": "退货期限为收货后七天内。",
    }


def test_retrieval_summary_serializes_without_internal_reasoning():
    payload = _summary().public_dict()
    assert payload == {
        "strategy": "baseline",
        "round_count": 1,
        "evidence_status": "sufficient",
        "latency_ms": 12,
    }


def test_retrieval_trace_serializes_metadata_only():
    trace = RetrievalTrace(
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

    assert trace.to_dict() == {
        "schema_version": 2,
        "candidates": [
            {
                "chunk_id": "chunk-a",
                "fused_rank": 1,
                "fused_score": 0.9,
                "resolution_status": "selected",
                "selection_reason": "selected",
            }
        ],
    }


def test_baseline_search_result_serialization_hides_selected_chunks():
    selected = _citation()  # stand-in: two selected objects
    result = BaselineSearchResult(
        ok=True,
        citations=[_citation()],
        retrieval_summary=_summary(),
        error=None,
        selected_chunks=[selected, selected],
    )

    payload = result.public_dict()

    assert "selected_chunks" not in payload
    assert payload["ok"] is True
    assert payload["citations"][0]["citation_id"] == "C1"
    assert payload["retrieval_summary"]["evidence_status"] == "sufficient"
    assert payload["error"] is None
    # The full body text appears in the structured citation, not duplicated
    # anywhere else in the payload (selected_chunks are hidden entirely).
    assert payload["citations"][0]["content"] == "退货期限为收货后七天内。"
    assert result.retrieval_trace.to_dict() == {
        "schema_version": 2,
        "candidates": [],
    }
    assert "retrieval_trace" not in payload


def test_error_serialization_exposes_only_stable_code_and_safe_message():
    error = VectorStoreUnavailableError(reason="qdrant transport refused")
    result = BaselineSearchResult(
        ok=False,
        citations=[],
        retrieval_summary=_summary("insufficient"),
        error=error,
        selected_chunks=[],
    )
    payload = result.public_dict()
    assert payload["ok"] is False
    assert payload["error"]["code"] == "VECTOR_STORE_UNAVAILABLE"
    assert payload["error"]["message"] == error.safe_message
    assert payload["error"]["message"].startswith("vector store unavailable")
