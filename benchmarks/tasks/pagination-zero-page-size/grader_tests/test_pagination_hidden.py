import pytest

from pagination import paginate


@pytest.mark.parametrize("page_size", [-1, -5])
# -> None 不返回结果只负责测试
def test_rejects_negative_page_size(page_size: int) -> None:
    with pytest.raises(ValueError, match="page_size"):
        paginate([1, 2, 3], page=1, page_size=page_size)


def test_rejects_page_zero() -> None:
    with pytest.raises(ValueError, match="page"):
        paginate([1, 2, 3], page=0, page_size=2)


def test_empty_input_returns_empty_page() -> None:
    assert paginate([], page=1, page_size=3) == []


def test_out_of_range_page_returns_empty() -> None:
    assert paginate([1, 2, 3], page=4, page_size=2) == []


def test_page_size_larger_than_collection() -> None:
    assert paginate([1, 2, 3], page=1, page_size=10) == [1, 2, 3]
