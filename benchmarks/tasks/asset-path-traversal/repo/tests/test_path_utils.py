import pytest

from path_utils import asset_path


def test_regular_asset_path_stays_under_root(tmp_path) -> None:
    assert asset_path(tmp_path, "images/logo.png") == (
        tmp_path / "images" / "logo.png"
    ).resolve()


def test_parent_escape_is_rejected(tmp_path) -> None:
    with pytest.raises(ValueError, match="root"):
        asset_path(tmp_path, "../outside.txt")
