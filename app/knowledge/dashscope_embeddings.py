"""DashScope ``text-embedding-v4`` dense+sparse hybrid adapter.

The client talks to the official synchronous ``dashscope.TextEmbedding.call``
interface and is tesbable without the network: the constructor accepts injected
``call`` and ``sleep`` callables that replace the real SDK function and a real
short sleep.

Response shape (``output_type="dense&sparse"``): each output item carries
``embedding`` (dense vector), ``sparse_embedding`` (``[{index,value,token}]``)
and ``text_index``. Items are ordered by ``text_index`` before conversion.

Temporary failures (429/5xx/connection/timeout) are retried once with a short
sleep, then surface as :class:`EmbeddingUnavailableError`. Non-temporary 4xx
errors are never retried.
"""

from __future__ import annotations

import time
from math import floor
from collections.abc import Sequence
from typing import Callable

import dashscope
import requests

from app.knowledge.base import EmbeddingUnavailableError
from app.knowledge.embeddings import (
    EmbeddingVector,
    QUERY_TIMEOUT_SECONDS,
    SparseValue,
    count_tokens,
)

MODEL_NAME = "text-embedding-v4"
TEXT_EMBEDDING_DIMENSION = 1024
OUTPUT_TYPE = "dense&sparse"
# 查询侧超时预算的唯一取值来源在 app/knowledge/embeddings.py（检索链路共用），
# 这里重新导出同名常量，保持本模块对外接口不变。
RETRIES_PER_CALL = 1
RETRY_SLEEP_SECONDS = 0.5
QUERY_INSTRUCT = (
    "Given an ecommerce after-sales policy question, "
    "retrieve the most relevant enterprise policy passages"
)

# A callable that mirrors dashscope sync API: f(**kwargs) -> response.
_Call = Callable[..., object]


