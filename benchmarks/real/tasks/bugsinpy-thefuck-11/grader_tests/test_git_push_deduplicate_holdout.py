from tests.utils import Command
from thefuck.rules.git_push import get_new_command


def test_old_upstream_option_is_not_repeated() -> None:
    stderr = (
        "fatal: The current branch topic has no upstream branch.\n"
        "To push and set the upstream, use\n\n"
        "    git push --set-upstream origin topic\n\n"
    )
    assert get_new_command(Command("git push -u origin", stderr=stderr)) == (
        "git push --set-upstream origin topic"
    )
