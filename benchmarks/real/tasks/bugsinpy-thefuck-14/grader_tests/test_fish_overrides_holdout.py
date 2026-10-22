from thefuck.shells import Fish


def test_custom_aliases_extend_defaults(monkeypatch) -> None:
    monkeypatch.setenv("TF_OVERRIDDEN_ALIASES", "awk, sed")
    assert set(Fish()._get_overridden_aliases()) == {
        "cd", "grep", "ls", "man", "open", "awk", "sed",
    }
