"""Small pagination helper used by the first RepoFix benchmark."""

from collections.abc import Sequence
from typing import TypeVar


T = TypeVar("T")


def paginate(items: Sequence[T], page: int, page_size: int) -> list[T]:
    """Return one 1-indexed page from *items*.

    ``page`` must be at least 1. The function is also expected to reject a
    non-positive ``page_size``, but the initial benchmark version contains a
    bug in that validation path.
    """

    if page < 1:
        raise ValueError("page must be greater than or equal to 1")

    # Calculating the page count before validating page_size causes the target
    # ZeroDivisionError when page_size is 0.
    total_pages = (len(items) + page_size - 1) // page_size
    if items and page > total_pages:
        return []

    start = (page - 1) * page_size
    end = start + page_size
    return list(items[start:end])
