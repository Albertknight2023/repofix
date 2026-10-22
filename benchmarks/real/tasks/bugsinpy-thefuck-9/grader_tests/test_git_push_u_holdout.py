from tests.utils import Command
from thefuck.rules.git_push import get_new_command


def test_trailing_short_upstream_option_does_not_crash() -> None:
    stderr = (
        "fatal: The current branch topic has no upstream branch.\n"
        "To set the remote as upstream, use\n\n"
        "    git push --set-upstream origin topic\n\n"
    )
    assert get_new_command(Command("git push -u", stderr=stderr)) == (
        "git push --set-upstream origin topic"
    )
