"""Qwen3 reranking boundary used only by Agentic Search."""

from __future__ import annotations

import math
from dataclasses import dataclass
from http import HTTPStatus
from typing import Callable, Protocol, Sequence

import dashscope
import requests

from app.knowledge.base import RerankUnavailableError
from app.knowledge.embeddings import count_tokens
from app.knowledge.results import ScoredChunk

MODEL_NAME = "qwen3-rerank"
DEFAULT_INSTRUCT = (
    "Given a web search query, retrieve relevant passages that answer the query."
)
MAX_DOCUMENTS = 500
MAX_DOCUMENT_TOKENS = 4000
MAX_REQUEST_TOKENS = 120000

_Call = Callable[..., object]


@dataclass(frozen=True)
class RerankedChunk:
    chunk: ScoredChunk  # 已通过向量召回和 SQLite 有效性校验的原始分块
    rerank_score: float  # qwen3-rerank 返回的查询相关性分数


class Reranker(Protocol):
    """Rerank already-recalled, trusted chunks for one agentic query."""

    def rerank(
        self,
        *,
        query: str,
        chunks: Sequence[ScoredChunk],
        timeout_seconds: int,
    ) -> tuple[RerankedChunk, ...]:
        raise NotImplementedError


class IdentityReranker:
    """Compatibility default for directly constructed adaptive services."""

    def rerank(
        self,
        *,
        query: str,
        chunks: Sequence[ScoredChunk],
        timeout_seconds: int,
    ) -> tuple[RerankedChunk, ...]:
        del query, timeout_seconds
        return tuple(
            RerankedChunk(chunk=item, rerank_score=item.fused_score)
            for item in chunks
        )


class DashScopeQwenReranker:
    """Call the DashScope qwen3-rerank endpoint and validate its index mapping."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str = MODEL_NAME,
        instruct: str = DEFAULT_INSTRUCT,
        call: _Call | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("api_key must not be blank")
        if not base_url.strip():
            raise ValueError("base_url must not be blank")
        if not instruct.strip():
            raise ValueError("instruct must not be blank")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._instruct = instruct
        self._call = call or dashscope.TextReRank.call

    def rerank(
        self,
        *,
        query: str,
        chunks: Sequence[ScoredChunk],
        timeout_seconds: int,
    ) -> tuple[RerankedChunk, ...]:
        if not chunks:
            return ()
        if len(chunks) > MAX_DOCUMENTS:
            raise RerankUnavailableError(reason="document count exceeds 500")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be a positive integer")

        documents = [item.chunk.content for item in chunks]
        document_tokens = [count_tokens(item) for item in documents]
        if any(count > MAX_DOCUMENT_TOKENS for count in document_tokens):
            raise RerankUnavailableError(reason="a document exceeds 4000 tokens")
        if count_tokens(query) + sum(document_tokens) > MAX_REQUEST_TOKENS:
            raise RerankUnavailableError(reason="request exceeds 120000 tokens")

        try:
            response = self._call(
                model=self._model,
                query=query,
                documents=documents,
                top_n=len(documents),
                api_key=self._api_key,
                return_documents=False,
                instruct=self._instruct,
                timeout=timeout_seconds,
                base_address=self._base_url,
            )
        except (requests.exceptions.RequestException, ConnectionError, TimeoutError) as exc:
            raise RerankUnavailableError(reason=f"{type(exc).__name__}: {exc}") from exc

        if int(getattr(response, "status_code", 0)) != HTTPStatus.OK:
            raise RerankUnavailableError(
                reason=(
                    f"provider status {getattr(response, 'status_code', '?')} "
                    f"code={getattr(response, 'code', '')}"
                )
            )
        try:
            return self._parse_response(response, chunks)
        except RerankUnavailableError:
            raise
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise RerankUnavailableError(
                reason="provider returned malformed rerank results"
            ) from exc

    @staticmethod
    def _parse_response(
        response: object,
        chunks: Sequence[ScoredChunk],
    ) -> tuple[RerankedChunk, ...]:
        output = getattr(response, "output", None)
        results = getattr(output, "results", None)
        if results is None and isinstance(output, dict):
            results = output.get("results")
        if not isinstance(results, (list, tuple)) or len(results) != len(chunks):
            raise RerankUnavailableError(reason="provider returned an invalid result count")

        reranked: list[RerankedChunk] = []
        seen: set[int] = set()
        for result in results:
            index = int(
                result.get("index") if isinstance(result, dict) else getattr(result, "index")
            )
            score = float(
                result.get("relevance_score")
                if isinstance(result, dict)
                else getattr(result, "relevance_score")
            )
            if index < 0 or index >= len(chunks) or index in seen:
                raise RerankUnavailableError(reason="provider returned an invalid document index")
            if not math.isfinite(score):
                raise RerankUnavailableError(reason="provider returned a non-finite score")
            seen.add(index)
            reranked.append(RerankedChunk(chunk=chunks[index], rerank_score=score))
        return tuple(reranked)
