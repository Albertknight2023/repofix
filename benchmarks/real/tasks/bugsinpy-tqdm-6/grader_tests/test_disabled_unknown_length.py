"""Independent regression check for BugsInPy tqdm #6.

This candidate still needs container qualification before becoming ready.
"""

from tqdm import tqdm


def test_disabled_unsized_iterator_len_falls_back_to_none() -> None:
    progress = tqdm((value for value in range(3)), disable=True)

    assert progress.__len__() is None


def test_disabled_iterator_with_explicit_total_still_has_safe_len() -> None:
    progress = tqdm((value for value in range(3)), total=3, disable=True)

    assert progress.__len__() is None
