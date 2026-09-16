"""Serialization tests for result objects.

The BaselineSearchResult must serializable for HTTP/report output WITHOUT ever
exposing the internal ``selected_chunks`` (which carry full body text). The
other result dataclasses serialize to their stable public shape.
"""

from app.knowledge.base import VectorStoreUnavailableError
from app.knowledge.results import (
    BaselineSearchResult,
    AdaptiveSearchResult,
    Citation,
    RetrievalCandidateTrace,
    QueryRetrievalResult,
    RetrievalSummary,
    RetrievalTrace,
    ScoredChunk,
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
        # 未提供偏移时序列化为 null：前端据此降级为「只打开来源文档」。
        "start_offset": None,
        "end_offset": None,
    }


def test_citation_serializes_chunk_offsets_when_present():
    # 保护行为：带偏移的引用必须把区间透出到 HTTP/工具结果，前端才有定位依据。
    citation = Citation(
        citation_id="C2",
        document_id="doc-1",
        version_id="version-1",
        chunk_id="chunk-2",
        title="七天无理由退货政策",
        heading_path="/退货政策/时限",
        content="退货期限为收货后七天内。",
        start_offset=12,
        end_offset=24,
    )
    payload = citation.public_dict()
    assert payload["start_offset"] == 12
    assert payload["end_offset"] == 24


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


def test_query_retrieval_result_keeps_internal_ranked_values():
    chunk = object()
    scored = ScoredChunk(chunk=chunk, fused_score=0.75)
    result = QueryRetrievalResult(
        query="returns",
        ranked_chunks=(scored,),
        raw_candidates=(),
        query_tokens=2,
    )

    assert result.ranked_chunks == (scored,)
    assert result.query_tokens == 2


def test_adaptive_result_public_shape_separates_success_and_error():
    result = AdaptiveSearchResult(
        ok=True,
        citations=[_citation()],
        retrieval_summary=RetrievalSummary("single", 1, "sufficient", 4),
        error=None,
        selected_chunks=(),
    )
    payload = result.public_dict()
    assert payload["ok"] is True
    assert payload["data"]["result_code"] == "KNOWLEDGE_FOUND"
    assert "selected_chunks" not in str(payload)

    failed = AdaptiveSearchResult(
        ok=False,
        citations=[],
        retrieval_summary=RetrievalSummary("single", 1, "insufficient", 4),
        error=VectorStoreUnavailableError(reason="secret transport"),
        selected_chunks=(),
    ).public_dict()
    assert failed["ok"] is False
    assert failed["error"]["code"] == "VECTOR_STORE_UNAVAILABLE"
