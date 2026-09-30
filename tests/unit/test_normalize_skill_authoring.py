from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

from literate_ai.contracts.authoring_markdown import (
    AGENT_SKILL_EVALUATION_AUTHOR,
    parse_authoring_markdown,
    render_authoring_markdown,
)

_REPOSITORY = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "normalize_skill_authoring",
    _REPOSITORY / "scripts/normalize_skill_authoring.py",
)
assert _SPEC is not None and _SPEC.loader is not None
_NORMALIZER = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _NORMALIZER
_SPEC.loader.exec_module(_NORMALIZER)


class NormalizeSkillAuthoringTests(unittest.TestCase):
    def test_normalizes_nested_skill_manifests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            manifest = (
                repository / "skills/specification-to-source/parent/nested/SKILL.md"
            )
            manifest.parent.mkdir(parents=True)
            manifest.write_bytes(
                render_authoring_markdown(
                    {
                        "schema": (
                            "urn:literate-ai:schema:v1:specification-to-source-skill"
                        ),
                        "skill_id": "nested",
                        "version": "1.0.0",
                        "title": "Nested skill",
                        "stages": ["generate"],
                        "dependencies": [],
                        "limitations": ["Stay nested."],
                        "trust": "repository-reviewed",
                    },
                    "Perform the nested work.",
                )
            )

            changes = _NORMALIZER.normalize_repository(repository)

            self.assertEqual([change.path for change in changes], [manifest.resolve()])
            metadata, body = parse_authoring_markdown(
                manifest.read_bytes(), source=manifest.as_posix()
            )
            self.assertEqual(metadata["name"], "nested")
            self.assertEqual(
                metadata["metadata"]["author"], AGENT_SKILL_EVALUATION_AUTHOR
            )
            self.assertEqual(body, "# Nested skill\n\nPerform the nested work.")
            self.assertEqual(_NORMALIZER.normalize_repository(repository), ())


if __name__ == "__main__":
    unittest.main()
