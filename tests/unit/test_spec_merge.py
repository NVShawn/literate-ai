"""Staged reverse-adoption does not re-quarantine an already-managed project."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.spec_merge import (
    SpecMergeError,
    plan_spec_merge,
)
from literate_ai.contracts import ProjectInitializationOrigin
from tests.unit.root_parent_adapter import (
    RootParentProjectInitializationAdapter as FilesystemProjectInitializationAdapter,
)


def _origin() -> ProjectInitializationOrigin:
    return ProjectInitializationOrigin(
        "ssh://git.example.test/operator/literate-ai.git",
        "a" * 40,
        "literate-ai",
        "0.2.0",
    )


class SpecMergeTests(unittest.TestCase):
    def test_refuses_to_overwrite_an_existing_component(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "managed"
            FilesystemProjectInitializationAdapter(
                initialization_origin_provider=_origin,
                standard_binding_provider=lambda: None,
            ).initialize(
                target,
                flavor_selectors=("+python", "+macos"),
                source_intelligence_provider="none",
                empty=True,
            )
            island = target / "app"
            island.mkdir()
            (island / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
            (target / "components" / "hardware-lookup").mkdir(parents=True)
            (target / "components" / "hardware-lookup" / "component.md").write_text(
                "# existing\n", encoding="utf-8"
            )
            with self.assertRaises(SpecMergeError) as raised:
                plan_spec_merge(target, island, component="hardware-lookup")
            self.assertEqual(
                raised.exception.code, "project.spec_merge_component_exists"
            )


if __name__ == "__main__":
    unittest.main()
