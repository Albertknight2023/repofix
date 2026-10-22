"""Independent CLI checks for BugsInPy tqdm #7."""

from io import StringIO
import sys

from tqdm import main


def test_desc_value_keeps_internal_hyphen(monkeypatch) -> None:
    output = StringIO()
    progress = StringIO()
    monkeypatch.setattr(sys, "argv", ["tqdm", "--desc", "build-step", "--total", "1"])
    monkeypatch.setattr(sys, "stdin", StringIO("one\n"))
    monkeypatch.setattr(sys, "stdout", output)

    main(fp=progress)

    assert output.getvalue() == "one\n"
    assert "build-step" in progress.getvalue()


def test_unit_value_keeps_internal_hyphen(monkeypatch) -> None:
    output = StringIO()
    progress = StringIO()
    monkeypatch.setattr(sys, "argv", ["tqdm", "--unit", "part-time", "--total", "1"])
    monkeypatch.setattr(sys, "stdin", StringIO("one\n"))
    monkeypatch.setattr(sys, "stdout", output)

    main(fp=progress)

    assert output.getvalue() == "one\n"
    assert "part-time" in progress.getvalue()
