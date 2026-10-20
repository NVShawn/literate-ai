"""Structural JSON uniqueness without pairwise object comparisons."""

from __future__ import annotations

import math


def _json_key(value: object) -> object:
    # JSON Schema equates 1 and 1.0, but distinguishes true from 1. Object
    # ordering is immaterial; array ordering and exact Unicode text matter.
    if value is None:
        return ("null",)
    if type(value) is bool:
        return ("boolean", value)
    if type(value) is int or (type(value) is float and math.isfinite(value)):
        return ("number", value)
    if type(value) is str:
        return ("string", value)
    if type(value) is list:
        return ("array", tuple(_json_key(item) for item in value))
    if type(value) is dict and all(type(key) is str for key in value):
        return (
            "object",
            frozenset((key, _json_key(item)) for key, item in value.items()),
        )
    raise TypeError("Structural uniqueness requires ordinary finite JSON")


def unique_json_items(validator, enabled, instance, schema):
    """Preserve Draft 7 uniqueItems semantics and its standard diagnostic."""

    if not enabled or not validator.is_type(instance, "array"):
        return
    from jsonschema import Draft7Validator
    from jsonschema.exceptions import ValidationError

    seen: set[object] = set()
    try:
        for item in instance:
            key = _json_key(item)
            if key in seen:
                yield ValidationError(f"{instance!r} has non-unique elements")
                return
            seen.add(key)
    except TypeError:
        # Not a decoded JSON value: retain the dependency's complete behavior.
        yield from Draft7Validator.VALIDATORS["uniqueItems"](
            validator, enabled, instance, schema
        )
