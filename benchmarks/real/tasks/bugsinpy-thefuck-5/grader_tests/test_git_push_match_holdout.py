from thefuck.rules.git_push import match
from thefuck.types import Command


def test_unrelated_upstream_text_does_not_trigger_rule() -> None:
    output = "remote: see https://example.invalid/docs/set-upstream for help"
    assert not match(Command("git push origin topic", output))
