"""qwen3-rerank 适配器测试；所有调用均使用假响应，不访问网络。"""

from types import SimpleNamespace

import pytest

from app.knowledge.base import ChunkWithDocumentTitle, RerankUnavailableError
from app.knowledge.reranking import (
    DEFAULT_INSTRUCT,
    DashScopeQwenReranker,
)
from app.knowledge.results import ScoredChunk


def _scored(
    chunk_id: str,
    content: str,
    score: float,
) -> ScoredChunk:
    return ScoredChunk(
        chunk=ChunkWithDocumentTitle(
            chunk_id=chunk_id,
            organization_id="org-a",
            document_id="doc-a",
            version_id="version-a",
            ordinal=0,
            heading_path=None,
            content=content,
            token_count=10,
            document_title="测试文档",
        ),
        fused_score=score,
    )


# 保护行为：适配器应使用指定百炼业务空间、环境注入的密钥和英文问答指令，并按返回索引重排分块。
def test_qwen_reranker_calls_workspace_endpoint_and_maps_indexes():
    calls = []

    def call(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            status_code=200,
            output={
                "results": [
                    {"index": 1, "relevance_score": 0.95},
                    {"index": 0, "relevance_score": 0.20},
                ]
            },
        )

    client = DashScopeQwenReranker(
        api_key="env-key",
        base_url=(
            "https://ws-tocwkn1wc3xhur1f.cn-beijing.maas.aliyuncs.com/api/v1"
        ),
        call=call,
    )
    result = client.rerank(
        query="如何退货？",
        chunks=(
            _scored("c1", "退货是一类售后业务。", 0.9),
            _scored("c2", "签收后七天内可以申请退货。", 0.7),
        ),
        timeout_seconds=4,
    )

    assert [item.chunk.chunk.chunk_id for item in result] == ["c2", "c1"]
    assert [item.rerank_score for item in result] == [0.95, 0.20]
    assert calls == [
        {
            "model": "qwen3-rerank",
            "query": "如何退货？",
            "documents": ["退货是一类售后业务。", "签收后七天内可以申请退货。"],
            "top_n": 2,
            "api_key": "env-key",
            "return_documents": False,
            "instruct": DEFAULT_INSTRUCT,
            "timeout": 4,
            "base_address": (
                "https://ws-tocwkn1wc3xhur1f.cn-beijing.maas.aliyuncs.com/api/v1"
            ),
        }
    ]


# 边界情况：提供方漏返候选项时必须失败，避免静默丢失 embedding 已召回的可信分块。
def test_qwen_reranker_rejects_incomplete_results():
    client = DashScopeQwenReranker(
        api_key="key",
        base_url="https://workspace.example/api/v1",
        call=lambda **_: SimpleNamespace(
            status_code=200,
            output={"results": [{"index": 0, "relevance_score": 0.8}]},
        ),
    )

    with pytest.raises(RerankUnavailableError):
        client.rerank(
            query="query",
            chunks=(
                _scored("c1", "first", 0.9),
                _scored("c2", "second", 0.8),
            ),
            timeout_seconds=5,
        )


# 边界情况：单个候选超过模型的 4000 token 上限时应在本地拒绝，且不发起外部请求。
def test_qwen_reranker_rejects_oversized_document_before_call():
    calls = []
    client = DashScopeQwenReranker(
        api_key="key",
        base_url="https://workspace.example/api/v1",
        call=lambda **kwargs: calls.append(kwargs),
    )

    with pytest.raises(RerankUnavailableError):
        client.rerank(
            query="query",
            chunks=(_scored("c1", "word " * 5000, 0.9),),
            timeout_seconds=5,
        )

    assert calls == []
