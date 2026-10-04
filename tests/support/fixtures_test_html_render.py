"""Shared test fixtures extracted from test_html_render."""

import json

from tests.support.fixtures_test_html_emitter import ROOT


def _project(root):
    (root / ".literate").mkdir()
    definition = json.loads((ROOT / "literate.project.json").read_text())
    for field in ("component_roots", "flavor_roots", "workflow_roots", "routing_roots"):
        definition[field] = []
    (root / "literate.project.json").write_text(json.dumps(definition))
    for name in ("repository-parent.json", "repository-lineage.json"):
        (root / ".literate" / name).write_bytes(
            (ROOT / ".literate" / name).read_bytes()
        )
    (root / "SKILL.md").write_text(
        "---\nname: example\ndescription: Test project instructions.\n---\n"
    )
    skill = root / "skills/agent/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: example-agent\ndescription: Nested test.\n---\n")
