from key_utils import normalize_key
from settings import get_setting


def test_get_setting_with_mixed_case_and_spaces() -> None:
    assert get_setting({"language": "zh"}, " LANGUAGE ") == "zh"


def test_unicode_casefold_differs_from_lower() -> None:
    assert normalize_key("  Straße ") == "strasse"


def test_unknown_key_returns_none() -> None:
    assert get_setting({"theme": "dark"}, " missing ") is None
