"""Independent holdout for BugsInPy tqdm #5."""

from tqdm import tqdm


def test_disabled_wrapper_preserves_iterable_length() -> None:
    with tqdm([1, 2, 3], disable=True) as progress:
        assert len(progress) == 3
        assert bool(progress)
    with tqdm([], disable=True) as progress:
        assert len(progress) == 0
        assert not bool(progress)
