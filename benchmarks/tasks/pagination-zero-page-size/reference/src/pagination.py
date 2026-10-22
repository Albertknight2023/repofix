"""Human-authored reference implementation for benchmark verification only."""

from collections.abc import Sequence
from typing import TypeVar


T = TypeVar("T")


def paginate(items: Sequence[T], page: int, page_size: int) -> list[T]:
    """Return one 1-indexed page from *items*."""

    if page < 1:
        raise ValueError("page must be greater than or equal to 1")
    if page_size <= 0:
        raise ValueError("page_size must be greater than 0")

    total_pages = (len(items) + page_size - 1) // page_size
    if items and page > total_pages:
        return []

    start = (page - 1) * page_size
    end = start + page_size
    return list(items[start:end])
