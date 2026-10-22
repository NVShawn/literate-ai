"""Captured loader selection, live retained commands and unchanged default wires."""

import hashlib
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
from literate_ai.contracts.retained_project_lifecycle import RetainedProjectProfile
from tests.critical import test_retained_project_execution as commands


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
            executable_paths=("tools/runner",),
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

    def test_source_empty_missing_and_file_directories_refuse(self):
        f = self.fixture
        for name in ("src", "missing", "tools/runner"):
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
