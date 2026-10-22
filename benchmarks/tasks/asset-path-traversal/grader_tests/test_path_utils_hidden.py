import pytest

from path_utils import asset_path


def test_nested_parent_escape_is_rejected(tmp_path) -> None:
    with pytest.raises(ValueError, match="root"):
        asset_path(tmp_path, "images/../../outside.txt")


def test_absolute_path_is_rejected_even_when_inside_root(tmp_path) -> None:
    with pytest.raises(ValueError, match="relative"):
        asset_path(tmp_path, str(tmp_path / "inside.txt"))


def test_dot_component_inside_root_is_allowed(tmp_path) -> None:
    assert asset_path(tmp_path, "images/../logo.png") == (
        tmp_path / "logo.png"
    ).resolve()
