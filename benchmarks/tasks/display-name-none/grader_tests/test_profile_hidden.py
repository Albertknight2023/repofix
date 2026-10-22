from profile import display_name


def test_empty_name_uses_guest_name() -> None:
    assert display_name("") == "Guest"


def test_whitespace_only_uses_guest_name() -> None:
    assert display_name(" \t ") == "Guest"


def test_preserves_case_of_nonempty_name() -> None:
    assert display_name("  McKay  ") == "McKay"
