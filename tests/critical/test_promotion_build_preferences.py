"""Parent build preference custody without fabricating qualified source state."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from literate_ai.cli.errors import CliFailure
from literate_ai.cli.source_to_specification import _inherited_promotion_build
from literate_ai.projects import (
    LoadedProject,
    PinnedInputClosureError,
    ProjectConfigurationStore,
)
from tests.support.fixtures_test_retained_harness_receipts import (
    _adapter,
    _legacy_project,
    _selectors,
)


class PromotionBuildPreferencesTests(unittest.TestCase):
    def project(self, root):
        path = root / "adopted"
        _legacy_project(path)
        _adapter().initialize(
            path,
            flavor_selectors=_selectors(path),
            source_intelligence_provider="none",
            convert=True,
        )
        snapshot = ProjectConfigurationStore(path).read()
        return LoadedProject(path, snapshot.definition)

    def test_no_parent_has_no_inherited_build(self):
        self.assertEqual(_inherited_promotion_build(None), (None, None))

    def test_exact_parent_selection_and_manifest_bytes_are_guarded(self):
        with tempfile.TemporaryDirectory() as directory:
            project = self.project(Path(directory))
            selected, guard = _inherited_promotion_build(project)
            self.assertEqual(
                selected.selection.coordinate_uri, "flavor://literate-ai/build-make"
            )
            guard.require_unchanged()
            selected.path.write_bytes(
                selected.path.read_bytes() + b"\nchanged parent authoring\n"
            )
            with self.assertRaises(PinnedInputClosureError):
                guard.require_unchanged()

    def test_changed_parent_configuration_refuses_before_inheritance(self):
        with tempfile.TemporaryDirectory() as directory:
            project = self.project(Path(directory))
            store = ProjectConfigurationStore(project.root)
            snapshot = store.read()
            store.update(snapshot, replace(snapshot.definition, log_dir="other-logs"))
            with self.assertRaises(CliFailure) as changed:
                _inherited_promotion_build(project)
            self.assertEqual(changed.exception.code, "promotion.parent_changed")

    def test_component_specific_build_preferences_require_explicit_choice(self):
        with tempfile.TemporaryDirectory() as directory:
            project = self.project(Path(directory))
            store = ProjectConfigurationStore(project.root)
            snapshot = store.read()
            definition = replace(
                snapshot.definition,
                component_flavor_selectors={
                    "components/legacy-project-wrapper": ("+make",)
                },
            )
            store.update(snapshot, definition)
            with self.assertRaises(CliFailure) as scoped:
                _inherited_promotion_build(LoadedProject(project.root, definition))
            self.assertEqual(
                scoped.exception.code, "promotion.qualification_flavor_required"
            )
