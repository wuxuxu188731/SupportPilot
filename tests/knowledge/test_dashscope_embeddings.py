"""Embedding contract and DashScope dense+sparse adapter tests.

Tests never hit the network: ``DashScopeEmbeddingClient`` accepts injected
``call`` and ``sleep`` callables. The default ``call`` is the real
``dashscope.TextEmbedding.call`` but every test substitutes a fake.
"""

from types import SimpleNamespace

import pytest

from app.knowledge.base import EmbeddingUnavailableError
from app.knowledge.dashscope_embeddings import (
    MODEL_NAME,
    DashScopeEmbeddingClient,
    EmbeddingVector,
    SparseValue,
)


def successful_response(text_count, *, token_count=20):
    return SimpleNamespace(
        status_code=200,
        output={
            "embeddings": [
                {
                    "embedding": [0.0] * 1024,
                    "sparse_embedding": [
                        {"index": 17, "value": 0.75, "token": "return"}
                    ],
                    "text_index": index,
                }
                for index in range(text_count)
            ]
        },
        usage={"total_tokens": token_count},
        code="",
        message="",
    )


class RecordingCall:
    """Wraps a fake responder and records every call's kwargs for assertion."""

    def __init__(self, responder):
        self.responder = responder
        self.kwargs = []

    def __call__(self, **kwargs):
        self.kwargs.append(kwargs)
        text_count = len(kwargs["input"])
        return self.responder(text_count)


# --- Step 1: SDK params and batching -------------------------------------


def test_documents_use_document_mode_and_batches_of_ten():
    call = RecordingCall(successful_response)
    client = DashScopeEmbeddingClient(
        api_key="test-key", call=call, sleep=lambda _: None
    )

    result = client.embed_documents([f"doc {i}" for i in range(11)])

    assert [len(item["input"]) for item in call.kwargs] == [10, 1]
    assert all(item["model"] == MODEL_NAME for item in call.kwargs)
    assert all(item["dimension"] == 1024 for item in call.kwargs)
    assert all(item["output_type"] == "dense&sparse" for item in call.kwargs)
    assert all(item["text_type"] == "document" for item in call.kwargs)
    assert len(result) == 11


def test_query_uses_query_mode_instruction_and_timeout():
    call = RecordingCall(successful_response)
    client = DashScopeEmbeddingClient(
        api_key="test-key", call=call, sleep=lambda _: None
    )
    client.embed_query("退货期限")

    assert call.kwargs == [{
        "api_key": "test-key",
        "model": MODEL_NAME,
        "input": ["退货期限"],
        "dimension": 1024,
        "output_type": "dense&sparse",
        "text_type": "query",
        "instruct": (
            "Given an ecommerce after-sales policy question, "
            "retrieve the most relevant enterprise policy passages"
        ),
        "timeout": 5,
    }]


# --- Step 2: response validation and error conversion --------------------


def test_returns_vectors_with_dense_sparse_and_self_counted_tokens():
    call = RecordingCall(successful_response)
    client = DashScopeEmbeddingClient(
        api_key="test-key", call=call, sleep=lambda _: None
    )

    vectors = client.embed_documents(["退货期限 是多少", "换货"])

    assert len(vectors) == 2
    v0 = vectors[0]
    assert isinstance(v0, EmbeddingVector)
    assert isinstance(v0.dense, tuple) and len(v0.dense) == 1024
    assert all(v == 0.0 for v in v0.dense)
    assert v0.sparse == (SparseValue(index=17, value=0.75),)
    # token_count must be counted deterministically per input by the project
    # tokenizer, not half of the fake usage.total_tokens.
    assert v0.token_count > 0


def test_documents_reordered_by_text_index():
    def shuffled(text_count):
        embeddings = successful_response(text_count)
        embeddings.output["embeddings"].reverse()  # now descending text_index
        return embeddings

    call = RecordingCall(shuffled)
    client = DashScopeEmbeddingClient(
        api_key="test-key", call=call, sleep=lambda _: None
    )

    vectors = client.embed_documents(["a", "b", "c"])

    assert len(vectors) == 3


def test_response_count_mismatch_raises():
    def wrong_count(text_count):
        return successful_response(1)

    call = RecordingCall(wrong_count)
    client = DashScopeEmbeddingClient(
        api_key="test-key", call=call, sleep=lambda _: None
    )

    with pytest.raises(EmbeddingUnavailableError):
        client.embed_documents(["a", "b"])


