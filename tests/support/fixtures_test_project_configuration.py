from __future__ import annotations
"""Shared fixtures extracted from ``tests.unit.test_project_configuration``."""

import json







from pathlib import Path



from literate_ai.contracts import (
    ProjectDefinition,
    RepositoryPolicy,
)

from literate_ai.projects import (
    PROJECT_FILENAME,
)

ROOT = Path(__file__).resolve().parents[2]

def _definition() -> ProjectDefinition:
    value = json.loads((ROOT / PROJECT_FILENAME).read_bytes())
    value["project_id"] = "configuration-test"
    value["repository_policy"] = RepositoryPolicy().to_dict()
    return ProjectDefinition.from_dict(value)

