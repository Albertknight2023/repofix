"""Independent holdout for BugsInPy tqdm #4 (unknown total + scaling)."""

from io import StringIO

from tqdm import tqdm


def test_unknown_total_numeric_unit_scale() -> None:
    output = StringIO()
    assert list(tqdm((item for item in range(3)), file=output, unit_scale=5)) == [0, 1, 2]
    assert "15it" in output.getvalue()
