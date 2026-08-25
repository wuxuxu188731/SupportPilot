"""Shared non-thinking JSON completion boundary for adaptive retrieval."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from app.knowledge.base import SearchInternalError
from app.knowledge.embeddings import count_tokens

STRUCTURED_MAX_TOKENS = 800
_SAFE_PROVIDER_CODE = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")


def _provider_diagnostic(exc: Exception) -> str:
    parts = [type(exc).__name__]
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int) and not isinstance(status_code, bool):
        parts.append(f"status={status_code}")
    provider_code = getattr(exc, "code", None)
    if isinstance(provider_code, str) and _SAFE_PROVIDER_CODE.fullmatch(
        provider_code
    ):
        parts.append(f"code={provider_code}")
    return "|".join(parts)


@dataclass(frozen=True)
class StructuredCompletion:
    raw_json: str
    estimated_tokens: int


class StructuredJSONClient(Protocol):
    def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
    ) -> StructuredCompletion:
        raise NotImplementedError


class OpenAIStructuredJSONClient:
    """Call an OpenAI-compatible SDK with a fixed JSON-only contract."""

    def __init__(self, client, *, model_name: str) -> None:
        self._client = client
        self._model_name = model_name

    def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
    ) -> StructuredCompletion:
        provider_failure = None
        try:
            response = self._client.chat.completions.create(
                model=self._model_name,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                response_format={"type": "json_object"},
                extra_body={"thinking": {"type": "disabled"}},
                max_tokens=STRUCTURED_MAX_TOKENS,
                timeout=timeout_seconds,
            )
        except Exception as exc:
            provider_failure = _provider_diagnostic(exc)

        if provider_failure is not None:
            raise SearchInternalError(internal_reason=provider_failure)

        choices = getattr(response, "choices", None)
        if not isinstance(choices, (list, tuple)) or len(choices) != 1:
            raise SearchInternalError()
        choice = choices[0]
        if getattr(choice, "finish_reason", None) == "length":
            raise SearchInternalError()
        message = getattr(choice, "message", None)
        if message is None or not hasattr(message, "content"):
            raise SearchInternalError()
        content = message.content
        if content is None:
            raw_json = ""
        elif isinstance(content, str):
            raw_json = content
        else:
            raise SearchInternalError()

        return StructuredCompletion(
            raw_json=raw_json,
            estimated_tokens=count_tokens(
                system_prompt + user_prompt + raw_json
            ),
        )
