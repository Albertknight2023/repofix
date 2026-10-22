"""Independent holdout cases for the real BugsInPy tqdm #1 defect."""

from tqdm.contrib import tenumerate


def test_nonzero_start_with_generator() -> None:
    values = (value * 2 for value in range(4))
    assert list(tenumerate(values, start=7, disable=True)) == [
        (7, 0), (8, 2), (9, 4), (10, 6)
    ]


def test_negative_start() -> None:
    assert list(tenumerate(["a", "b"], start=-2, disable=True)) == [
        (-2, "a"), (-1, "b")
    ]
