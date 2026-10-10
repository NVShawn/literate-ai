"""Bounded Elixir package namespaces and native export identifiers."""

import re


def elixir_namespace(package: str) -> str:
    """Map a native application/package identifier to its module namespace."""
    if re.fullmatch(r"[a-z][a-z0-9_]*", package) is None:
        raise ValueError("Elixir packages require a lowercase native identifier")
    return "".join(part[:1].upper() + part[1:] for part in package.split("_"))


def is_elixir_module(value: str) -> bool:
    return (
        re.fullmatch(r"[A-Z][A-Za-z0-9_]*(?:\.[A-Z][A-Za-z0-9_]*)*", value) is not None
    )


def is_elixir_symbol(value: str) -> bool:
    return re.fullmatch(r"[a-z_][A-Za-z0-9_]*[!?]?", value) is not None
