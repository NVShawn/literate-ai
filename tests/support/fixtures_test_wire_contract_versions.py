"""Shared test fixtures extracted from test_wire_contract_versions."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support.fixtures_test_schema_catalog import SchemaCatalog

REPOSITORY = Path(__file__).resolve().parents[2]

V1_SCHEMAS = REPOSITORY / "schemas" / "v1"

V2_SCHEMAS = REPOSITORY / "schemas" / "v2"


def _v2_schemas() -> SchemaCatalog:
    schemas = SchemaCatalog(V2_SCHEMAS)
    for path in sorted(V1_SCHEMAS.glob("*.schema.json")):
        schemas._collect(json.loads(path.read_text(encoding="utf-8")))
    return schemas
