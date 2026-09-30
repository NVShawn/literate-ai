"""Semantic-version range evaluation for capability requirements."""

from __future__ import annotations

import re

from literate_ai.contracts.versioning import SemanticVersion

_TERM_RE = re.compile(r"^(>=|<=|>|<|==|=|\^|~)?\s*(.+)$")


def _range_bound(value: str) -> SemanticVersion:
    """Accept conventional partial bounds while object versions stay strict."""

    parts = value.split("-", 1)[0].split("+", 1)[0].split(".")
    if len(parts) < 3 and all(part.isdigit() for part in parts):
        value = value + ".0" * (3 - len(parts))
    return SemanticVersion.parse(value)


def _caret_upper_bound(raw_bound: str, bound: SemanticVersion) -> SemanticVersion:
    """Return the first incompatible version for one conventional caret range."""

    numeric = raw_bound.split("-", 1)[0].split("+", 1)[0]
    precision = len(numeric.split("."))
    if bound.major > 0 or precision == 1:
        return SemanticVersion(bound.major + 1, 0, 0)
    if bound.minor > 0 or precision == 2:
        return SemanticVersion(0, bound.minor + 1, 0)
    return SemanticVersion(0, 0, bound.patch + 1)


def version_satisfies(version: str, version_range: str) -> bool:
    candidate = SemanticVersion.parse(version)
    expression = version_range.strip()
    if expression in ("", "*"):
        return True
    for raw_term in expression.split(","):
        match = _TERM_RE.fullmatch(raw_term.strip())
        if match is None:
            raise ValueError(f"unsupported version range term {raw_term!r}")
        operator, raw_bound = match.groups()
        bound = _range_bound(raw_bound)
        operator = operator or "="
        accepted = {
            "=": candidate == bound,
            "==": candidate == bound,
            ">": candidate > bound,
            ">=": candidate >= bound,
            "<": candidate < bound,
            "<=": candidate <= bound,
            "^": candidate >= bound
            and candidate < _caret_upper_bound(raw_bound, bound),
            "~": candidate >= bound
            and candidate < SemanticVersion(bound.major, bound.minor + 1, 0),
        }[operator]
        if not accepted:
            return False
    return True


__all__ = ["SemanticVersion", "version_satisfies"]
