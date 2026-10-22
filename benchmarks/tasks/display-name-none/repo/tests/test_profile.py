from profile import display_name


def test_trims_a_regular_name() -> None:
    assert display_name("  Alice  ") == "Alice"


def test_none_uses_guest_name() -> None:
    assert display_name(None) == "Guest"
