"""Canonical keys for configuration lookups."""


def normalize_key(key: str) -> str:
    return key.strip().casefold()
