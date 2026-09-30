"""Private validation helpers shared by executable-Component contracts."""

from __future__ import annotations

import re
from typing import Any

from .._validation import fail, string_value, unique
from ..identity import ContentIdentity

_PORTABLE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,126}$")


def identity(value: object, path: str) -> ContentIdentity:
    if not isinstance(value, ContentIdentity):
        fail(path, "must be a ContentIdentity")
    return value


def optional_identity(value: Any, path: str) -> ContentIdentity | None:
    if value is None:
        return None
    return ContentIdentity.from_dict(value, path=path)


def portable_name(value: str, path: str) -> str:
    raw = string_value(value, path, max_length=127)
    if _PORTABLE_NAME.fullmatch(raw) is None:
        fail(path, "must be a portable lower-case identifier")
    return raw


def tuple_value(value: object, path: str) -> tuple[object, ...]:
    if not isinstance(value, tuple):
        fail(path, "must be a tuple")
    return value


def texts(
    value: object,
    path: str,
    *,
    required: bool = False,
    maximum_items: int = 256,
) -> tuple[str, ...]:
    values = tuple_value(value, path)
    if required and not values:
        fail(path, "must not be empty")
    if len(values) > maximum_items:
        fail(path, f"must contain at most {maximum_items} values")
    parsed = tuple(
        string_value(item, f"{path}[{index}]", max_length=4096)
        for index, item in enumerate(values)
    )
    unique(parsed, path)
    return parsed


def canonical_identities(
    value: object, path: str, *, required: bool = False
) -> tuple[ContentIdentity, ...]:
    values = tuple_value(value, path)
    if required and not values:
        fail(path, "must not be empty")
    identities = tuple(
        identity(item, f"{path}[{index}]") for index, item in enumerate(values)
    )
    uris = tuple(item.uri for item in identities)
    unique(uris, path, "identities")
    if uris != tuple(sorted(uris)):
        fail(path, "must use canonical identity order")
    return identities


__all__ = [
    "canonical_identities",
    "identity",
    "optional_identity",
    "portable_name",
    "texts",
    "tuple_value",
]