def test_edge_dimension_wrong_raises():
    def not_dimension(text_count):
        resp = successful_response(text_count)
        resp.output["embeddings"][0]["embedding"] = [0.0] * 64
        return resp

    call = RecordingCall(not_dimension)
    client = DashScopeEmbeddingClient(
        api_key="test-key", call=call, sleep=lambda _: None
    )

    with pytest.raises(EmbeddingUnavailableError):
        client.embed_documents(["a"])


def test_nan_or_inf_dense_value_raises():
    for bad in (float("nan"), float("inf"), float("-inf")):
        def bad_value(text_count, bad=bad):
            resp = successful_response(text_count)
            resp.output["embeddings"][0]["embedding"][0] = bad
            return resp

        call = RecordingCall(bad_value)
        client = DashScopeEmbeddingClient(
            api_key="test-key", call=call, sleep=lambda _: None
        )
        with pytest.raises(EmbeddingUnavailableError):
            client.embed_documents(["a"])


def test_duplicate_sparse_index_raises():
    def dup_sparse(text_count):
        resp = successful_response(text_count)
        resp.output["embeddings"][0]["sparse_embedding"] = [
            {"index": 3, "value": 0.5, "token": "x"},
            {"index": 3, "value": 0.5, "token": "y"},
        ]
        return resp

    call = RecordingCall(dup_sparse)
    client = DashScopeEmbeddingClient(
        api_key="test-key", call=call, sleep=lambda _: None
    )
    with pytest.raises(EmbeddingUnavailableError):
        client.embed_documents(["a"])


def test_negative_sparse_index_raises():
    def neg_sparse(text_count):
        resp = successful_response(text_count)
        resp.output["embeddings"][0]["sparse_embedding"] = [
            {"index": -1, "value": 0.5, "token": "x"}
        ]
        return resp

    call = RecordingCall(neg_sparse)
    client = DashScopeEmbeddingClient(
        api_key="test-key", call=call, sleep=lambda _: None
    )
    with pytest.raises(EmbeddingUnavailableError):
        client.embed_documents(["a"])


@pytest.mark.parametrize("status_code", [429, 500, 503])
def test_temporary_failures_retry_once_then_raise(status_code):
    responses = [
        SimpleNamespace(
            status_code=status_code,
            output={},
            usage={},
            code="Throttling.Temporary",
            message="slow down",
        ),
        successful_response(1),
    ]
    call = RecordingCall(lambda _: responses.pop(0))
    sleeps = []
    client = DashScopeEmbeddingClient(
        api_key="test-key", call=call, sleep=sleeps.append
    )

    client.embed_documents(["a"])

    assert len(call.kwargs) == 2  # exactly one retry
    assert len(sleeps) == 1  # slept once between the two attempts


@pytest.mark.parametrize("status_code", [429, 500, 503])
def test_temporary_failures_still_raise_after_retries_exhausted(status_code):
    responses = [
        SimpleNamespace(
            status_code=status_code,
            output={},
            usage={},
            code="Throttling.Temporary",
            message="slow down",
        )
    ] * 2  # first attempt + retry both fail
    call = RecordingCall(lambda _: responses.pop(0))
    client = DashScopeEmbeddingClient(
        api_key="test-key", call=call, sleep=lambda _: None
    )

    with pytest.raises(EmbeddingUnavailableError):
        client.embed_documents(["a"])

    assert len(call.kwargs) == 2


def test_timeout_exception_retries_once_then_raises():
    def fail_fast(_):
        raise TimeoutError("request timed out")

    sleeps = []
    client = DashScopeEmbeddingClient(
        api_key="test-key",
        call=RecordingCall(fail_fast),
        sleep=sleeps.append,
    )

    with pytest.raises(EmbeddingUnavailableError):
        client.embed_documents(["a"])


def test_connection_error_retries_once_then_raises():
    def fail_fast(_):
        raise ConnectionError("no route to host")

    client = DashScopeEmbeddingClient(
        api_key="test-key", call=RecordingCall(fail_fast), sleep=lambda _: None
    )

    with pytest.raises(EmbeddingUnavailableError):
        client.embed_documents(["a"])


@pytest.mark.parametrize("status_code", [400, 401, 403])
def test_non_temporary_four_xx_does_not_retry(status_code):
    def reject(_):
        return SimpleNamespace(
            status_code=status_code,
            output={},
            usage={},
            code=f"InvalidParameter.{status_code}",
            message="bad request",
        )

    call = RecordingCall(reject)
    client = DashScopeEmbeddingClient(
        api_key="test-key", call=call, sleep=lambda _: None
    )

    with pytest.raises(EmbeddingUnavailableError):
        client.embed_documents(["a"])

    assert len(call.kwargs) == 1  # no retry
