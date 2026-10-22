from tests.utils import Command
from thefuck.rules.open import get_new_command


def test_xdg_open_preserves_executable_name_and_url() -> None:
    assert get_new_command(Command("xdg-open docs.example.net/page"), None) == (
        "xdg-open http://docs.example.net/page"
    )
