"""Embedding contract: vector value types and the embedding-client boundary.

``EmbeddingVector.token_count`` is deterministic per-input, counted with the
same ``cl100k_base`` project tokenizer the chunker uses, so cost/token
estimates stay stable and never depend on mutable "last-call" usage state held
on a shared client.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

import tiktoken

PROJECT_TOKENIZER = tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    """Deterministic token count for a single text using the project tokenizer."""
    return len(PROJECT_TOKENIZER.encode(text))


@dataclass(frozen=True)
class SparseValue:
    index: int
    value: float


@dataclass(frozen=True)
class EmbeddingVector:
    dense: tuple[float, ...]
    sparse: tuple[SparseValue, ...]
    token_count: int


# 单次「查询侧」embedding 的超时预算（秒）。这里是唯一的取值来源：
# 适配器默认参数、基线检索（HybridRetriever 未注入 provider 时）与 Adaptive Search
# 的每次查询都以它为上限，实际下发的是 min(该值, 本次检索的剩余预算)。
#
# 为什么从 5 秒提到 10 秒：实测 DashScope text-embedding-v4 单次查询向量耗时约 3.6 秒，
# 5 秒预算在服务端抖动时会直接判死——第一次尝试就吃掉全部预算，重试只剩不到 1 秒，
# 于是整条检索以 EMBEDDING_UNAVAILABLE 失败（用户反馈：对话连续答"基础设施失败"）。
# 单次调用的放大不会突破总预算：Adaptive Search 仍受
# KNOWLEDGE_SEARCH_TIMEOUT_SECONDS（默认 30 秒，见 service.SearchBudget）约束。
QUERY_TIMEOUT_SECONDS = 10


class EmbeddingClient(Protocol):
    """Produces dense+sparse hybrid vectors for a document or a query."""

    def embed_documents(self, texts: Sequence[str]) -> list[EmbeddingVector]:
        raise NotImplementedError

    def embed_query(
        self, text: str, *, timeout_seconds: int = QUERY_TIMEOUT_SECONDS
    ) -> EmbeddingVector:
        raise NotImplementedError
