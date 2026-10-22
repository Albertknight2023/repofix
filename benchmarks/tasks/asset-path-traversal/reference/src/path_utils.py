"""Resolve application asset paths."""

from pathlib import Path


def asset_path(root: Path, relative: str) -> Path:
    base = root.resolve()
    if Path(relative).is_absolute():
        raise ValueError("relative path required")
    candidate = (base / relative).resolve()
    if not candidate.is_relative_to(base):
        raise ValueError("path is outside root")
    return candidate
