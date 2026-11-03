"""Project Flavor names never authorize project-authored host probes."""

import hashlib
import json
import os
import shutil
import tempfile
import unittest
from dataclasses import replace
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters.harness_inventory import legacy_shim_authority
from literate_ai.adapters.operator_status import (
    inspect_host_preflight,
    inspect_operator_status,
)
from literate_ai.adapters.project_host_flavors import project_host_flavor_names
from literate_ai.bootstrap.host_install_requirements import (
    HostInstallTarget,
    parse_flavor_toolchain_sbom,
)
from literate_ai.projects import ProjectConfigurationStore, load_project
from tests.smoke.test_project_reparenting import initialize_root


class ProjectHostFlavorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / "project"
        initialize_root(self.root)
        for relative, content in legacy_shim_authority(
            {"commands": {}}, {"phases": []}
        ).items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        self.project = load_project(self.root)
        self.packaged = Path(str(files("literate_ai.project_template") / "flavors"))
        self.shim = "+flavor://legacy-adoption/build-legacy-shim"

    def snapshot(self):
        return {
            path.relative_to(self.root).as_posix(): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in self.root.rglob("*")
            if path.is_file() and not path.is_symlink()
        }

    def preflight(self, selectors, *, public_status=False):
        observations = []

        def observe(**kwargs):
            observations.append(kwargs["sbom"])
            return SimpleNamespace(to_dict=lambda: {"ready": True})

        with (
            patch(
                "literate_ai.adapters.operator_status.detect_current_install_target",
                return_value=HostInstallTarget("macos", "arm64"),
            ),
            patch(
                "literate_ai.adapters.operator_status.observe_host_install_dependencies",
                side_effect=observe,
            ),
            patch(
                "literate_ai.adapters.operator_status.inspect_coding_cli_authentication",
                return_value={"state": "unavailable"},
            ),
        ):
            report = (
                inspect_operator_status(self.root)["host"]
                if public_status
                else inspect_host_preflight(
                    project=self.project, flavor_selectors=selectors
                )
            )
        return report, observations

    def test_rendered_adoption_shim_resolves_coordinate_without_host_requirements(self):
        store = ProjectConfigurationStore(self.root)
        original = store.read()
        store.update(
            original,
            replace(original.definition, default_flavor_selectors=(self.shim,)),
        )
        before = self.snapshot()
        report, observations = self.preflight((self.shim,), public_status=True)
        self.assertTrue(report["host_tools"]["ready"], report)
        self.assertEqual(len(observations), 1)
        self.assertEqual(
            project_host_flavor_names(self.project, (self.shim,), self.packaged), ()
        )
        self.assertEqual(self.snapshot(), before)

    def test_copied_framework_flavor_retains_tools_even_if_sidecar_is_removed(self):
        selectors = ("+flavor://literate-ai/lang-python", self.shim)
        expected = project_host_flavor_names(self.project, selectors, self.packaged)
        self.assertIn("lang-python", expected)
        sidecar = self.root / "flavors/lang-python/host-toolchain.cdx.json"
        sidecar.unlink()
        self.assertEqual(
            project_host_flavor_names(self.project, selectors, self.packaged), expected
        )
        report, observations = self.preflight(selectors)
        self.assertTrue(report["host_tools"]["ready"], report)
        self.assertIn("python", {item.capability for item in observations[0].packages})

    def test_custom_probe_refuses_before_any_host_observation_without_writes(self):
        sidecar = self.root / "flavors/legacy-project-shim/host-toolchain.cdx.json"
        sentinel = self.root.parent / "probe-executed"
        document = json.loads(
            (self.packaged / "os-freebsd/host-toolchain.cdx.json").read_text()
        )
        code = "exit(99)"
        for item in document["components"][0]["properties"]:
            if item["name"] == "literate-ai:base-toolchain:probe-arguments":
                item["value"] = json.dumps(["-c", code])
        parse_flavor_toolchain_sbom(document)
        sidecar.write_text(json.dumps(document))
        before = self.snapshot()
        report, observations = self.preflight((self.shim,))
        self.assertFalse(report["host_tools"]["ready"])
        self.assertEqual(
            report["host_tools"]["error"]["code"],
            "host-install.project-toolchain-observation-unsupported",
        )
        self.assertEqual(observations, [])
        self.assertFalse(sentinel.exists())
        self.assertEqual(self.snapshot(), before)

    def test_ordered_selection_and_missing_flavor_refusal(self):
        selectors = (self.shim, "-flavor://legacy-adoption/build-legacy-shim")
        self.assertEqual(
            project_host_flavor_names(self.project, selectors, self.packaged), ()
        )
        report, observations = self.preflight(("+flavor://missing/unknown",))
        self.assertFalse(report["host_tools"]["ready"])
        self.assertEqual(observations, [])
        self.assertEqual(
            report["host_tools"]["error"]["code"], "flavor_selection.unknown_flavor"
        )

    def test_duplicate_canonical_flavor_refuses_before_host_observation(self):
        shutil.copytree(
            self.root / "flavors/legacy-project-shim",
            self.root / "flavors/duplicate-shim",
        )
        report, observations = self.preflight((self.shim,))
        self.assertFalse(report["host_tools"]["ready"])
        self.assertEqual(observations, [])
        self.assertEqual(
            report["host_tools"]["error"]["code"], "component_lock.flavor_duplicate"
        )

    @unittest.skipIf(os.name == "nt", "POSIX symlink counterexample")
    def test_symbolic_toolchain_refuses_without_reading_or_executing_target(self):
        target = self.root.parent / "untrusted-probe.json"
        target.write_text('{"probe": ["python", "-c", "raise SystemExit(99)"]}')
        sidecar = self.root / "flavors/legacy-project-shim/host-toolchain.cdx.json"
        sidecar.symlink_to(target)
        before = target.read_bytes()
        report, observations = self.preflight((self.shim,))
        self.assertFalse(report["host_tools"]["ready"])
        self.assertEqual(observations, [])
        self.assertEqual(
            report["host_tools"]["error"]["code"], "component_lock.flavor_unsafe"
        )
        self.assertEqual(target.read_bytes(), before)

    def test_oversized_sidecar_refuses_before_decoding_or_observation(self):
        sidecar = self.root / "flavors/legacy-project-shim/host-toolchain.cdx.json"
        with sidecar.open("wb") as output:
            output.truncate(2 * 1024**2 + 1)
        report, observations = self.preflight((self.shim,))
        self.assertFalse(report["host_tools"]["ready"])
        self.assertEqual(observations, [])
        self.assertEqual(
            report["host_tools"]["error"]["code"],
            "component_lock.flavor_toolchain_unavailable",
        )
