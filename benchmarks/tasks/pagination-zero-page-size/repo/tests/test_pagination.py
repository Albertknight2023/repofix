import pytest

from pagination import paginate


def test_returns_first_page() -> None:
    assert paginate([1, 2, 3, 4, 5], page=1, page_size=2) == [1, 2]


def test_returns_partial_last_page() -> None:
    assert paginate([1, 2, 3, 4, 5], page=3, page_size=2) == [5]


def test_rejects_zero_page_size() -> None:
    with pytest.raises(ValueError, match="page_size"):
        paginate([1, 2, 3], page=1, page_size=0)
