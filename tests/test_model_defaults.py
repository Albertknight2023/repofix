"""Changing the project model default must not require a network request."""

from pathlib import Path

from repofix.cli import _create_client, _parser


def test_api_defaults_to_pro_when_no_model_override(tmp_path: Path, monkeypatch) -> None:
    for name in ("REPOFIX_API_KEY", "DEEPSEEK_API_KEY", "REPOFIX_BASE_URL", "REPOFIX_MODEL"):
        monkeypatch.delenv(name, raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("DEEPSEEK_API_KEY=offline-test-key\n", encoding="utf-8")
    args = _parser().parse_args([
        "repair", "--task", str(tmp_path / "task.yaml"),
        "--provider", "api", "--env-file", str(env_path),
    ])

    client = _create_client(args)

    assert client.model_name == "deepseek-v4-pro"
    assert client.max_output_tokens == 3000
