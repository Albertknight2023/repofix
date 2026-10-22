"""Independent holdout for BugsInPy PySnooper #3."""

from pysnooper.pysnooper import get_write_function


def test_writer_appends_to_given_path(tmp_path) -> None:
    output = tmp_path / "trace.log"
    write = get_write_function(str(output))
    write("first\n")
    write("second\n")
    assert output.read_text(encoding="utf-8") == "first\nsecond\n"
