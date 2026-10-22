"""Independent holdout for BugsInPy tqdm #8 (custom bar fragments)."""

from tqdm import tqdm


def test_custom_bar_format_respects_width() -> None:
    rendered = tqdm.format_meter(
        50, 100, 5, ncols=16, rate=10, ascii=True,
        bar_format="{l_bar}{bar}|{n_fmt}/{total_fmt}",
    )
    assert rendered.endswith("|50/100")
    assert len(rendered) == 16
