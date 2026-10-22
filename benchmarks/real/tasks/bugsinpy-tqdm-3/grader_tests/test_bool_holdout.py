"""Independent holdout cases for the real BugsInPy tqdm #3 defect."""

from __future__ import annotations

from io import StringIO

import pytest
from tqdm import tqdm


@pytest.mark.parametrize("items,expected", [([], False), ([1], True)])
def test_bool_uses_iterable_truth_value(items: list[int], expected: bool) -> None:
    with tqdm(items, file=StringIO(), disable=True) as progress:
        assert bool(progress) is expected


def test_bool_without_iterable_or_total_is_undefined() -> None:
    with tqdm(file=StringIO(), disable=True) as progress:
        with pytest.raises(TypeError):
            bool(progress)
