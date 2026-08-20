from types import SimpleNamespace

import pytest
import requests

from app.knowledge.base import SearchInternalError
from app.knowledge.structured_llm import OpenAIStructuredJSONClient


class _Completions:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


class RecordingOpenAIClient:
    def __init__(self, content='{"strategy":"SINGLE"}', *, finish_reason="stop"):
        self.completions = _Completions(
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content=content),
                        finish_reason=finish_reason,
                    )
                ]
            )
        )
        self.chat = SimpleNamespace(completions=self.completions)

    @property
    def calls(self):
        return self.completions.calls


def test_structured_client_disables_thinking_and_requests_json():
    sdk = RecordingOpenAIClient()
    client = OpenAIStructuredJSONClient(sdk, model_name="deepseek-v4-flash")

    result = client.complete(
        system_prompt=(
            'Return json only. Example: '
            '{"strategy":"SINGLE","queries":["returns"],'
            '"reason_code":"SIMPLE_POLICY"}'
        ),
        user_prompt="question",
        timeout_seconds=4.5,
    )

    call = sdk.calls[0]
    assert call["model"] == "deepseek-v4-flash"
    assert call["response_format"] == {"type": "json_object"}
    assert call["extra_body"] == {"thinking": {"type": "disabled"}}
    assert call["timeout"] == 4.5
    assert call["max_tokens"] == 800
    assert result.raw_json == '{"strategy":"SINGLE"}'
    assert result.estimated_tokens > 0


def test_none_content_is_normalized_to_empty_json():
    sdk = RecordingOpenAIClient(content=None)
    result = OpenAIStructuredJSONClient(sdk, model_name="model").complete(
        system_prompt='return json: {"ok":true}',
        user_prompt="question",
        timeout_seconds=2,
    )
    assert result.raw_json == ""


@pytest.mark.parametrize(
    "response",
    [
        SimpleNamespace(choices=[]),
        SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop")]),
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content={"not": "text"}),
                    finish_reason="stop",
                )
            ]
        ),
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="{}"),
                    finish_reason="length",
                )
            ]
        ),
    ],
)
def test_malformed_or_truncated_provider_response_is_internal_error(response):
    sdk = SimpleNamespace(
        chat=SimpleNamespace(completions=_Completions(response=response))
    )
    with pytest.raises(SearchInternalError):
        OpenAIStructuredJSONClient(sdk, model_name="model").complete(
            system_prompt='return json: {"ok":true}',
            user_prompt="question",
            timeout_seconds=2,
        )


@pytest.mark.parametrize(
    "error",
    [TimeoutError("timeout"), ConnectionError("offline"), requests.Timeout("timeout")],
)
def test_transport_errors_are_internal_and_not_retried(error):
    completions = _Completions(error=error)
    sdk = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    with pytest.raises(SearchInternalError):
        OpenAIStructuredJSONClient(sdk, model_name="model").complete(
            system_prompt='return json: {"ok":true}',
            user_prompt="question",
            timeout_seconds=2,
        )
    assert len(completions.calls) == 1


def test_provider_error_retains_only_sanitized_internal_diagnostic():
    class ProviderBalanceError(Exception):
        status_code = 402
        code = "insufficient_balance"

    completions = _Completions(
        error=ProviderBalanceError(
            "account acct-secret cannot pay with token sk-secret"
        )
    )
    sdk = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    with pytest.raises(SearchInternalError) as raised:
        OpenAIStructuredJSONClient(sdk, model_name="model").complete(
            system_prompt='return json: {"ok":true}',
            user_prompt="private customer question",
            timeout_seconds=2,
        )

    assert raised.value.internal_reason == (
        "ProviderBalanceError|status=402|code=insufficient_balance"
    )
    assert raised.value.safe_message == "knowledge search could not be completed"
    assert "acct-secret" not in raised.value.internal_reason
    assert "sk-secret" not in raised.value.internal_reason
