from __future__ import annotations

"""Shared fixtures extracted from ``tests.unit.test_component_lock_planning``."""


import shutil
from pathlib import Path

from literate_ai.contracts.authoring_markdown import (
    parse_authoring_markdown,
    render_authoring_markdown,
)

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _component_document() -> str:
    return """---
namespace: examples
version: 1.0.0
display_name: Portable Greeting
profiles:
  - application
  - portable
sample: true
provides:
  - name: sample.portable-app
    version: 1.0.0
requires: []
specification_provider: openspec
specification_roots:
  - specs/spec.md
authoring_inputs:
  - kind: specification-to-source-skill
    uri: skills/implement.json
workflow_definition: workflows/host.json
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
  - acceptance/execution.json
source_dependencies: []
---
A small portable greeting application with deterministic behavior.
"""


def _fixture(root: Path) -> tuple[Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text(
        "---\n"
        "name: fixture-project\n"
        "description: Component-lock planning fixture.\n"
        "---\n"
        "# Fixture project\n",
        encoding="utf-8",
        newline="\n",
    )
    component = root / "greeting"
    for relative, content in {
        "component.md": _component_document(),
        "specs/spec.md": (
            "# Greeting\n\n### Requirement: Deterministic greeting\n\n"
            "The application SHALL print a stable greeting.\n\n"
            "#### Scenario: Run greeting\n\n- **WHEN** the application runs\n"
            "- **THEN** it prints `hello`\n"
        ),
        "acceptance/execution.json": '{"stdout":"hello\\n"}\n',
    }.items():
        path = component / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
    for relative, content in {
        "skills/implement.json": '{"skill":"portable"}\n',
        "workflows/host.json": '{"workflow":"host"}\n',
        "routing/default.json": '{"routing":"default"}\n',
    }.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
    flavors = root / "flavors"
    for flavor in ("os-macos", "lang-python"):
        shutil.copytree(_REPOSITORY_ROOT / "flavors" / flavor, flavors / flavor)
    for skill in (
        "portable-specification-planning",
        "portable-application-implementation",
        "python-portable-application",
    ):
        shutil.copytree(
            _REPOSITORY_ROOT / "skills" / "specification-to-source" / skill,
            root / "skills" / "specification-to-source" / skill,
        )
    return component, flavors


def _flavor_variant(
    source: Path,
    destination: Path,
    *,
    name: str,
    value: str,
    conflicts: tuple[str, ...] = (),
) -> None:
    shutil.copytree(source, destination)
    manifest = destination / "flavor.md"
    document, body = parse_authoring_markdown(
        manifest.read_bytes(), source=manifest.as_posix()
    )
    document["name"] = name
    document["display_name"] = name
    document["target"] = value
    document["conflicts"] = list(conflicts)
    manifest.write_bytes(render_authoring_markdown(document, body))
