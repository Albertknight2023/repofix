from tests.utils import Command
from thefuck.rules.git_diff_staged import get_new_command


def test_staged_flag_precedes_existing_diff_options() -> None:
    assert get_new_command(Command("git diff --stat HEAD~2"), None) == (
        "git diff --staged --stat HEAD~2"
    )
