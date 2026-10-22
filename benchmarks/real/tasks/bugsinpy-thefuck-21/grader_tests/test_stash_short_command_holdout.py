from tests.utils import Command
from thefuck.rules.git_fix_stash import match


def test_bare_git_command_does_not_raise_index_error() -> None:
    assert not match(Command("git ", stderr="usage: git stash pop"))
