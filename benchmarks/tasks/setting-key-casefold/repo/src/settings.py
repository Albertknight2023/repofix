"""Read a setting using the shared key normalizer."""

from collections.abc import Mapping

from key_utils import normalize_key


def get_setting(settings: Mapping[str, str], key: str) -> str | None:
    return settings.get(normalize_key(key))
