"""Verify the provider adapter without making an external API request."""

import json
from http.client import IncompleteRead
from io import BytesIO

import pytest

from repofix.llm import ChatMessage, OpenAICompatibleClient
from repofix.llm.client import IncompleteModelResponseError


def test_compatible_client_posts_json_and_reads_usage(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(request, *, timeout):
        captured["url"] = request.full_url
        captured["authorization"] = request.get_header("Authorization")
        captured["payload"] = json.loads(request.data)
        captured["timeout"] = timeout
        return BytesIO(
            json.dumps(
                {
                    "model": "test-model",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": '{"status":"insufficient_evidence"}'},
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 21, "completion_tokens": 8,
                        "completion_tokens_details": {"reasoning_tokens": 3},
                    },
                }
            ).encode("utf-8")
        )

    monkeypatch.setattr("repofix.llm.client.urlopen", fake_urlopen)
    client = OpenAICompatibleClient(
        api_key="test-key",
        base_url="https://example.invalid/v1",
        model="test-model",
        timeout_seconds=12,
        thinking_mode="disabled",
    )

    response = client.complete(
        (ChatMessage(role="system", content="Return JSON"),)
    )

    assert captured["url"] == "https://example.invalid/v1/chat/completions"
    assert captured["authorization"] == "Bearer test-key"
    assert captured["payload"]["response_format"] == {"type": "json_object"}
    assert captured["payload"]["messages"][0]["role"] == "system"
    assert captured["payload"]["thinking"] == {"type": "disabled"}
    assert captured["timeout"] == 12
    assert response.model == "test-model"
    assert response.prompt_tokens == 21
    assert response.completion_tokens == 8
    assert response.reasoning_tokens == 3


def test_compatible_client_rejects_incomplete_response(monkeypatch) -> None:
    def fake_urlopen(request, *, timeout):
        del request, timeout
        return BytesIO(
            b'{"choices":[{"finish_reason":"length","message":{"content":"{}"}}],'
            b'"usage":{"prompt_tokens":12,"completion_tokens":6000,'
            b'"completion_tokens_details":{"reasoning_tokens":5500}}}'
        )

    monkeypatch.setattr("repofix.llm.client.urlopen", fake_urlopen)
    client = OpenAICompatibleClient(
        api_key="test-key",
        base_url="https://example.invalid",
        model="test-model",
    )

    with pytest.raises(IncompleteModelResponseError, match="usable answer") as caught:
        client.complete((ChatMessage(role="user", content="JSON"),))
    assert caught.value.prompt_tokens == 12
    assert caught.value.completion_tokens == 6000
    assert caught.value.reasoning_tokens == 5500


def test_empty_json_content_keeps_provider_usage(monkeypatch) -> None:
    def fake_urlopen(request, *, timeout):
        del request, timeout
        return BytesIO(
            b'{"choices":[{"finish_reason":"stop","message":{"content":"  "}}],'
            b'"usage":{"prompt_tokens":12,"completion_tokens":6000,'
            b'"completion_tokens_details":{"reasoning_tokens":5900}}}'
        )

    monkeypatch.setattr("repofix.llm.client.urlopen", fake_urlopen)
    client = OpenAICompatibleClient(
        api_key="test-key", base_url="https://example.invalid", model="test-model",
    )

    with pytest.raises(IncompleteModelResponseError) as caught:
        client.complete((ChatMessage(role="user", content="Return JSON"),))
    assert caught.value.finish_reason == "empty_content"
    assert caught.value.prompt_tokens == 12
    assert caught.value.completion_tokens == 6000
    assert caught.value.reasoning_tokens == 5900


def test_interrupted_chunked_body_is_a_retryable_model_error(monkeypatch) -> None:
    class InterruptedBody:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self, _limit):
            raise IncompleteRead(b"")

    monkeypatch.setattr(
        "repofix.llm.client.urlopen",
        lambda request, *, timeout: InterruptedBody(),
    )
    client = OpenAICompatibleClient(
        api_key="test-key", base_url="https://example.invalid", model="test-model",
    )

    with pytest.raises(RuntimeError, match="response was interrupted"):
        client.complete((ChatMessage(role="user", content="Return JSON"),))


def test_compatible_client_sends_low_reasoning_effort(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(request, *, timeout):
        del timeout
        captured["payload"] = json.loads(request.data)
        return BytesIO(
            b'{"model":"test-model","choices":[{"finish_reason":"stop",'
            b'"message":{"content":"{}"}}]}'
        )

    monkeypatch.setattr("repofix.llm.client.urlopen", fake_urlopen)
    client = OpenAICompatibleClient(
        api_key="test-key", base_url="https://example.invalid",
        model="test-model", thinking_mode="enabled", reasoning_effort="low",
    )

    client.complete((ChatMessage(role="user", content="Return JSON"),))

    assert captured["payload"]["thinking"] == {"type": "enabled"}
    assert captured["payload"]["reasoning_effort"] == "low"
    assert client.reasoning_effort == "low"


def test_default_model_timeout_is_long_enough_for_pro() -> None:
    client = OpenAICompatibleClient(
        api_key="test-key", base_url="https://example.invalid", model="test-model"
    )
    assert client._timeout_seconds == 180


def test_direct_mode_uses_no_system_proxy(monkeypatch) -> None:
    observed: dict[str, object] = {}

    class FakeOpener:
        def open(self, request, *, timeout):
            observed["url"] = request.full_url
            observed["timeout"] = timeout
            return BytesIO(
                b'{"model":"test-model","choices":[{"finish_reason":"stop",'
                b'"message":{"content":"{}"}}]}'
            )

    def fake_build_opener(handler):
        observed["proxy_map"] = handler.proxies
        return FakeOpener()

    monkeypatch.setattr("repofix.llm.client.build_opener", fake_build_opener)
    client = OpenAICompatibleClient(
        api_key="test-key",
        base_url="https://example.invalid",
        model="test-model",
        proxy_mode="direct",
    )

    response = client.complete((ChatMessage(role="user", content="JSON"),))

    assert observed["proxy_map"] == {}
    assert observed["url"] == "https://example.invalid/chat/completions"
    assert response.model == "test-model"
