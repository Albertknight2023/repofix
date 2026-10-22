from thefuck.rules.pip_unknown_command import get_new_command
from thefuck.types import Command


def test_unknown_command_with_hyphen_and_underscore() -> None:
    command = Command(
        "pip foo-bar package",
        'ERROR: unknown command "foo-bar" - maybe you meant "foo_bar"',
    )

    assert get_new_command(command) == "pip foo_bar package"
