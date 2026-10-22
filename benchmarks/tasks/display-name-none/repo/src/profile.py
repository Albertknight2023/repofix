"""Public display-name formatting."""


def display_name(name: str | None) -> str:
    cleaned = name.strip()
    return cleaned or "Guest"
