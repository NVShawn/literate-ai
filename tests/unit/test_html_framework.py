"""Installation binding with explicit synthetic wheel fixtures, not release proof."""

import json
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from unittest import mock

import literate_ai
from literate_ai.adapters import html_framework
from literate_ai.adapters.standard_lifecycle_binding import (
    observe_installed_framework_distribution,
)
from literate_ai.contracts.html_observability import HtmlRenderRefusal


class _FixtureDistribution:
    def __init__(self, root):
        self.root = root
        self.metadata = {"Name": "literate-ai"}
        self.version = "1.1.0"
        self.direct_url = None
        payload = {
            "literate_ai/__init__.py": b'"""Synthetic test package."""\n',
            "literate_ai/standard_policies/standard-lifecycle-v1.json": b"{}\n",
            "share/literate-ai/schemas/v1/index.json": b'{"fixture":1}\n',
            "share/literate-ai/schemas/v2/index.json": b'{"fixture":2}\n',
        }
        self.files = tuple(PurePosixPath(name) for name in payload)
        for name, content in payload.items():
            path = self.locate_file(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

    def locate_file(self, name):
        return self.root / str(name)

    def read_text(self, name):
        return self.direct_url if name == "direct_url.json" else None


class HtmlFrameworkTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.distribution = _FixtureDistribution(self.root / "wheel")
        self.init = self.distribution.locate_file("literate_ai/__init__.py")
        self.catalog = self.distribution.root / "share/literate-ai/schemas"
        patches = (
            mock.patch.object(literate_ai, "__file__", str(self.init)),
            mock.patch.object(
                html_framework.importlib.metadata,
                "distributions",
                return_value=(self.distribution,),
            ),
            mock.patch.object(
                html_framework,
                "_imported_framework_files",
                return_value=(self.init,),
            ),
            mock.patch.object(
                html_framework,
                "schema_catalog_root",
                side_effect=lambda version: self.catalog / version,
            ),
        )
        self.mocks = []
        for patch in patches:
            self.mocks.append(patch.start())
            self.addCleanup(patch.stop)

    def assert_refused(self):
        result = html_framework.observe_html_framework()
        self.assertIsInstance(result, HtmlRenderRefusal)
        self.assertEqual(result.code, "render.surface_unavailable")
        return result

    def test_exact_fixture_uses_existing_distribution_identity_without_writes(self):
        expected = observe_installed_framework_distribution(
            distribution_finder=lambda _: (self.distribution,)
        )
        before = {p: p.stat().st_mtime_ns for p in self.root.rglob("*")}
        with mock.patch.object(Path, "mkdir", side_effect=AssertionError("write")):
            result = html_framework.observe_html_framework()
        self.assertIsInstance(result, html_framework.HtmlFrameworkObservation)
        self.assertEqual(result.framework_distribution_identity, expected.identity)
        self.assertEqual(result.schema_catalog_release, "1.1.0")
        self.assertEqual(
            before, {p: p.stat().st_mtime_ns for p in self.root.rglob("*")}
        )

    def test_unrelated_wheel_cannot_stand_in_for_imported_source(self):
        other = _FixtureDistribution(self.root / "other")
        self.mocks[1].return_value = (other,)
        self.assert_refused()

    def test_extra_unrelated_wheel_does_not_change_exact_binding(self):
        before = html_framework.observe_html_framework()
        other = _FixtureDistribution(self.root / "other")
        self.mocks[1].return_value = (other, self.distribution)
        self.assertEqual(html_framework.observe_html_framework(), before)

    def test_duplicate_metadata_for_imported_files_refuses(self):
        self.mocks[1].return_value = (self.distribution, self.distribution)
        self.assert_refused()

    def test_editable_installation_refuses(self):
        self.distribution.direct_url = json.dumps({"dir_info": {"editable": True}})
        self.assert_refused()

    def test_unrecorded_imported_module_refuses(self):
        extra = self.init.parent / "unrecorded.py"
        extra.write_bytes(b"pass\n")
        self.mocks[2].return_value = (self.init, extra)
        self.assert_refused()

    def test_module_from_another_import_root_refuses(self):
        other = _FixtureDistribution(self.root / "other")
        self.mocks[2].return_value = (
            self.init,
            other.locate_file("literate_ai/__init__.py"),
        )
        self.assert_refused()

    def test_catalog_override_must_match_exact_bytes_and_inventory(self):
        other = _FixtureDistribution(self.root / "other")
        self.catalog = other.root / "share/literate-ai/schemas"
        self.assertIsInstance(
            html_framework.observe_html_framework(),
            html_framework.HtmlFrameworkObservation,
        )
        (self.catalog / "v2/index.json").write_bytes(b'{"fixture":3}\n')
        self.assert_refused()

    def test_missing_or_extra_catalog_member_refuses(self):
        extra = self.catalog / "v2/extra.json"
        extra.write_bytes(b"{}")
        self.assert_refused()
        extra.unlink()
        (self.catalog / "v2/index.json").unlink()
        self.assert_refused()

    def test_reobservation_detects_changed_wheel(self):
        before = html_framework.observe_html_framework()
        self.init.write_bytes(b'"""Different fixture code."""\n')
        after = html_framework.observe_html_framework()
        self.assertIsInstance(after, html_framework.HtmlFrameworkObservation)
        self.assertNotEqual(before, after)

    def test_discovery_failure_is_a_bounded_typed_refusal(self):
        self.mocks[1].side_effect = OSError("private filesystem detail")
        refusal = self.assert_refused()
        self.assertNotIn("private", refusal.message)

    def test_loaded_module_discovery_observes_real_python_files(self):
        # Exercise discovery separately from the synthetic distribution fixture.
        with mock.patch.dict("sys.modules", {"literate_ai.fixture": None}):
            paths = _REAL_IMPORTED_FILES()
        self.assertIn(self.init, paths)
        self.assertTrue(all(path.is_file() for path in paths))


_REAL_IMPORTED_FILES = html_framework._imported_framework_files
