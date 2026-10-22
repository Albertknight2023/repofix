from mock import Mock
from thefuck.rules.ls_lah import match


def test_unrelated_command_containing_ls_does_not_match() -> None:
    assert not match(Mock(script="printf false"), None)
