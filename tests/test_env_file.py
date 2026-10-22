"""Verify local API credentials can be loaded without a shell assignment."""

from argparse import Namespace
from pathlib import Path

import pytest

from repofix import cli
from repofix.llm.env_file import read_env_file


def test_reads_key_and_optional_model_from_env_file(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "# Local configuration\n"
        "DEEPSEEK_API_KEY='test-file-key'\n"
        "REPOFIX_MODEL=example-model # comment\n",
        encoding="utf-8",
    )

    assert read_env_file(env_path) == {
        "DEEPSEEK_API_KEY": "test-file-key",
        "REPOFIX_MODEL": "example-model",
    }


def test_cli_uses_env_file_without_process_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "DEEPSEEK_API_KEY=file-key\n"
        "REPOFIX_BASE_URL=https://example.invalid/v1\n"
        "REPOFIX_MODEL=file-model\n"
        "REPOFIX_PROXY_MODE=direct\n",
        encoding="utf-8",
    )
    for name in (
        "REPOFIX_API_KEY",
        "DEEPSEEK_API_KEY",
        "REPOFIX_BASE_URL",
        "REPOFIX_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        cli,
        "OpenAICompatibleClient",
        lambda **settings: settings,
    )

    client_settings = cli._create_client(
        Namespace(
            provider="api",
            env_file=env_path,
            base_url=None,
            model=None,
        )
    )

    assert client_settings["api_key"] == "file-key"
    assert client_settings["base_url"] == "https://example.invalid/v1"
    assert client_settings["model"] == "file-model"
    assert client_settings["proxy_mode"] == "direct"


def test_process_environment_takes_priority_over_env_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("DEEPSEEK_API_KEY=file-key\n", encoding="utf-8")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "process-key")
    monkeypatch.delenv("REPOFIX_API_KEY", raising=False)
    monkeypatch.setattr(
        cli,
        "OpenAICompatibleClient",
        lambda **settings: settings,
    )

    client_settings = cli._create_client(
        Namespace(
            provider="api",
            env_file=env_path,
            base_url=None,
            model=None,
        )
    )

    assert client_settings["api_key"] == "process-key"


def test_reports_invalid_env_line_without_echoing_secret(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("accidental-secret-value\n", encoding="utf-8")

    with pytest.raises(ValueError, match="line 1") as error:
        read_env_file(env_path)

    assert "accidental-secret-value" not in str(error.value)


def test_cli_allows_model_timeout_from_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "DEEPSEEK_API_KEY=file-key\nREPOFIX_MODEL_TIMEOUT_SECONDS=240\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("REPOFIX_MODEL_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("REPOFIX_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(cli, "OpenAICompatibleClient", lambda **settings: settings)
    settings = cli._create_client(Namespace(
        provider="api", env_file=env_path, base_url=None, model=None,
        model_timeout=None,
    ))
    assert settings["timeout_seconds"] == 240


def test_cli_allows_explicit_thinking_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "DEEPSEEK_API_KEY=file-key\nREPOFIX_THINKING=enabled\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("REPOFIX_THINKING", raising=False)
    monkeypatch.delenv("REPOFIX_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(cli, "OpenAICompatibleClient", lambda **settings: settings)
    settings = cli._create_client(Namespace(
        provider="api", env_file=env_path, base_url=None, model=None,
        thinking="disabled",
    ))
    assert settings["thinking_mode"] == "disabled"


def test_cli_allows_explicit_reasoning_effort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("DEEPSEEK_API_KEY=file-key\n", encoding="utf-8")
    monkeypatch.delenv("REPOFIX_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(cli, "OpenAICompatibleClient", lambda **settings: settings)
    settings = cli._create_client(Namespace(
        provider="api", env_file=env_path, base_url=None, model=None,
        thinking="enabled", reasoning_effort="low",
    ))
    assert settings["reasoning_effort"] == "low"
