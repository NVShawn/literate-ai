#!/usr/bin/env python3
"""Convert legacy flavor.json intent into canonical human-authored flavor.md."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from literate_ai.adapters.flavor_markdown import FLAVOR_MARKDOWN_SCHEMA
from literate_ai.contracts import FlavorDefinition
from literate_ai.contracts.authoring_markdown import render_authoring_markdown


def migrate(path: Path) -> Path:
    if path.name != "flavor.json" or not path.is_file() or path.is_symlink():
        raise ValueError(f"expected a direct regular flavor.json: {path}")
    target = path.with_name("flavor.md")
    if target.exists():
        raise ValueError(f"refusing ambiguous dual authority: {target}")
    definition = FlavorDefinition.from_dict(json.loads(path.read_bytes()))
    frontmatter = {
        "schema": FLAVOR_MARKDOWN_SCHEMA,
        "namespace": definition.coordinate.namespace,
        "name": definition.coordinate.name,
        "version": definition.version,
        "display_name": definition.display_name,
        "primary_axis": definition.primary_axis.value,
        "target": definition.supported_targets[0],
        "secondary_constraints": [
            item.to_dict() for item in definition.secondary_constraints
        ],
        "applicable_capabilities": list(definition.applicable_capabilities),
        "provides": [
            {
                "name": item.name,
                "version": item.version,
                "contract": None if item.contract is None else item.contract.to_dict(),
            }
            for item in definition.provides
        ],
        "requires": [
            {key: value for key, value in item.to_dict().items() if key != "schema"}
            for item in definition.requires
        ],
        "specification_roots": [
            item.uri for item in definition.specification_fragments
        ],
        "authoring_inputs": [
            {"kind": item.kind, "uri": item.uri} for item in definition.authoring_inputs
        ],
        "contributions": [
            {
                "contribution_id": item.contribution_id,
                "kind": item.kind.value,
                "merge_operator": item.merge_operator.value,
                "slot": item.slot,
                "content": {"kind": item.content.kind, "uri": item.content.uri},
            }
            for item in definition.contributions
        ],
        "conflicts": list(definition.conflicts),
        "co_requisites": list(definition.co_requisites),
        "order_before": list(definition.order_before),
        "order_after": list(definition.order_after),
    }
    body = (
        f"# {definition.display_name}\n\n"
        f"Select this Flavor when the `{definition.primary_axis.value}` axis should "
        f"resolve to `{definition.supported_targets[0]}`. The referenced specification "
        "contains the exact generation policy contributed by this choice."
    )
    target.write_bytes(render_authoring_markdown(frontmatter, body))
    path.unlink()
    return target


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+", type=Path)
    arguments = parser.parse_args()
    for path in arguments.paths:
        print(migrate(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
