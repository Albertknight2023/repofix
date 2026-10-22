from key_utils import normalize_key
from settings import get_setting


def test_normalize_key_removes_spaces_and_folds_case() -> None:
    assert normalize_key("  THEME  ") == "theme"


def test_get_setting_with_exact_key() -> None:
    assert get_setting({"theme": "dark"}, "theme") == "dark"
