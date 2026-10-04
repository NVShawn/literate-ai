from __future__ import annotations

"""Shared fixtures extracted from ``tests.unit.test_cli_locked_generation``."""

import json
import shutil
from pathlib import Path

from literate_ai.adapters.component_lock_planning import (
    FilesystemComponentLockPlanner,
)
from literate_ai.adapters.component_locks import ComponentLockStore
from literate_ai.adapters.component_resolution_audits import (
    ComponentResolutionAuditStore,
)
from literate_ai.application.component_lock_resolution import ComponentLockResolver
from literate_ai.contracts.authoring_markdown import (
    parse_authoring_markdown,
    render_authoring_markdown,
)
from tests.support.fixtures_test_component_lock_planning import _fixture

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

_SELECTORS = ("+macos", "+python")

_TARGET = "macos-host"


def _generation_fixture(root: Path) -> tuple[Path, Path]:
    component, flavors = _fixture(root)
    component_manifest = component / "component.md"
    component_manifest.write_text(
        component_manifest.read_text(encoding="utf-8").replace(
            "workflows/host.json", "workflows/host.md"
        ),
        encoding="utf-8",
    )
    (root / "skills" / "implement.json").write_text(
        json.dumps(
            {
                "schema": "urn:literate-ai:schema:v1:specification-to-source-skill",
                "skill_id": "locked-generation-test",
                "version": "1.0.0",
                "title": "Locked generation test",
                "stages": ["plan", "generate"],
                "dependencies": [],
                "instructions": "Generate the exact portable test application.",
                "limitations": ["Use only locked inputs."],
                "trust": "test-reviewed",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    shutil.copy2(
        _REPOSITORY_ROOT / "workflows" / "sample-host.md",
        root / "workflows" / "host.md",
    )
    shutil.copy2(
        _REPOSITORY_ROOT / "routing" / "sample-host.json",
        root / "routing" / "default.json",
    )
    python_manifest = flavors / "lang-python" / "flavor.md"
    python_flavor, body = parse_authoring_markdown(
        python_manifest.read_bytes(), source=python_manifest.as_posix()
    )
    python_flavor["authoring_inputs"] = []
    python_manifest.write_bytes(render_authoring_markdown(python_flavor, body))
    return component, flavors


def _write_lock(component: Path, flavors: Path):
    plan = FilesystemComponentLockPlanner().plan(
        component,
        target_name=_TARGET,
        flavor_selectors=_SELECTORS,
        flavor_roots=(flavors,),
    )
    result = ComponentLockResolver().resolve(
        plan, expected_input_evidence_identity=plan.identity
    )
    ComponentResolutionAuditStore(component, _TARGET).update(result.catalog_audit)
    ComponentLockStore(component).update(result.lock)
    return result.lock


def _add_generation_dependency(component: Path) -> None:
    root_document = component / "component.md"
    root_document.write_text(
        root_document.read_text(encoding="utf-8").replace(
            "requires: []",
            """requires:
  - requirement_id: dependency-contract
    capability: sample.dependency-contract
    version_range: ">=1,<2"
    dependency_kind: generation
    optional: false
    constraints: []""",
        ),
        encoding="utf-8",
    )
    dependency = component / "dependency"
    for relative, content in {
        "component.md": """---
namespace: examples
version: 1.0.0
display_name: Dependency Contract
profiles:
  - portable
sample: false
provides:
  - name: sample.dependency-contract
    version: 1.0.0
    interface:
      uri: interfaces/public.md
requires: []
specification_provider: openspec
specification_roots:
  - specs/private.md
authoring_inputs:
  - kind: specification-to-source-skill
    uri: skills/implement.json
workflow_definition: workflows/host.md
routing_policy: routing/default.json
flavor_slots:
  - slot_id: language
    axis: implementation.language-ecosystem
    cardinality: exactly-one
    capability_contract: sample.portable-app
  - slot_id: os
    axis: platform.os
    cardinality: exactly-one
    capability_contract: sample.portable-app
entrypoints:
  - name: run
    kind: portable-application
    path: run
acceptance_contracts:
  - acceptance/private.json
source_dependencies: []
---
Dependency internals remain outside the consumer generation context.
""",
        "specs/private.md": """# Private dependency implementation

### Requirement: Private behavior

The dependency SHALL keep its implementation private.

#### Scenario: Internal execution

- **WHEN** the dependency runs
- **THEN** it follows its private implementation
""",
        "interfaces/public.md": "# Public dependency contract\n",
        "acceptance/private.json": '{"private":true}\n',
    }.items():
        path = dependency / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
