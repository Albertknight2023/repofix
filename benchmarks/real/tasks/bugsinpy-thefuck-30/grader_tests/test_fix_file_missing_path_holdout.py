from tests.utils import Command
from thefuck.rules.fix_file import match


def test_fix_file_rejects_nonexistent_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("EDITOR", "vi")
    missing = tmp_path / "absent-source.py"
    command = Command("python absent-source.py", stderr=f"{missing}:12:3: error: syntax error")

    assert not match(command, None)
