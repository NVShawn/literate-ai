"""Deterministic extraction of literal constant tables referenced by inverse evidence.

A closed literal mapping, enumerated constant set, or lookup table with concrete
scalar values is data, not prose-describable behavior. Asking a translator model to
restate it loses precision (see issue #116: 15 of 16 entries in a hand-authored GCP
family/hardware table were silently dropped, and `spec derive` never flagged the gap
because the model's structural Requirements about the table's *shape* were accepted
as full coverage). This module recovers the literal values straight from source via
``ast``, independent of any model call, so they can be pinned into a verbatim asset
instead.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass

_MINIMUM_ENTRIES = 3


def _is_literal_constant(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        return _is_literal_constant(node.operand)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(_is_literal_constant(item) for item in node.elts)
    if isinstance(node, ast.Dict):
        return all(
            key is not None and _is_literal_constant(key) for key in node.keys
        ) and all(_is_literal_constant(value) for value in node.values)
    return False


def _entry_count(node: ast.AST) -> int:
    if isinstance(node, ast.Dict):
        return len(node.keys)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return len(node.elts)
    return 1


@dataclass(frozen=True, slots=True)
class LiteralTable:
    """One closed literal dict/list/tuple/set constant with concrete values."""

    name: str
    value: object


def python_literal_tables(content: str) -> tuple[LiteralTable, ...]:
    """Return module-level literal constants large enough to be a lookup table.

    Single scalar constants (a version string, a timeout) are left as ordinary
    prose-describable behavior; only closed collections with several concrete
    entries are treated as data requiring verbatim extraction.
    """

    try:
        tree = ast.parse(content)
    except SyntaxError:
        return ()
    tables: list[LiteralTable] = []
    seen: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = node.targets
            value_node = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
            value_node = node.value
        else:
            continue
        if not isinstance(value_node, (ast.Dict, ast.List, ast.Tuple, ast.Set)):
            continue
        if (
            not _is_literal_constant(value_node)
            or _entry_count(value_node) < _MINIMUM_ENTRIES
        ):
            continue
        for target in targets:
            if not isinstance(target, ast.Name) or target.id in seen:
                continue
            try:
                value = ast.literal_eval(value_node)
            except (ValueError, TypeError):
                continue
            seen.add(target.id)
            tables.append(LiteralTable(name=target.id, value=value))
    return tuple(tables)


def literal_table_symbol_suffixes(*, language: str, content: str) -> tuple[str, ...]:
    """Stable surface-identity suffixes only; carries no extracted values.

    Used to register literal-data as its own required behavioral surface so
    `spec derive` coverage cannot silently classify it as ordinary covered
    behavior; the exact values are recovered separately by
    :func:`python_literal_tables` and pinned into an asset.
    """

    if language != "python":
        return ()
    return tuple(
        f"literal-data:{table.name}" for table in python_literal_tables(content)
    )


def canonical_literal_table_json(value: object) -> str:
    """Render a literal table as stable, verbatim JSON asset content."""

    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def literal_asset_path(*, source_path: str, name: str) -> str:
    """Deterministic component-relative path for a literal table's pinned asset."""

    stem = re.sub(
        r"[^a-zA-Z0-9_-]+", "-", source_path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    ).strip("-")
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", name).strip("-")
    return f"assets/{stem or 'source'}-{slug or 'table'}.json"


__all__ = [
    "LiteralTable",
    "canonical_literal_table_json",
    "literal_asset_path",
    "literal_table_symbol_suffixes",
    "python_literal_tables",
]
