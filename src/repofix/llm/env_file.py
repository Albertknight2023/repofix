"""Read the small KEY=VALUE subset of .env used by RepoFix."""

import re
from pathlib import Path


_KEY_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def read_env_file(path: Path) -> dict[str, str]:
    """Read local configuration without changing the process environment.

    Blank lines, comments, bare values, and simple quoted values are supported.
    The caller decides how file values combine with actual environment values.
    """

    if not path.exists():
        return {}
    if not path.is_file():
        raise ValueError(f"environment file is not a file: {path}")

    settings: dict[str, str] = {}
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8-sig").splitlines(),
        start=1,
    ):
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        if "=" not in entry:
            raise ValueError(f"invalid .env entry at line {line_number}")

        name, value = (part.strip() for part in entry.split("=", 1))
        if not _KEY_PATTERN.fullmatch(name):
            raise ValueError(f"invalid .env key at line {line_number}")

        if value.startswith(('"', "'")):
            quote = value[0]
            if len(value) < 2 or not value.endswith(quote):
                raise ValueError(f"invalid quoted .env value at line {line_number}")
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()

        settings[name] = value

    return settings
