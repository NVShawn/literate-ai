"""Real lock-state transitions with synthetic wheel discovery; not wheel proof."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import chdir
from pathlib import Path
from unittest import mock

from literate_ai.adapters import html_render, html_staleness
from literate_ai.adapters.html_framework import HtmlFrameworkObservation
from literate_ai.cli.dispatch import main
from literate_ai.projects import ProjectConfigurationStore
from scripts import installed_html_smoke
from tests.unit.test_component_lock_planning import _fixture
from tests.unit.test_html_emitter import DISTRIBUTION
from tests.unit.test_html_render import _project


class InstalledLockHealthSmokeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        _project(self.root)
        component, _ = _fixture(self.root)
        self.component = self.root / "samples/hello-component"
        self.component.parent.mkdir()
        component.rename(self.component)
        self.configuration = self.root / "literate.project.json"
        definition = json.loads(self.configuration.read_text())
        definition.update(
            component_roots=["samples/hello-component"],
            flavor_roots=["flavors"],
            component_flavor_selectors={
                "samples/hello-component": ["+macos", "+python"]
            },
        )
        self.configuration.write_text(json.dumps(definition))
        store = ProjectConfigurationStore(self.root)
        snapshot = store.read()
        store.update(snapshot, snapshot.definition)
        self.original = self.configuration.read_bytes()

    def qualify(self, *, corrupt_error=False):
        def command(_litai, project, environment, *args, expected_exit=0):
            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                chdir(project),
                mock.patch.dict(
                    os.environ,
                    {**environment, "LITAI_CONFIG_DIR": str(self.root / "operator")},
                ),
            ):
                status = main(args, stdout=stdout, stderr=stderr)
            self.assertEqual(
                status, expected_exit, stdout.getvalue() + stderr.getvalue()
            )
            payload = json.loads(stdout.getvalue())
            self.assertTrue(payload["ok"])
            result = payload["result"]
            if corrupt_error and args[:2] == ("render", "html"):
                path = result["artifact"]["artifact_path"]
                if path == "locks-error.html":
                    target = project / path
                    target.write_bytes(target.read_bytes() + b"modified")
            return result

        framework = HtmlFrameworkObservation(DISTRIBUTION, "1.1.0")
        with (
            mock.patch.object(installed_html_smoke, "run_command", side_effect=command),
            mock.patch.object(
                html_render, "observe_html_framework", return_value=framework
            ),
            mock.patch.object(
                html_staleness, "observe_html_framework", return_value=framework
            ),
        ):
            return installed_html_smoke.qualify_lock_health(
                Path("unused-litai"), self.root, DISTRIBUTION.uri
            )

    def assert_restored(self):
        self.assertEqual(self.configuration.read_bytes(), self.original)
        self.assertFalse((self.component / "component.lock.json").exists())
        self.assertFalse(
            (self.component / "component.resolution-audit.host.json").exists()
        )

    def test_actual_states_retain_distinct_bound_html_and_restore_fixture(self):
        result = self.qualify()
        self.assertEqual(
            [(state["state"], state["gate"]) for state in result["states"]],
            [("not-current", "fail"), ("error", "fail"), ("empty", "skipped")],
        )
        for state in result["states"]:
            self.assertTrue((self.root / state["artifact"]["artifact_path"]).is_file())
        self.assertEqual(
            len({state["report_identity"] for state in result["states"]}), 3
        )
        self.assert_restored()

    def test_corrupted_error_html_refuses_and_still_restores_fixture(self):
        with self.assertRaisesRegex(RuntimeError, "differs from its public report"):
            self.qualify(corrupt_error=True)
        self.assert_restored()
