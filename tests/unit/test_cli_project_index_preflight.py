"""Build and run must stop before effects when the project index is unavailable."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from literate_ai.cli import build_run, rebuild
from literate_ai.cli.errors import CliFailure
from literate_ai.project_source_index import ProjectSourceIntelligenceError


def _project(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        root=root,
        definition=SimpleNamespace(
            source_intelligence=SimpleNamespace(provider_id="none"),
            lifecycle_driver=Mock(),
        ),
    )


def _unavailable() -> ProjectSourceIntelligenceError:
    return ProjectSourceIntelligenceError(
        "project.source_intelligence_missing",
        "source-intelligence is unavailable",
    )


class CliProjectIndexPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.project_root = Path(temporary_directory.name)

    def test_rebuild_rejects_before_driver_resolution_or_runtime_allocation(
        self,
    ) -> None:
        project = _project(self.project_root)
        args = Namespace(allow_host_execution=True, project=".")
        with (
            patch.object(rebuild, "_project", return_value=project),
            patch.object(
                rebuild, "require_lifecycle_project_index", side_effect=_unavailable()
            ),
            patch.object(rebuild, "_resolve_standard_binding") as resolve_binding,
            patch.object(rebuild, "_external_runtime_root") as runtime_root,
        ):
            with self.assertRaises(CliFailure) as caught:
                rebuild.rebuild_from_args(args)

        self.assertEqual(caught.exception.code, "project.source_intelligence_missing")
        resolve_binding.assert_not_called()
        runtime_root.assert_not_called()

    def test_run_rejects_before_reading_or_executing_an_export(self) -> None:
        project = _project(self.project_root)
        args = Namespace(project=".", component=None, arguments=[])
        with (
            patch.object(build_run, "_project_root", return_value=project.root),
            patch.object(build_run, "discover_project", return_value=project),
            patch.object(
                build_run,
                "require_lifecycle_project_index",
                side_effect=_unavailable(),
            ),
            patch.object(build_run, "load_artifact_export") as load_export,
            patch.object(subprocess, "run") as execute,
        ):
            with self.assertRaises(CliFailure) as caught:
                build_run.run_from_args(args)

        self.assertEqual(caught.exception.code, "project.source_intelligence_missing")
        load_export.assert_not_called()
        execute.assert_not_called()

    def test_rebuild_carries_the_exact_project_index_observation(self) -> None:
        project = _project(self.project_root)
        observation = {
            "schema": "literate-ai/project-source-intelligence-status@1",
            "state": "current",
            "database_identity": "sha256:" + "1" * 64,
        }
        args = Namespace(allow_host_execution=True, project=".")
        driver = Mock(spec=rebuild.StandardProjectLifecycleDriver)
        project.definition.lifecycle_driver = driver
        rebuilt = {"schema": "literate-ai/project-rebuild-result@3", "passed": True}
        with (
            patch.object(rebuild, "_project", return_value=project),
            patch.object(
                rebuild,
                "require_lifecycle_project_index",
                return_value=observation,
            ),
            patch.object(rebuild, "_resolve_standard_binding", return_value=Mock()),
            patch.object(rebuild, "_standard_rebuild_from_args", return_value=rebuilt),
        ):
            result = rebuild.rebuild_from_args(args)

        self.assertEqual(result["project_source_intelligence"], observation)


if __name__ == "__main__":
    unittest.main()