class DashScopeEmbeddingClient:
    """Adapts DashScope ``text-embedding-v4`` to :class:`EmbeddingClient`.

    ``call`` defaults to the real ``dashscope.TextEmbedding.call`` so the client
    is production-ready out of the box; ``sleep`` defaults to a short real
    sleep for the retry backoff. Tests replace both with fakes.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str | None = None,
        call: _Call | None = None,
        sleep: Callable[[float], object] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._api_key = api_key
        if base_url:
            # The dashscope SDK reads `dashscope.base_http_api_url` as a
            # PROCESS-GLOBAL module attribute at request-build time (e.g.
            # api_request_factory and base_api url builders). Setting it here
            # once configures every subsequent HTTP call made through this
            # SDK in the process. Because it is global, a single process that
            # mixes clients must not point different clients at different
            # DashScope-compatible endpoints.
            dashscope.base_http_api_url = base_url
        self._call: _Call = call or dashscope.TextEmbedding.call
        self._sleep: Callable[[float], object] = (
            sleep if sleep is not None else time.sleep
        )
        self._monotonic = monotonic or time.monotonic

    def embed_documents(
        self, texts: Sequence[str]
    ) -> list[EmbeddingVector]:
        vectors: list[EmbeddingVector] = []
        for start in range(0, len(texts), 10):
            batch = texts[start : start + 10]
            vectors.extend(self._embed_batch(list(batch), text_type="document", timeout=None))
        return vectors

    def embed_query(
        self, text: str, *, timeout_seconds: int = QUERY_TIMEOUT_SECONDS
    ) -> EmbeddingVector:
        self._validate_timeout(timeout_seconds)
        vectors = self._embed_batch(
            [text], text_type="query", timeout=timeout_seconds
        )
        return vectors[0]

    def _embed_batch(
        self,
        texts: list[str],
        *,
        text_type: str,
        timeout: int | None,
    ) -> list[EmbeddingVector]:
        kwargs: dict[str, object] = {
            "api_key": self._api_key,
            "model": MODEL_NAME,
            "input": texts,
            "dimension": TEXT_EMBEDDING_DIMENSION,
            "output_type": OUTPUT_TYPE,
            "text_type": text_type,
        }
        if text_type == "query":
            kwargs["instruct"] = QUERY_INSTRUCT
        deadline = None
        if timeout is not None:
            deadline = self._monotonic() + timeout

        for attempt in range(RETRIES_PER_CALL + 1):
            if deadline is not None:
                attempt_timeout = (
                    timeout
                    if attempt == 0
                    else floor(deadline - self._monotonic())
                )
                if attempt_timeout < 1:
                    raise EmbeddingUnavailableError(
                        reason="query embedding deadline exhausted"
                    )
                kwargs["timeout"] = attempt_timeout
            try:
                response = self._call(**kwargs)
                if self._is_temporary_failure(response):
                    if attempt < RETRIES_PER_CALL:
                        self._sleep(RETRY_SLEEP_SECONDS)
                        continue
                    raise EmbeddingUnavailableError(
                        reason=self._failure_reason(response)
                    )
                if self._is_failed_status(response):
                    # Non-temporary provider error (e.g. 4xx): do not retry.
                    raise EmbeddingUnavailableError(
                        reason=self._failure_reason(response)
                    )
                return self._parse_response(response, texts)
            except EmbeddingUnavailableError:
                raise
            except (requests.exceptions.RequestException, ConnectionError, TimeoutError) as exc:
                if attempt < RETRIES_PER_CALL:
                    self._sleep(RETRY_SLEEP_SECONDS)
                    continue
                raise EmbeddingUnavailableError(
                    reason=f"{type(exc).__name__}: {exc}"
                ) from exc

        # Unreachable: the loop always returns or raises.
        raise RuntimeError("unreachable")  # pragma: no cover

    @staticmethod
    def _validate_timeout(timeout_seconds: int) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be a positive integer")

    @staticmethod
    def _is_temporary_failure(response: object) -> bool:
        status_code = int(getattr(response, "status_code", 0))
        return status_code in (429, 500, 502, 503, 504)

    @staticmethod
    def _is_failed_status(response: object) -> bool:
        status_code = int(getattr(response, "status_code", 0))
        return status_code != 200

    @staticmethod
    def _failure_reason(response: object) -> str:
        code = getattr(response, "code", "")
        message = getattr(response, "message", "")
        status_code = getattr(response, "status_code", "?")
        return f"provider status {status_code} code={code} message={message}"

    def _parse_response(
        self, response: object, texts: list[str]
    ) -> list[EmbeddingVector]:
        output = getattr(response, "output", None)
        items = (output or {}).get("embeddings", []) if isinstance(output, dict) else []
        if len(items) != len(texts):
            raise EmbeddingUnavailableError(
                reason=(
                    f"returned {len(items)} embeddings for {len(texts)} inputs"
                )
            )

        # DashScope does not guarantee order; sort by text_index so the i-th
        # sorted item corresponds to the i-th input.
        ordered = sorted(items, key=lambda item: int(item.get("text_index", 0)))

        vectors: list[EmbeddingVector] = []
        for i, item in enumerate(ordered):
            dense = tuple(self._validate_dense(item["embedding"]))
            sparse = self._validate_sparse(item["sparse_embedding"])
            # Deterministic per-input token counts come from the project
            # tokenizer, never by averaging usage.total_tokens.
            vectors.append(
                EmbeddingVector(
                    dense=dense,
                    sparse=sparse,
                    token_count=count_tokens(texts[i]),
                )
            )

        # usage.total_tokens is used only for a response-validity sanity check
        # (the provider echoed a plausible count), never as the canonical count.
        self._check_usage(response, texts)

        return vectors

    def _validate_dense(self, values: Sequence[object]) -> tuple[float, ...]:
        if len(values) != TEXT_EMBEDDING_DIMENSION:
            raise EmbeddingUnavailableError(
                reason=(
                    f"dense vector is {len(values)}-dim, "
                    f"expected {TEXT_EMBEDDING_DIMENSION}"
                )
            )
        out = tuple(float(v) for v in values)
        for v in out:
            if not self._is_finite(v):
                raise EmbeddingUnavailableError(reason="dense vector has non-finite values")
        return out

    @staticmethod
    def _is_finite(value: float) -> bool:
        try:
            return value == value and value not in (float("inf"), float("-inf"))
        except TypeError:
            return False

    def _validate_sparse(self, values: Sequence[object]) -> tuple[SparseValue, ...]:
        seen: set[int] = set()
        out: list[SparseValue] = []
        for entry in values:
            index = int(entry["index"])
            value = float(entry["value"])
            if not self._is_finite(value):
                raise EmbeddingUnavailableError(reason="sparse value is non-finite")
            if index < 0:
                raise EmbeddingUnavailableError(reason="sparse index is negative")
            if index in seen:
                raise EmbeddingUnavailableError(reason="duplicate sparse index")
            seen.add(index)
            out.append(SparseValue(index=index, value=value))
        return tuple(out)

    @staticmethod
    def _check_usage(response: object, texts: list[str]) -> None:
        usage = getattr(response, "usage", None) or {}
        total = usage.get("total_tokens", 0) if isinstance(usage, dict) else 0
        if total and total < len(texts):
            raise EmbeddingUnavailableError(
                reason="provider total_tokens below input count"
            )
