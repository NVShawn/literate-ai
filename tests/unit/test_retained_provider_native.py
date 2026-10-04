"""Native provider inputs use real locks, promotion files and oracle custody."""

import hashlib
import json
import unittest
from unittest import mock

from literate_ai.adapters.lifecycle.standard_local import LocalStandardLifecycleError
from literate_ai.adapters.qualification import FilesystemQualificationError
from literate_ai.adapters.retained_provider_native import read_retained_provider_native
from literate_ai.adapters.standard_project import (
    project_locked_standard_toolchain_closure,
)
from literate_ai.projects import PinnedInputClosureError
from tests.support import fixtures_test_retained_provider_authority as provider_fixtures
from tests.support import fixtures_test_retained_provider_generation as generation_fixtures
from tests.support import fixtures_test_standard_command_projection as command_fixtures


class RetainedProviderNativeTests(unittest.TestCase):
    def setUp(self):
        class LibraryGenerationFixture(
            generation_fixtures.RetainedProviderGenerationTests
        ):
            def setUp(inner):
                super().setUp()
                path = inner.component / "component.md"
                content = (
                    path.read_text()
                    .replace("version: 1.0.0\n", "version: 1.0.0\nkind: library\n", 1)
                    .replace(
                        "entrypoints:\n  - name: run\n"
                        "    kind: portable-application\n    path: run\n",
                        "entrypoints: []\n",
                    )
                    .replace(
                        "  - name: sample.portable-app\n    version: 1.0.0\n",
                        "  - name: sample.portable-app\n    version: 1.0.0\n"
                        "    interface:\n      uri: interfaces/library.md\n",
                    )
                )
                path.write_text(content)
                interface = inner.component / "interfaces/library.md"
                interface.parent.mkdir(exist_ok=True)
                interface.write_text(
                    "# Library\n\nThe library SHALL expose its portable capability.\n"
                )

        self.fixture = provider_fixtures.RetainedProviderAuthorityTests("runTest")
        self.fixture.generation_fixture_class = LibraryGenerationFixture
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.provider = self.fixture.read()
        self.tool_changed = False

        def discover(name, constraint, environment):
            tool = command_fixtures._tool(name)

            def guard():
                if self.tool_changed:
                    raise ValueError("fixture native tool changed")

            tool.require_unchanged = guard
            return tool

        def project(snapshot, execution, **kwargs):
            return project_locked_standard_toolchain_closure(
                snapshot,
                execution,
                host_platform="macos",
                toolchain_discoverer=discover,
                dependency_observer=command_fixtures._observation,
                **kwargs,
            )

        patch = mock.patch(
            "literate_ai.adapters.retained_provider_native.project_locked_standard_toolchain_closure",
            side_effect=project,
        )
        patch.start()
        self.addCleanup(patch.stop)
        projected = project(
            self.provider.generation.prepared.locked_authority_snapshot,
            self.provider.generation.execution,
        )
        lock = self.fixture.authority.lock
        node = next(n for n in lock.nodes if n.revision.identity == lock.root_revision)
        surface = next(
            c for c in projected.contracts if c.component_revision == lock.root_revision
        ).library_import_surface
        self.oracle_path = self.fixture.root / "verification/acceptance/greeting.json"
        self.oracle_path.parent.mkdir(parents=True)
        self.harness = self.oracle_path.with_name("harness.py")
        self.harness.write_bytes(b"# Independently reviewed fixture harness.\n")
        self.document = {
            "schema": "literate-ai/library-acceptance-oracle@1",
            "component": "greeting",
            "specification_set_identity": node.revision.specification_set_identity.uri,
            "public_interface_identities": sorted(
                i.identity.uri for i in node.revision.public_interfaces
            ),
            "import_surface_identity": surface.identity.uri,
            "language": surface.language,
            "harness": self.harness.name,
            "harness_identity": "sha256:"
            + hashlib.sha256(self.harness.read_bytes()).hexdigest(),
            "cases": [
                {
                    "case_id": case.case_id,
                    "capability": surface.capabilities[0].capability,
                    "arguments": list(case.arguments),
                    "expected_result": case.expected_result,
                }
                for case in self.fixture.profile.cases
            ],
        }
        self.write_oracle()

    def write_oracle(self):
        self.oracle_path.write_text(json.dumps(self.document))

    def read(self):
        return read_retained_provider_native(self.provider, environment={"PATH": ""})

    def test_current_commands_and_oracle_preserve_files_without_runtime(self):
        before = self.fixture.inventory()
        with mock.patch("subprocess.Popen", side_effect=AssertionError("runtime")):
            current = self.read()
            current.require_unchanged()
        self.assertEqual(set(current.commands), set(self.provider.generation.recipes))
        self.assertEqual(current.oracle.harness_content, self.harness.read_bytes())
        self.assertEqual(before, self.fixture.inventory())
        with self.assertRaises(TypeError):
            current.commands[self.fixture.authority.lock.root_revision] = None

    def test_oracle_file_and_harness_drift_invalidate_custody(self):
        for path in (self.oracle_path, self.harness):
            with self.subTest(path=path.name):
                current = self.read()
                original = path.read_bytes()
                path.write_bytes(original + b"\n")
                with self.assertRaises(PinnedInputClosureError):
                    current.require_unchanged()
                path.write_bytes(original)

    def test_changed_native_tool_invalidates_current_guard(self):
        current = self.read()
        self.tool_changed = True
        with self.assertRaisesRegex(LocalStandardLifecycleError, "toolchain changed"):
            current.require_unchanged()

    def test_changed_oracle_cases_refuse_current_profile(self):
        self.document["cases"][0]["expected_result"] = "changed"
        self.write_oracle()
        with self.assertRaises(FilesystemQualificationError):
            self.read()

    def test_wrong_current_specification_or_surface_refuses(self):
        for key in ("specification_set_identity", "import_surface_identity"):
            with self.subTest(key=key):
                original = self.document[key]
                self.document[key] = "sha256:" + "a" * 64
                self.write_oracle()
                with self.assertRaisesRegex(ValueError, "oracle-authority-mismatch"):
                    self.read()
                self.document[key] = original

    def test_mutating_returned_case_data_does_not_change_authority(self):
        current = self.read()
        current.oracle.cases[0].arguments.append("unreviewed")
        with self.assertRaisesRegex(ValueError, "oracle-changed"):
            current.require_unchanged()

    def test_oversized_harness_is_refused_by_bounded_custody(self):
        self.harness.write_bytes(b"x" * (1024 * 1024 + 1))
        with self.assertRaises(PinnedInputClosureError):
            self.read()

    def test_tool_mutation_while_loading_oracle_prevents_return(self):
        from literate_ai.adapters.component_acceptance import load_library_acceptance

        def load_then_change(*args, **kwargs):
            oracle = load_library_acceptance(*args, **kwargs)
            self.tool_changed = True
            return oracle

        with (
            mock.patch(
                "literate_ai.adapters.qualification.load_library_acceptance",
                side_effect=load_then_change,
            ),
            self.assertRaises(LocalStandardLifecycleError),
        ):
            self.read()

    def test_oracle_mutation_during_capture_prevents_return(self):
        from literate_ai.adapters.component_acceptance import load_library_acceptance

        def load_then_change(*args, **kwargs):
            oracle = load_library_acceptance(*args, **kwargs)
            self.harness.write_bytes(b"# changed after capture\n")
            return oracle

        with (
            mock.patch(
                "literate_ai.adapters.qualification.load_library_acceptance",
                side_effect=load_then_change,
            ),
            self.assertRaises(PinnedInputClosureError),
        ):
            self.read()
