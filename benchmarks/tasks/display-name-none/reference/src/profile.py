"""Public display-name formatting."""


def display_name(name: str | None) -> str:
    if name is None:
        return "Guest"
    cleaned = name.strip()
    return cleaned or "Guest"
