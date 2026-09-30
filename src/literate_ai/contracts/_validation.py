"""Small, dependency-free helpers for strict wire-contract validation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, NoReturn


class ContractValidationError(ValueError):
    """A portable contract value failed structural or semantic validation."""

    def __init__(self, path: str, message: str) -> None:
        self.path = path
        self.message = message
        super().__init__(f"{path}: {message}")


def fail(path: str, message: str) -> NoReturn:
    raise ContractValidationError(path, message)


def object_value(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        fail(path, "must be an object")
    for key in value:
        if not isinstance(key, str):
            fail(path, "object keys must be strings")
    return value


def fields(
    value: Any,
    *,
    path: str,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> Mapping[str, Any]:
    data = object_value(value, path)
    keys = frozenset(data)
    missing = required - keys
    if missing:
        fail(path, f"missing required fields: {', '.join(sorted(missing))}")
    unknown = keys - required - optional
    if unknown:
        fail(path, f"unknown fields: {', '.join(sorted(unknown))}")
    return data


def contract_fields(
    value: Any,
    *,
    path: str,
    schema_uri: str,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> Mapping[str, Any]:
    data = fields(
        value,
        path=path,
        required=required | {"schema"},
        optional=optional,
    )
    schema = string_value(data["schema"], f"{path}.schema")
    if schema != schema_uri:
        fail(f"{path}.schema", f"must be {schema_uri!r}")
    return data


def string_value(
    value: Any,
    path: str,
    *,
    nonempty: bool = True,
    max_length: int = 4096,
) -> str:
    if not isinstance(value, str):
        fail(path, "must be a string")
    if nonempty and not value:
        fail(path, "must not be empty")
    if len(value) > max_length:
        fail(path, f"must contain at most {max_length} characters")
    return value


def optional_string(value: Any, path: str) -> str | None:
    if value is None:
        return None
    return string_value(value, path)


def bool_value(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        fail(path, "must be a boolean")
    return value


def int_value(
    value: Any,
    path: str,
    *,
    minimum: int = 0,
    maximum: int = 2**63 - 1,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        fail(path, "must be an integer")
    if not minimum <= value <= maximum:
        fail(path, f"must be between {minimum} and {maximum}")
    return value


def list_value(value: Any, path: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        fail(path, "must be an array")
    return value


def mapping_value(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        fail(path, "must be a JSON object")
    return value


def string_tuple(value: Any, path: str) -> tuple[str, ...]:
    return tuple(
        string_value(item, f"{path}[{index}]")
        for index, item in enumerate(list_value(value, path))
    )


def unique(values: Sequence[Any], path: str, label: str = "values") -> None:
    try:
        if len(set(values)) != len(values):
            fail(path, f"{label} must be unique")
    except TypeError:
        fail(path, f"{label} must be hashable")


def enum_value(enum_type: type[Any], value: Any, path: str) -> Any:
    raw = string_value(value, path)
    try:
        return enum_type(raw)
    except ValueError:
        allowed = ", ".join(sorted(item.value for item in enum_type))
        fail(path, f"must be one of: {allowed}")


def parse_tuple(value: Any, path: str, parser: Any) -> tuple[Any, ...]:
    return tuple(
        parser(item, path=f"{path}[{index}]")
        for index, item in enumerate(list_value(value, path))
    )
