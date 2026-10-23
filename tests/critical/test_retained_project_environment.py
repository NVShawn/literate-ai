"""Captured loader selection, live retained commands and unchanged default wires."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import unittest
from dataclasses import replace
from unittest.mock import patch

from literate_ai._filesystem import UnsafeFilesystemPathError
from literate_ai.adapters import retained_project_environment as environment
from literate_ai.adapters.retained_project_inputs import (
    discover_retained_project_inputs,
)
from literate_ai.contracts import ContentIdentity
from literate_ai.contracts.retained_project_lifecycle import (
    RetainedProjectAction,
    RetainedProjectProfile,
)
from tests.critical import test_retained_project_execution as commands


class RetainedOutputCommandPathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        commands.RetainedProjectExecutionTests.setUpClass()
        cls.addClassCleanup(commands.RetainedProjectExecutionTests.doClassCleanups)

    def setUp(self):
        self.fixture = commands.RetainedProjectExecutionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        actions = list(f.actions)
        actions[0] = replace(
            actions[0],
            argv=("export-nested", "{output_root}/bin/retained-child"),
            outputs=("bin",),
        )
        report = json.dumps(
            {
                "schema": "literate-ai/retained-project-test-report@1",
                "action": "tests",
                "cases": [
                    {"id": "original-suite", "outcome": "passed", "reason": None}
                ],
            }
        )
        actions[1] = replace(
            actions[1],
            argv=(
                "via-path",
                "retained-child",
                "report",
                "{output_root}/test-result.json",
                report,
            ),
        )
        f.profile = replace(
            f.profile,
            actions=tuple(actions),
            package_inventory=("bin", "docs.txt"),
            output_command_paths=("bin",),
        )
        f.plan = replace(f.plan, profile=f.profile)
        f.current = f.plan

    def test_opt_in_round_trip_preserves_default_wire(self):
        selected = self.fixture.profile
        legacy = replace(selected, output_command_paths=())
        self.assertNotIn("output_command_paths", legacy.to_dict())
        self.assertEqual(RetainedProjectProfile.from_dict(selected.to_dict()), selected)
        self.assertNotEqual(legacy.identity, selected.identity)
        for value in (
            None,
            [],
            "bin",
            ["../bin"],
            ["/bin"],
            ["bin", "BIN"],
            ["tools"],
            ["docs.txt"],
        ):
            wire = legacy.to_dict() | {"output_command_paths": value}
            with self.subTest(value=value), self.assertRaises((TypeError, ValueError)):
                RetainedProjectProfile.from_dict(wire)
        with self.assertRaises(ValueError):
            replace(selected, output_command_paths=tuple(f"bin/{i}" for i in range(33)))

    def test_missing_future_directory_and_captured_tools_have_fixed_order(self):
        f = self.fixture
        workspace = f.workspace()
        selected = workspace._environment()["PATH"].split(os.pathsep)
        self.assertEqual(
            selected, [str(workspace.outputs / "bin"), str(workspace.inputs / "tools")]
        )
        self.assertFalse((workspace.outputs / "bin").exists())
        self.assertEqual(workspace.observations, ())

    @unittest.skipUnless(os.name == "nt", "Requires native Windows command")
    def test_actual_windows_command_and_late_source_or_context_drift(self):
        from literate_ai.adapters import retained_windows_context

        f = self.fixture
        native = retained_windows_context.RetainedWindowsSystemContext.capture()
        path = f.source / "tools/whoami.exe"
        shutil.copyfile(native.root / "System32/whoami.exe", path)
        f.manifest = discover_retained_project_inputs(
            f.source,
            {"src": "source", "tools": "toolchain", "test-policy.json": "test-policy"},
            f.manifest.limits,
            executable_paths=tuple(sorted((f.runner_path, "tools/whoami.exe"))),
        )
        identity = ContentIdentity.parse_uri(
            "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        )
        action = RetainedProjectAction(
            "native-identity",
            "stage",
            "windows-identity",
            (),
            (),
            ("native-windows-identity",),
            (),
            10,
        )
        f.profile = replace(
            f.profile,
            tools=(
                *f.profile.tools,
                ("windows-identity", "tools/whoami.exe", identity),
            ),
            actions=(
                action,
                replace(
                    f.profile.actions[0],
                    requires=(action.action_id, *f.profile.actions[0].requires),
                ),
                *f.profile.actions[1:],
            ),
        )
        f.plan = replace(
            f.plan, profile=f.profile, manifest_identity=f.manifest.identity
        )
        f.current = f.plan
        for changed in ("context", "source"):
            with self.subTest(changed=changed):
                workspace = f.workspace()
                workspace.execute_next()
                self.assertEqual(len(workspace.observations), 1)
                observed = workspace.observations[0]
                self.assertEqual(observed.result.returncode, 0)
                self.assertTrue(observed.result.stdout.strip())
                self.assertEqual(
                    workspace._environment()["SystemRoot"], str(native.root)
                )
                if changed == "context":
                    with patch.object(
                        retained_windows_context,
                        "_system_windows_directory",
                        return_value=f.base,
                    ):
                        with self.assertRaisesRegex(
                            ValueError, "system-context-changed"
                        ):
                            workspace.execute_next()
                else:
                    path.write_bytes(path.read_bytes() + b"substitution")
                    with self.assertRaises(ValueError):
                        workspace.execute_next()
                self.assertEqual(len(workspace.observations), 1)

    @unittest.skipUnless(
        os.name != "nt" and shutil.which("cc"), "requires native POSIX fixture"
    )
    def test_original_style_lookup_executes_new_export_and_refuses_without_selection(
        self,
    ):
        f = self.fixture
        workspace = f.workspace()
        workspace.execute_next()
        with patch.dict(os.environ, {"PATH": "/unreviewed/ambient"}):
            self.assertNotIn("/unreviewed/ambient", workspace._environment()["PATH"])
        # Host inspectors retain their own trusted discovery context; only the
        # target command receives the captured/output-only search path.
        result = workspace.execute_next()
        self.assertEqual(result.result.returncode, 0)
        self.assertEqual(workspace.test_reports[0].report.passed, 1)
        f.plan = replace(f.plan, profile=replace(f.profile, output_command_paths=()))
        f.current = f.plan
        unselected = f.workspace()
        unselected.execute_next()
        with self.assertRaises(ValueError):
            unselected.execute_next()
        self.assertEqual(len(unselected.observations), 2)
        self.assertEqual(unselected.observations[-1].result.returncode, 96)
        self.assertEqual(unselected.test_reports, ())

    @unittest.skipIf(os.name == "nt", "requires POSIX directory symlink")
    def test_existing_file_and_redirected_directory_refuse(self):
        f = self.fixture
        for redirected in (False, True):
            workspace = f.workspace()
            path = workspace.outputs / "bin"
            if redirected:
                path.symlink_to(f.source / "tools", target_is_directory=True)
            else:
                path.write_bytes(b"not a directory")
            with (
                self.subTest(redirected=redirected),
                self.assertRaises((ValueError, OSError, UnsafeFilesystemPathError)),
            ):
                workspace._environment()


class RetainedRuntimePathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        commands.RetainedProjectExecutionTests.setUpClass()
        cls.addClassCleanup(commands.RetainedProjectExecutionTests.doClassCleanups)

    def setUp(self):
        self.fixture = commands.RetainedProjectExecutionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.directory = self.fixture.source / "tools/lib"
        self.directory.mkdir()
        (self.directory / "member").write_bytes(b"captured library input")
        self.refresh()

    def refresh(self, paths=("tools/lib",)):
        f = self.fixture
        f.manifest = discover_retained_project_inputs(
            f.source,
            {"src": "source", "tools": "toolchain", "test-policy.json": "test-policy"},
            f.manifest.limits,
            executable_paths=(f.runner_path,),
        )
        f.profile = replace(f.profile, runtime_library_paths=paths)
        f.plan = replace(
            f.plan, profile=f.profile, manifest_identity=f.manifest.identity
        )
        f.current = f.plan

    def test_legacy_wire_and_explicit_selection_round_trip(self):
        profile = replace(self.fixture.profile, runtime_library_paths=())
        self.assertNotIn("runtime_library_paths", profile.to_dict())
        self.assertEqual(RetainedProjectProfile.from_dict(profile.to_dict()), profile)
        selected = self.fixture.profile
        self.assertEqual(RetainedProjectProfile.from_dict(selected.to_dict()), selected)
        self.assertNotEqual(profile.identity, selected.identity)
        for value in (
            None,
            [],
            "",
            ["../ambient"],
            ["/ambient"],
            ["tools/lib", "TOOLS/lib"],
        ):
            wire = profile.to_dict()
            wire["runtime_library_paths"] = value
            with self.subTest(value=value), self.assertRaises((TypeError, ValueError)):
                RetainedProjectProfile.from_dict(wire)
        with self.assertRaises(ValueError):
            replace(profile, runtime_library_paths=tuple(f"d{i}" for i in range(33)))

    def test_host_mapping_order_and_no_caller_environment(self):
        f = self.fixture
        (f.source / "tools/second").mkdir()
        (f.source / "tools/second/member").write_bytes(b"other captured library")
        self.refresh(("tools/second", "tools/lib"))
        expected = ":".join(str(f.source / p) for p in f.profile.runtime_library_paths)
        for platform, variable in (
            ("linux", "LD_LIBRARY_PATH"),
            ("freebsd16", "LD_LIBRARY_PATH"),
            ("darwin", "DYLD_LIBRARY_PATH"),
        ):
            with (
                self.subTest(platform=platform),
                patch.object(environment.sys, "platform", platform),
                patch.dict(
                    os.environ,
                    {"LD_PRELOAD": "/ambient", "LD_LIBRARY_PATH": "/ambient"},
                ),
            ):
                self.assertEqual(
                    environment.captured_runtime_environment(
                        f.source, f.manifest, f.profile
                    ),
                    {variable: expected},
                )
        with patch.object(environment.sys, "platform", "win32"):
            with self.assertRaisesRegex(ValueError, "unsupported"):
                environment.captured_runtime_environment(
                    f.source, f.manifest, f.profile
                )
            self.assertEqual(
                environment.captured_runtime_environment(
                    f.source, f.manifest, replace(f.profile, runtime_library_paths=())
                ),
                {},
            )

    @patch.object(environment.sys, "platform", "linux")
    def test_source_empty_missing_and_file_directories_refuse(self):
        f = self.fixture
        for name in ("src", "missing", f.runner_path):
            with self.subTest(name=name), self.assertRaises(ValueError):
                environment.captured_runtime_environment(
                    f.source,
                    f.manifest,
                    replace(f.profile, runtime_library_paths=(name,)),
                )
        mixed = replace(
            f.manifest,
            members=tuple(
                replace(m, role="source") if m.path == "tools/lib/member" else m
                for m in f.manifest.members
            ),
        )
        with self.assertRaisesRegex(ValueError, "role"):
            environment.captured_runtime_environment(f.source, mixed, f.profile)
        (self.directory / "member").unlink()
        self.refresh()
        with self.assertRaisesRegex(ValueError, "role"):
            environment.captured_runtime_environment(f.source, f.manifest, f.profile)

    @unittest.skipIf(os.name == "nt", "requires native directory symlink")
    def test_redirected_runtime_directory_refuses(self):
        f = self.fixture
        self.directory.rename(self.directory.with_name("original"))
        self.directory.symlink_to("original", target_is_directory=True)
        with self.assertRaises((OSError, ValueError, UnsafeFilesystemPathError)):
            environment.captured_runtime_environment(f.source, f.manifest, f.profile)

    @unittest.skipUnless(
        os.name != "nt" and shutil.which("cc"), "requires native POSIX compiler"
    )
    def test_real_private_library_tool_runs_only_with_reviewed_captured_search_path(
        self,
    ):
        f = self.fixture
        compiler = shutil.which("cc")
        library_source = f.base / "private.c"
        library_source.write_text("int private_fixture_sentinel(void) { return 73; }\n")
        extension = ".dylib" if sys.platform == "darwin" else ".so"
        library = self.directory / ("libprivate_fixture" + extension)
        options = (
            ["-dynamiclib", "-Wl,-install_name,@rpath/" + library.name]
            if sys.platform == "darwin"
            else ["-shared", "-fPIC", "-Wl,-soname," + library.name]
        )
        subprocess.run(
            [compiler, *options, str(library_source), "-o", str(library)],
            check=True,
            capture_output=True,
            timeout=30,
        )
        original = (
            commands.RetainedProjectExecutionTests.fixture_binary.parent / "fixture.c"
        )
        object_file = f.base / "fixture.o"
        subprocess.run(
            [
                compiler,
                "-Dmain=fixture_main",
                "-c",
                str(original),
                "-o",
                str(object_file),
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        wrapper = f.base / "wrapper.c"
        wrapper.write_text(
            "int private_fixture_sentinel(void); int fixture_main(int,char**);\n"
            "int main(int n,char**v) { return private_fixture_sentinel()==73\n"
            " ? fixture_main(n,v) : 97; }\n"
        )
        executable = f.source / "tools/runner"
        subprocess.run(
            [
                compiler,
                str(wrapper),
                str(object_file),
                "-L" + str(self.directory),
                "-lprivate_fixture",
                "-o",
                str(executable),
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        without = subprocess.run(
            [str(executable), "empty"],
            env={"LANG": "C"},
            capture_output=True,
            timeout=10,
        )
        self.assertNotEqual(without.returncode, 0)
        f.profile = replace(
            f.profile,
            tools=(
                (
                    "shell",
                    "tools/runner",
                    ContentIdentity.parse_uri(
                        "sha256:" + hashlib.sha256(executable.read_bytes()).hexdigest()
                    ),
                ),
            ),
        )
        self.refresh()
        # Only the captured search path is projected for this tool's private
        # dependency; its compiled image contains no fixture library rpath.
        workspace = f.workspace()
        self.addCleanup(shutil.rmtree, workspace.root)
        variable = (
            "DYLD_LIBRARY_PATH" if sys.platform == "darwin" else "LD_LIBRARY_PATH"
        )
        projected = workspace._environment()
        self.assertEqual(projected[variable], str(workspace.inputs / "tools/lib"))
        self.assertNotIn("LD_PRELOAD", projected)
        for _ in f.plan.profile.actions:
            workspace.execute_next()
        self.assertEqual(len(workspace.observations), 4)
        self.assertEqual((workspace.outputs / "docs.txt").read_bytes(), b"docs")
        # Source drift still invalidates a reviewed path after successful use.
        library.write_bytes(b"substituted library")
        with self.assertRaises(ValueError):
            workspace._require_current()
