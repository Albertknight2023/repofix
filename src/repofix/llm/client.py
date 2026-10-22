"""A small, provider-independent interface for chat-completion models."""

import json
from http.client import HTTPException
from dataclasses import dataclass, field
from typing import Literal, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener, urlopen


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True, slots=True)
class ModelResponse:
    content: str
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    reasoning_tokens: int | None = None


class IncompleteModelResponseError(ValueError):
    """An API response stopped early; retain any usage reported by the provider."""

    def __init__(self, reason: str, usage: object) -> None:
        super().__init__(f"model response ended before a usable answer ({reason})")
        self.finish_reason = reason
        self.prompt_tokens = _token_count(usage, "prompt_tokens")
        self.completion_tokens = _token_count(usage, "completion_tokens")
        self.reasoning_tokens = _reasoning_token_count(usage)


class ModelClient(Protocol):
    """The model operation required by RepoFix's current agents."""

    def complete(self, messages: tuple[ChatMessage, ...]) -> ModelResponse:
        ...


@dataclass(slots=True)
class ScriptedModelClient:
    """Return a fixed response for reproducible, network-free tests."""

    response_text: str
    calls: list[tuple[ChatMessage, ...]] = field(default_factory=list)

    def complete(self, messages: tuple[ChatMessage, ...]) -> ModelResponse:
        self.calls.append(messages)
        return ModelResponse(content=self.response_text, model="scripted-mock")


@dataclass(slots=True)
class ScriptedSequenceClient:
    """Return one scripted answer per call for offline multi-candidate tests."""

    responses: tuple[str, ...]
    calls: list[tuple[ChatMessage, ...]] = field(default_factory=list)

    def complete(self, messages: tuple[ChatMessage, ...]) -> ModelResponse:
        if len(self.calls) >= len(self.responses):
            raise RuntimeError("scripted model responses exhausted")
        response = self.responses[len(self.calls)]
        self.calls.append(messages)
        return ModelResponse(content=response, model="scripted-mock")


def _token_count(usage: object, field_name: str) -> int | None:
    if not isinstance(usage, dict):
        return None
    value = usage.get(field_name)
    return value if type(value) is int and value >= 0 else None


def _reasoning_token_count(usage: object) -> int | None:
    if not isinstance(usage, dict):
        return None
    return _token_count(usage.get("completion_tokens_details"), "reasoning_tokens")


class OpenAICompatibleClient:
    """Call a configurable OpenAI-compatible chat-completions endpoint."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout_seconds: float = 180,
        max_tokens: int = 1200,
        proxy_mode: str = "system",
        thinking_mode: str | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        parsed = urlparse(base_url)
        local_host = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and local_host
        ):
            raise ValueError("model base_url must use HTTPS or local HTTP")
        if not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("model base_url must be a plain service URL")
        if not api_key.strip():
            raise ValueError("model API key must not be empty")
        if not model.strip():
            raise ValueError("model name must not be empty")
        if timeout_seconds <= 0 or max_tokens < 1:
            raise ValueError("model timeout and max_tokens must be positive")
        if proxy_mode not in {"system", "direct"}:
            raise ValueError("model proxy_mode must be system or direct")
        if thinking_mode not in {None, "enabled", "disabled"}:
            raise ValueError("thinking_mode must be enabled, disabled, or unset")
        if reasoning_effort not in {None, "low", "high", "max"}:
            raise ValueError("reasoning_effort must be low, high, max, or unset")
        if thinking_mode == "disabled" and reasoning_effort is not None:
            raise ValueError("reasoning_effort requires thinking mode")

        self._api_key = api_key
        self._endpoint = base_url.rstrip("/") + "/chat/completions"
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._max_tokens = max_tokens
        self._proxy_mode = proxy_mode
        self._thinking_mode = thinking_mode
        self._reasoning_effort = reasoning_effort

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def max_output_tokens(self) -> int:
        return self._max_tokens

    @property
    def thinking_mode(self) -> str | None:
        return self._thinking_mode

    @property
    def reasoning_effort(self) -> str | None:
        return self._reasoning_effort

    @property
    def timeout_seconds(self) -> float:
        return self._timeout_seconds

    def complete(self, messages: tuple[ChatMessage, ...]) -> ModelResponse:
        if not messages:
            raise ValueError("model messages must not be empty")

        payload = {
            "model": self._model,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in messages
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": self._max_tokens,
            "stream": False,
        }
        if self._thinking_mode is not None:
            payload["thinking"] = {"type": self._thinking_mode}
        if self._reasoning_effort is not None:
            payload["reasoning_effort"] = self._reasoning_effort
        request = Request(
            self._endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            if self._proxy_mode == "direct":
                connection = build_opener(ProxyHandler({})).open(
                    request, timeout=self._timeout_seconds
                )
            else:
                connection = urlopen(request, timeout=self._timeout_seconds)
            with connection as response:
                raw_response = response.read(2_000_001)
        except HTTPError as error:
            raise RuntimeError(
                f"model API returned HTTP {error.code}"
            ) from error
        except URLError as error:
            raise RuntimeError("model API connection failed") from error
        except HTTPException as error:
            # The server or proxy can close a chunked body midway through a
            # response. Treat it as a retryable transport failure rather than
            # crashing the entire formal experiment.
            raise RuntimeError("model API response was interrupted") from error

        if len(raw_response) > 2_000_000:
            raise ValueError("model API response exceeds size limit")

        try:
            response_data = json.loads(raw_response)
            choice = response_data["choices"][0]
            finish_reason = choice.get("finish_reason")
            if finish_reason in {
                "length",
                "content_filter",
                "insufficient_system_resource",
                "aborted",
            }:
                raise IncompleteModelResponseError(
                    finish_reason, response_data.get("usage")
                )
            content = choice["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise IncompleteModelResponseError(
                    "empty_content", response_data.get("usage")
                )
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
            raise ValueError("model API returned an invalid response") from error

        return ModelResponse(
            content=content,
            model=str(response_data.get("model", self._model)),
            prompt_tokens=_token_count(
                response_data.get("usage"), "prompt_tokens"
            ),
            completion_tokens=_token_count(
                response_data.get("usage"), "completion_tokens"
            ),
            reasoning_tokens=_reasoning_token_count(response_data.get("usage")),
        )
