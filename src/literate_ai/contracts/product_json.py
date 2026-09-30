"""Finite application JSON, separate from integer-only canonical contract JSON v1.

Sorted UTF-8 JSON uses compact separators and preserves numeric values (including
negative zero). Finite binary64 values use Python's shortest round-trip JSON spelling.
Integer-only documents retain the exact bytes and identities of contract JSON v1.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any

from ._validation import fail
from .identity import ContentIdentity, HashAlgorithm, _canonical_value


def _product_value(value: Any, path: str = "$") -> Any:
    if isinstance(value, float):
        if not math.isfinite(value):
            fail(path, "product JSON numbers must be finite")
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            fail(path, "product JSON object keys must be strings")
        return {
            key: _product_value(item, f"{path}.{key}") for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            _product_value(item, f"{path}[{index}]") for index, item in enumerate(value)
        ]
    return _canonical_value(value, path)


def product_json_bytes(value: Any) -> bytes:
    """Encode finite product values without changing contract serialization."""
    return json.dumps(
        _product_value(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def product_json_identity(value: Any) -> ContentIdentity:
    """Bind the actual numeric JSON document, including fractional values."""
    return ContentIdentity(
        HashAlgorithm.SHA256,
        hashlib.sha256(product_json_bytes(value)).hexdigest(),
    )


def product_json_values_equal(left: Any, right: Any) -> bool:
    """Compare exact JSON values, allowing equivalent integer/float spellings.

    This does not normalize retained bytes or identities. Boolean values remain
    distinct from numbers, negative zero retains its sign, and no tolerance or
    integer-to-float rounding is introduced.
    """

    def equal(a: Any, b: Any) -> bool:
        if type(a) in (int, float) and type(b) in (int, float):
            return a == b and (a != 0 or math.copysign(1, a) == math.copysign(1, b))
        if type(a) is not type(b):
            return False
        if isinstance(a, dict):
            return a.keys() == b.keys() and all(equal(a[key], b[key]) for key in a)
        if isinstance(a, list):
            return len(a) == len(b) and all(
                equal(x, y) for x, y in zip(a, b, strict=True)
            )
        return a == b

    # Encoding validates finiteness, bounds, keys and UTF-8; decoding gives plain
    # JSON primitives even when callers supply supported Mapping/Sequence values.
    return equal(
        json.loads(product_json_bytes(left)), json.loads(product_json_bytes(right))
    )
