"""Reusable public library with an intentionally internal cache."""

_CACHE: dict[str, str] = {}


def normalize(value: str) -> str:
    if value not in _CACHE:
        _CACHE[value] = value.strip().lower()
    return _CACHE[value]
