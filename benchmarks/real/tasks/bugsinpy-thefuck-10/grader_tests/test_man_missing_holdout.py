from tests.utils import Command
from thefuck.rules.man import get_new_command


def test_missing_manual_has_only_help_fallback() -> None:
    command = Command("man obscuretool", stderr="No manual entry for obscuretool\n")
    assert get_new_command(command) == ["obscuretool --help"]
