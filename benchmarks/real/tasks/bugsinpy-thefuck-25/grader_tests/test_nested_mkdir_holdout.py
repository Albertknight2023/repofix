from tests.utils import Command
from thefuck.rules.mkdir_p import get_new_command


def test_mkdir_inside_composite_command_gets_parent_flag() -> None:
    command = Command("env X=1 mkdir nested/path")
    assert get_new_command(command, None) == "env X=1 mkdir -p nested/path"
