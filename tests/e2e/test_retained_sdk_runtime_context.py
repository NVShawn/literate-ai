"""Exercise the captured loader/SDK boundary with real native library loading."""

import os
import shutil
import stat
import subprocess
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters import retained_project_execution as execution
from literate_ai.adapters.retained_project_runtime import execute_local_retained_project
from literate_ai.contracts import canonical_identity
from literate_ai.security import AuthorizationError
from tests.e2e import test_retained_sdk_public as public


@unittest.skipUnless(
    os.name != "nt" and shutil.which("cc") and shutil.which("cmake"),
    "requires native POSIX CMake fixture",
)
class CapturedRetainedSdkRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        public.PublicRetainedSdkRuntimeTests.setUpClass()
        cls.addClassCleanup(public.PublicRetainedSdkRuntimeTests.doClassCleanups)

    def setUp(self):
        public.PublicRetainedSdkRuntimeTests.setUp(self)
        f = self.fixture
        library = next(
            p
            for p in self.built.product.snapshot.native_libraries
            if "/vendor_math." in p
        )
        self.library = Path(library)
        profile = f.plan.profile
        actions = list(profile.actions)
        # A basename lookup genuinely requires the captured loader search path.
        actions[0] = replace(
            actions[0], argv=("scale", self.library.name, "{output_root}/scale.txt")
        )
        f.plan = replace(
            f.plan,
            profile=replace(
                profile,
                actions=tuple(actions),
                runtime_library_paths=("vendor/" + self.library.parent.as_posix(),),
            ),
        )
        self.case.refresh()

    def test_public_runtime_retains_required_captured_loader_context(self):
        f = self.fixture
        tool = next(path for name, path, _ in f.plan.profile.tools if name == "shell")
        control = subprocess.run(
            [
                str(f.source / tool),
                "scale",
                self.library.name,
                str(f.source / "without-loader.txt"),
            ],
            cwd=f.source,
            env={"PATH": os.defpath},
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(control.returncode, 17)
        self.assertFalse((f.source / "without-loader.txt").exists())
        args, kwargs = self.case.public_runtime_arguments()
        with patch(
            "literate_ai.adapters.retained_project_sdk_runtime.project_component_roots",
            return_value=(self.sdk_fixture.recipe_fixture.component,),
        ):
            admission, workspace = execute_local_retained_project(*args, **kwargs)
            admission.require_current()
        self.assertEqual((workspace.outputs / "scale.txt").read_bytes(), b"4.5")
        records = [
            admission.store.get_manifest(ref)
            for ref in admission.receipt.records
            if ref.media_type == "application/json"
        ]
        sdk = [
            r
            for r in records
            if r.get("schema") == "literate-ai/native-sdk-command-execution@4"
        ]
        self.assertEqual(len(sdk), 6)
        for command in sdk:
            context = command["captured_runtime_context"]
            self.assertTrue(context["captured_loader_environment"])
            self.assertEqual(
                context["environment_identity"],
                command["command_binding"]["environment_identity"],
            )
            self.assertTrue(context["observation"]["components"])
        self.assertFalse(admission.receipt.to_dict()["release_authorized"])

    def test_ambient_overrides_refuse_before_command_or_grant(self):
        workspace = self.case.workspace()
        action = workspace.plan.profile.actions[0]
        loader_key = next(
            key
            for key in workspace._environment()
            if key in {"LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"}
        )
        for name, value in [
            ("PYTHONPATH", "/ambient"),
            ("LD_PRELOAD", "/ambient.so"),
            ("DYLD_INSERT_LIBRARIES", "/ambient.dylib"),
            (loader_key, "/unreviewed/loader"),
        ]:
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(ValueError, "uncaptured-loader-environment"),
            ):
                with workspace._sdk_command_scope(
                    workspace._tools[action.tool].command,
                    tool=workspace._tools[action.tool],
                    cwd=workspace.objects,
                    environment={**workspace._environment(), name: value},
                    action_identity=canonical_identity(action.to_dict()),
                    phase=action.stage,
                ):
                    self.fail("ambient loader context reached execution")
        self.assertEqual(self.case.grants, [])

    def test_captured_shadow_library_refuses_before_execution(self):
        f = self.fixture
        directory = f.source / "tools/shadow"
        directory.mkdir()
        library = f.source / "vendor" / self.library
        shutil.copyfile(library, directory / self.library.name)
        f.plan = replace(
            f.plan,
            profile=replace(
                f.plan.profile,
                runtime_library_paths=(
                    "tools/shadow",
                    *f.plan.profile.runtime_library_paths,
                ),
            ),
        )
        self.case.refresh()
        workspace = self.case.workspace()
        with (
            patch.object(execution, "run_bounded_process") as run,
            self.assertRaisesRegex(ValueError, "shadows-sdk-dependency"),
        ):
            workspace.execute_next()
        run.assert_not_called()
        self.assertEqual(self.case.grants, [])

    def test_captured_input_change_refuses_before_execution(self):
        workspace = self.case.workspace()
        library = workspace.inputs / "vendor" / self.library
        mode = stat.S_IMODE(library.stat().st_mode)
        library.chmod(mode | 0o200)
        library.write_bytes(library.read_bytes() + b"changed")
        library.chmod(mode)
        with (
            patch.object(execution, "run_bounded_process") as run,
            self.assertRaises(ValueError),
        ):
            workspace.execute_next()
        run.assert_not_called()
        self.assertEqual(self.case.grants, [])

    def test_revocation_after_real_captured_sdk_command_prevents_observation(self):
        workspace = self.case.workspace()
        original = execution.run_bounded_process

        def execute_then_revoke(*args, **kwargs):
            result = original(*args, **kwargs)
            self.case.revoke_latest()
            return result

        with (
            patch.object(execution, "run_bounded_process", execute_then_revoke),
            self.assertRaises(AuthorizationError),
        ):
            workspace.execute_next()
        self.assertEqual(workspace.observations, ())

    def test_private_case_keeps_captured_context_through_cleanup(self):
        self.case.test_private_filesystem_case_preserves_live_sdk_command_scope()
        commands = [
            v
            for v in self.case.documents.values()
            if v.get("schema") == "literate-ai/native-sdk-command-execution@4"
        ]
        self.assertEqual(len(commands), 6)

    def test_process_uses_bound_environment_if_authorizer_mutates_caller_mapping(self):
        workspace = self.case.workspace()
        environment = workspace._environment()
        loader_key = next(
            k for k in environment if k in {"LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"}
        )
        expected_loader = environment[loader_key]
        original_authorize = workspace._native_sdk_authorize
        original_execute = execution.run_bounded_process
        used = []

        def authorize_then_change(*args):
            grant = original_authorize(*args)
            environment[loader_key] = "/unreviewed/loader"
            return grant

        def observe_process(*args, **kwargs):
            used.append(kwargs["environment"][loader_key])
            return original_execute(*args, **kwargs)

        workspace._native_sdk_authorize = authorize_then_change
        with (
            patch.object(workspace, "_environment", return_value=environment),
            patch.object(execution, "run_bounded_process", observe_process),
            self.assertRaises(ValueError),
        ):
            workspace.execute_next()
        self.assertEqual(used, [expected_loader])
        self.assertEqual((workspace.outputs / "scale.txt").read_bytes(), b"4.5")
        self.assertEqual(workspace.observations, ())
