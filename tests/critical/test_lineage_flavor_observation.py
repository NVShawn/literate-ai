"""Operation-local graph metadata preserves fresh authority and exact inputs."""

import os
import pickle
import shutil
import unittest
from unittest.mock import Mock, patch

import literate_ai.adapters.legacy_generation_catalog as catalog
import literate_ai.project_authority_graph as graph
from tests.critical import test_current_component_locks as fixtures
from tests.support.fixtures_test_cli_component_locks import _run


class LineageFlavorObservationTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.CurrentComponentLockTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.root = fixture.root
        self.component = fixture.component
        self.roots = (self.root / "flavors",)

    def capture(self):
        return catalog.LegacyFlavorGraphObservation.capture(self.roots)

    def test_raw_and_transported_success_claims_are_not_observations(self):
        with self.assertRaises(TypeError):
            catalog.LegacyFlavorGraphObservation()
        with self.assertRaises(TypeError):
            pickle.dumps(self.capture())
        claimed = Mock()
        with self.assertRaises(TypeError):
            graph.project_authority_graph(self.root, flavor_observation=claimed)
        claimed.require_current.assert_not_called()

    def test_metadata_reuse_keeps_full_fresh_graphs_and_skill_validation(self):
        expected = graph.project_authority_graph(
            self.root, include_component_locks=False
        )
        with patch.object(
            catalog,
            "_load_legacy_flavor_entries",
            wraps=catalog._load_legacy_flavor_entries,
        ) as load:
            observed = self.capture()
            with patch.object(
                graph, "project_skill_catalog", wraps=graph.project_skill_catalog
            ) as skills:
                for _ in range(2):
                    actual = graph.project_authority_graph(
                        self.root,
                        include_component_locks=False,
                        flavor_observation=observed,
                    )
                    self.assertEqual(actual, expected)
                self.assertEqual(skills.call_count, 2)
            self.assertEqual(load.call_count, 1)
        self.assertTrue(
            all(
                isinstance(item, catalog.FlavorGraphMetadata)
                for item in observed.require_current(self.roots)
            )
        )

    def test_same_size_same_mtime_manifest_change_refuses(self):
        observed = self.capture()
        path = self.roots[0] / "os-macos/flavor.md"
        original = path.read_bytes()
        token = b"macOS"
        self.assertIn(token, original)
        info = path.stat()
        path.write_bytes(original.replace(token, b"macOs", 1))
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        with self.assertRaises(ValueError):
            observed.require_current(self.roots)

    def test_new_and_missing_members_refuse(self):
        for change in ("add", "missing"):
            with self.subTest(change=change):
                observed = self.capture()
                path = self.roots[0] / "os-macos/flavor.md"
                original = path.read_bytes()
                extra = self.roots[0] / "additional"
                try:
                    if change == "add":
                        shutil.copytree(path.parent, extra)
                    else:
                        path.unlink()
                    with self.assertRaises((ValueError, RuntimeError)):
                        observed.require_current(self.roots)
                finally:
                    path.write_bytes(original)
                    if extra.exists():
                        shutil.rmtree(extra)

    @unittest.skipIf(
        os.name == "nt", "POSIX symlink; native Windows reparse checks are separate"
    )
    def test_redirected_member_refuses(self):
        observed = self.capture()
        path = self.roots[0] / "os-macos/flavor.md"
        backup = path.with_suffix(".saved")
        path.rename(backup)
        try:
            path.symlink_to(backup)
            with self.assertRaises(ValueError):
                observed.require_current(self.roots)
        finally:
            path.unlink(missing_ok=True)
            backup.rename(path)

    def test_nearer_project_creation_and_changed_project_bytes_refuse(self):
        observed = self.capture()
        nearer = self.roots[0] / "os-macos/literate.project.json"
        nearer.write_bytes((self.root / "literate.project.json").read_bytes())
        with self.assertRaises(RuntimeError):
            observed.require_current(self.roots)
        nearer.unlink()
        project = self.root / "literate.project.json"
        project.write_bytes(project.read_bytes() + b"\n")
        with self.assertRaises(RuntimeError):
            observed.require_current(self.roots)

    def test_wrong_catalog_roots_refuse(self):
        with self.assertRaises(RuntimeError):
            self.capture().require_current(())

    def test_changed_specification_and_contribution_bytes_refuse(self):
        for relative in ("lang-python/openspec/spec.md", "lang-python/toolchain.json"):
            with self.subTest(relative=relative):
                observed = self.capture()
                path = self.roots[0] / relative
                original = path.read_bytes()
                try:
                    path.write_bytes(original + b"\n")
                    with self.assertRaises(ValueError):
                        observed.require_current(self.roots)
                finally:
                    path.write_bytes(original)

    def test_public_check_refuses_drift_without_replacing_lock_or_audit(self):
        common = [
            "lock",
            str(self.component),
            "--target",
            "host",
            "--flavor",
            "+python",
            "--flavor",
            "+macos",
            "--flavor-root",
            str(self.roots[0]),
            "--json",
            "--check",
        ]
        before = {p: p.read_bytes() for p in self.component.glob("component*.json")}
        capture = catalog.LegacyFlavorGraphObservation.capture

        def changed(roots):
            observed = capture(roots)
            path = self.roots[0] / "os-macos/flavor.md"
            path.write_bytes(path.read_bytes() + b"\n")
            return observed

        with patch.object(
            catalog.LegacyFlavorGraphObservation, "capture", side_effect=changed
        ):
            status, report, error = _run(common)
            self.assertEqual(status, 2, (report, error))
            self.assertEqual(report["error"]["code"], "inputs.closure_changed")
        self.assertEqual({p: p.read_bytes() for p in before}, before)

    def test_unrelated_skill_invalidity_still_fails_fresh_graph_validation(self):
        observed = self.capture()
        (self.root / "SKILL.md").write_bytes(b"---\nname: [invalid\n---\n")
        with self.assertRaises(ValueError):
            graph.project_authority_graph(
                self.root, include_component_locks=False, flavor_observation=observed
            )

    def test_new_component_self_cycle_still_fails_fresh_graph_validation(self):
        observed = self.capture()
        path = self.component / "component.md"
        content = path.read_text()
        self.assertIn("uri: skills/implement.json", content)
        path.write_text(
            content.replace("uri: skills/implement.json", "uri: greeting/component.md")
        )
        with self.assertRaises(graph.AuthorityGraphError) as refusal:
            graph.project_authority_graph(
                self.root, include_component_locks=False, flavor_observation=observed
            )
        self.assertEqual(refusal.exception.code, "graph.cycle")

    def test_drift_during_graph_construction_refuses_after_graph_validation(self):
        observed = self.capture()
        original = graph._component_authoring
        path = self.roots[0] / "os-macos/flavor.md"

        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            path.write_bytes(path.read_bytes() + b"\n")
            return result

        with patch.object(graph, "_component_authoring", side_effect=changed):
            with self.assertRaises(ValueError):
                graph.project_authority_graph(
                    self.root,
                    include_component_locks=False,
                    flavor_observation=observed,
                )

    def test_check_owns_one_observation_and_writers_remain_fresh(self):
        common = [
            "lock",
            str(self.component),
            "--target",
            "host",
            "--flavor",
            "+python",
            "--flavor",
            "+macos",
            "--flavor-root",
            str(self.roots[0]),
            "--json",
        ]
        artifacts = tuple(self.component.glob("component*.json"))
        before = {p: p.read_bytes() for p in artifacts}
        with (
            patch.object(
                catalog.LegacyFlavorGraphObservation,
                "capture",
                wraps=catalog.LegacyFlavorGraphObservation.capture,
            ) as capture,
            patch.object(
                graph, "project_authority_graph", wraps=graph.project_authority_graph
            ) as build,
        ):
            for _ in range(2):
                status, report, error = _run([*common, "--check"])
                self.assertEqual(status, 0, (report, error))
            self.assertEqual(capture.call_count, 2)
            self.assertEqual(build.call_count, 8)
        self.assertEqual({p: p.read_bytes() for p in artifacts}, before)
        with patch.object(
            catalog.LegacyFlavorGraphObservation,
            "capture",
            side_effect=AssertionError("writer reused graph metadata"),
        ):
            status, report, error = _run(common)
            self.assertEqual(status, 0, (report, error))
