"""Public-run orchestration retains real execution and refuses authority shortcuts."""

import json
import os
import shutil
import unittest
from argparse import Namespace
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters.retained_project_planning import RetainedProjectPlanningResult
from literate_ai.adapters.retained_project_runtime import execute_local_retained_project
from literate_ai.cache_directories import CacheDirectories
from literate_ai.cli.errors import CliFailure
from literate_ai.cli.retained_project import retained_project_from_args
from literate_ai.contracts import BlobRef, canonical_json_bytes
from literate_ai.contracts.execution_dispatch import (
    ExecutionWorker,
    ExecutionWorkerKind,
)
from literate_ai.contracts.retained_project_sbom import (
    project_retained_project_managed_graph,
)
from literate_ai.storage.cas import FileSystemCAS
from tests.critical import test_retained_project_completion as completed
from tests.critical import test_retained_project_execution as commands


@unittest.skipUnless(
    os.name != "nt" and shutil.which("cc"), "requires native POSIX fixture"
)
class RetainedProjectRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        commands.RetainedProjectExecutionTests.setUpClass()
        cls.addClassCleanup(commands.RetainedProjectExecutionTests.doClassCleanups)

    def setUp(self):
        self.fixture = completed.RetainedProjectCompletionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.worker = ExecutionWorker("local", ExecutionWorkerKind.LOCAL)
        f = self.fixture.fixture
        f.plan = replace(
            f.plan,
            profile=replace(f.plan.profile, worker_identity=self.worker.identity),
        )
        f.current = f.plan
        self.directories = CacheDirectories(
            f.source, f.base / "generated", f.base / "objects"
        )

    def run_project(self, **changes):
        f = self.fixture.fixture
        planning = RetainedProjectPlanningResult(
            f.plan,
            project_retained_project_managed_graph(
                f.plan, self.fixture.locks, version="1.0.0"
            ),
        )
        return execute_local_retained_project(
            f.source,
            f.manifest,
            planning,
            **{
                "component_locks": self.fixture.locks,
                "worker": self.worker,
                "current_plan": lambda: f.current,
                "test_receipt_policy": f.configured_test_policy,
                "cache_directories": self.directories,
                "authorization": f.plan.identity.uri,
                "allow_host_execution": True,
                **changes,
            },
        )

    def test_real_complete_run_returns_live_admission_and_reopens_custody(self):
        admission, workspace = self.run_project()
        admission.require_current()
        self.assertEqual(admission.receipt.passed, 1)
        self.assertFalse(admission.receipt.to_dict()["conversion_advanced"])
        self.assertFalse(admission.receipt.to_dict()["release_authorized"])
        diagnostics = json.loads(
            (workspace.root / "command-diagnostics.json").read_bytes()
        )
        self.assertFalse(diagnostics["admission"])
        self.assertEqual(len(diagnostics["command_observations"]), 4)
        for row in diagnostics["command_observations"]:
            for reference in row.values():
                admission.store.verify(BlobRef.from_dict(reference))
        destination = self.fixture.fixture.base / "reopened-products"
        admission.materialize_payload(destination)
        self.assertTrue((destination / "app.bin").is_file())

    def test_authorization_and_worker_drift_refuse_before_creating_caches(self):
        for changes in (
            {"authorization": "sha256:" + "0" * 64},
            {"allow_host_execution": False},
            {"worker": replace(self.worker, worker_id="different")},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.run_project(**changes)
            self.assertFalse(self.directories.build_dir.exists())
            self.assertFalse(self.directories.obj_dir.exists())

    def test_changed_current_plan_refuses_before_start(self):
        self.fixture.fixture.current = replace(
            self.fixture.fixture.plan, project_id="changed"
        )
        with self.assertRaisesRegex(ValueError, "plan-changed"):
            self.run_project()
        self.assertFalse(self.directories.obj_dir.exists())

    def test_missing_independent_cases_refuse_before_commands_or_caches(self):
        f = self.fixture.fixture
        f.plan = replace(
            f.plan, profile=replace(f.plan.profile, acceptance_policy_identity=None)
        )
        f.current = f.plan
        with self.assertRaisesRegex(ValueError, "independent-acceptance-required"):
            self.run_project()
        self.assertFalse(self.directories.build_dir.exists())
        self.assertFalse(self.directories.obj_dir.exists())

    def test_remote_worker_never_falls_back_to_local_execution(self):
        remote = ExecutionWorker(
            "remote", ExecutionWorkerKind.COMMAND, command=("unused-worker",)
        )
        with self.assertRaisesRegex(ValueError, "worker-runtime-unavailable"):
            self.run_project(worker=remote)
        self.assertFalse(self.directories.obj_dir.exists())

    def test_sdk_inputs_cannot_use_ambient_producers(self):
        f = self.fixture.fixture
        f.manifest = replace(
            f.manifest,
            members=tuple(
                replace(m, role="sdk") if m.path == "tools/runner" else m
                for m in f.manifest.members
            ),
        )
        f.plan = replace(
            f.plan,
            manifest_identity=f.manifest.identity,
            profile=replace(
                f.plan.profile,
                toolchain_identity=f.manifest.role_identity({"sdk", "toolchain"}),
            ),
        )
        f.current = f.plan
        with self.assertRaisesRegex(ValueError, "producer-bindings-required"):
            self.run_project()
        self.assertEqual(
            list(self.directories.obj_dir.glob("retained-project-runs/*")), []
        )

    def test_failed_command_retains_streams_without_receipt(self):
        f = self.fixture.fixture
        actions = list(f.plan.profile.actions)
        actions[0] = replace(actions[0], argv=("fail",))
        f.plan = replace(
            f.plan, profile=replace(f.plan.profile, actions=tuple(actions))
        )
        f.current = f.plan
        with self.assertRaisesRegex(ValueError, "command-failed"):
            self.run_project()
        (index,) = self.directories.obj_dir.glob(
            "retained-project-runs/*/command-diagnostics.json"
        )
        diagnostics = json.loads(index.read_bytes())
        self.assertFalse(diagnostics["admission"])
        self.assertEqual(len(diagnostics["command_observations"]), 1)
        store = FileSystemCAS(
            self.directories.build_dir / "retained-project-cas", create=False
        )
        row = diagnostics["command_observations"][0]
        self.assertEqual(store.get_bytes(BlobRef.from_dict(row["stderr"])), b"failed")
        self.assertNotEqual(
            store.get_manifest(BlobRef.from_dict(row["observation"]))["exit_code"], 0
        )

    def test_changed_source_invalidates_returned_admission(self):
        admission, _workspace = self.run_project()
        (self.fixture.fixture.source / "src/main.txt").write_bytes(b"changed")
        with self.assertRaises(ValueError):
            admission.require_current()

    def handler_arguments(self):
        f = self.fixture.fixture
        (f.source / "manifest.json").write_bytes(
            canonical_json_bytes(f.manifest.to_dict())
        )
        (f.source / "profile.json").write_bytes(
            canonical_json_bytes(f.plan.profile.to_dict())
        )
        return Namespace(
            retained_project="manifest.json",
            retained_project_profile="profile.json",
            retained_project_plan=False,
            force_regeneration=False,
            specification=".",
            target="host",
            authorize_retained_project=f.plan.identity.uri,
            allow_host_execution=True,
            build_dir=str(self.directories.build_dir),
            obj_dir=str(self.directories.obj_dir),
        )

    def call_handler(self, args, *, changed_after_first_read=False):
        """Only authority discovery is a fixture; target execution/admission is real.

        These handler-wiring checks do not qualify installed canonical-project
        discovery, Standard binding or complete public lifecycle packaging.
        """
        f = self.fixture.fixture
        project = SimpleNamespace(
            root=f.source,
            definition=SimpleNamespace(test_receipt_policy=f.configured_test_policy),
        )
        planning = RetainedProjectPlanningResult(
            f.plan,
            project_retained_project_managed_graph(
                f.plan, self.fixture.locks, version="1.0.0"
            ),
        )
        calls = []

        def discover(_project, _binding, manifest, profile, **selection):
            self.assertEqual(manifest, f.manifest)
            self.assertEqual(selection["worker_identity"], self.worker.identity)
            if profile != f.plan.profile:
                raise ValueError("fixture.authority.profile-changed")
            calls.append(profile)
            if changed_after_first_read and len(calls) == 1:
                actions = list(profile.actions)
                actions[0] = replace(actions[0], timeout_seconds=11)
                changed = replace(profile, actions=tuple(actions))
                (f.source / "profile.json").write_bytes(
                    canonical_json_bytes(changed.to_dict())
                )
            return planning

        with (
            patch(
                "literate_ai.cli.retained_project.plan_retained_project_with_graph",
                side_effect=discover,
            ),
            patch(
                "literate_ai.adapters.component_lock_application.current_project_component_locks",
                return_value=self.fixture.locks,
            ),
        ):
            return retained_project_from_args(project, object(), args), calls

    def test_handler_planning_is_read_only(self):
        args = self.handler_arguments()
        args.retained_project_plan = True
        result, calls = self.call_handler(args)
        self.assertFalse(result["admitted"])
        self.assertEqual(len(calls), 1)
        self.assertFalse(self.directories.build_dir.exists())
        self.assertFalse(self.directories.obj_dir.exists())

    def test_handler_uses_real_live_admission_after_fresh_reads(self):
        result, calls = self.call_handler(self.handler_arguments())
        self.assertTrue(result["admitted"])
        self.assertGreater(len(calls), 4)
        self.assertFalse(result["conversion_advanced"])
        self.assertFalse(result["package_qualified"])
        self.assertFalse(result["release_authorized"])

    def test_handler_profile_drift_is_reopened_before_cache_writes(self):
        with self.assertRaises(CliFailure) as error:
            self.call_handler(self.handler_arguments(), changed_after_first_read=True)
        self.assertEqual(error.exception.code, "retained_project.execution_failed")
        self.assertIn("profile-changed", str(error.exception))
        self.assertFalse(self.directories.obj_dir.exists())
