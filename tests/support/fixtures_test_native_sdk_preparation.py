"""Shared fixtures extracted from ``tests.unit.test_native_sdk_preparation``."""

import json

from literate_ai.contracts.authoring_markdown import (
    parse_authoring_markdown,
    render_authoring_markdown,
)
from tests.support.fixtures_test_native_sdk_closure import linked_recipe


def preparation_recipe(fixture):
    linked_recipe(fixture)
    path = fixture.component / "component.md"
    document, body = parse_authoring_markdown(path.read_bytes(), source=str(path))
    document["requires"].append(
        {
            **document["requires"][0],
            "requirement_id": "provider-api",
            "dependency_kind": "generation",
        }
    )
    path.write_bytes(render_authoring_markdown(document, body))
    for component in (fixture.component, fixture.component / "provider"):
        path = component / "component.md"
        document, body = parse_authoring_markdown(path.read_bytes(), source=str(path))
        if component != fixture.component:
            document["kind"] = "library"
        document["authoring_inputs"].extend(
            {
                "kind": "specification-to-source-skill",
                "uri": f"skills/specification-to-source/{name}/SKILL.md",
            }
            for name in (
                "portable-specification-planning",
                "portable-application-implementation",
            )
        )
        path.write_bytes(render_authoring_markdown(document, body))
    (fixture.component.parent / "skills/implement.json").write_text(
        json.dumps(
            {
                "schema": "urn:literate-ai:schema:v1:specification-to-source-skill",
                "skill_id": "fixture-implementation",
                "version": "1.0.0",
                "title": "Fixture implementation",
                "stages": ["generate"],
                "dependencies": [],
                "instructions": "Implement the declared public contracts.",
                "limitations": ["Do not invent behavior."],
                "trust": "fixture-reviewed",
            }
        )
    )
