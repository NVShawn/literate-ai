from __future__ import annotations
"""Shared fixtures extracted from ``tests.unit.test_executable_component_v2_schemas``."""

import json


from pathlib import Path

from jsonschema import Draft202012Validator

from referencing import Registry, Resource











ROOT = Path(__file__).resolve().parents[2]

V2_ROOT = ROOT / "schemas" / "v2"

def _official_validator(resource_id: str) -> Draft202012Validator:
    registry = Registry()
    for directory in (ROOT / "schemas" / "v1", V2_ROOT):
        for path in sorted(directory.glob("*.schema.json")):
            document = json.loads(path.read_text(encoding="utf-8"))
            registry = registry.with_resource(
                document["$id"], Resource.from_contents(document)
            )
    return Draft202012Validator({"$ref": resource_id}, registry=registry.crawl())

