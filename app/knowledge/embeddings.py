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


class EmbeddingClient(Protocol):
    """Produces dense+sparse hybrid vectors for a document or a query."""

    def embed_documents(self, texts: Sequence[str]) -> list[EmbeddingVector]:
        raise NotImplementedError

    def embed_query(
        self, text: str, *, timeout_seconds: int = 5
    ) -> EmbeddingVector:
        raise NotImplementedError
