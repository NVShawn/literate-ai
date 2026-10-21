"""Public-run orchestration retains real execution and refuses authority shortcuts."""

import json
import os
import shutil
import sys
import unittest
from argparse import Namespace
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

from literate_ai.adapters.retained_project_planning import RetainedProjectPlanningResult
from literate_ai.adapters.retained_project_runtime import execute_local_retained_project
from literate_ai.adapters.worker_provisioning import configure
from literate_ai.cache_directories import CacheDirectories
from literate_ai.cli.errors import CliFailure
from literate_ai.cli.retained_project import retained_project_from_args
from literate_ai.contracts import BlobRef, canonical_json_bytes
from literate_ai.contracts.execution_dispatch import (
    ExecutionRequirements,
    ExecutionWorker,
    ExecutionWorkerKind,
)
from literate_ai.contracts.projects import StandardProjectLifecycleDriver
from literate_ai.contracts.retained_project_sbom import (
    project_retained_project_managed_graph,
)
from literate_ai.contracts.retained_secondary_access import (
    RetainedReadOnlyView,
    RetainedSecondaryAccessPolicy,
)
from literate_ai.contracts.retained_test_identity import RetainedSecondaryTestIdentity
from literate_ai.contracts.worker_provisioning import WorkerProvisioner
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
        evidence = [
            admission.store.get_manifest(ref)
            for ref in admission.receipt.records
            if ref.media_type == "application/json"
        ]
        records = [
            record
            for record in evidence
            if record.get("schema") == "literate-ai/retained-local-worker-evidence@1"
        ]
        self.assertEqual(len(records), 1)
        self.assertEqual(
            records[0]["plan_identity"], self.fixture.fixture.plan.identity.uri
        )
        self.assertEqual(len(records[0]["observations"]), 2)
        self.assertFalse(records[0]["grants_admission"])

    def test_secondary_policy_without_owner_refuses_before_platform_or_cache(self):
        f = self.fixture.fixture
        policy = RetainedSecondaryAccessPolicy(
            self.worker.identity,
            RetainedSecondaryTestIdentity("litaitest", 2001, 2001, (2001,)),
            (next(a.action_id for a in f.plan.profile.actions if a.stage == "test"),),
            (RetainedReadOnlyView("runtime", "inputs", "sdk"),),
            ("work",),
        )
        f.plan = replace(
            f.plan, profile=replace(f.plan.profile, secondary_access=policy)
        )
        f.current = f.plan
        with (
            patch(
                "literate_ai.adapters.retained_project_runtime.RetainedLocalPlatform"
            ) as platform,
            patch(
                "literate_ai.adapters.retained_project_runtime.bind_cache_directories"
            ) as cache,
            self.assertRaisesRegex(ValueError, "provisioning-owner-required"),
        ):
            self.run_project()
        platform.assert_not_called()
        cache.assert_not_called()

    def secondary_operator_context(self, *, overlap=False, enabled=True):
        """A real private inspector protocol fixture, not native allocation proof."""
        from literate_ai.adapters.retained_allocation import RESPONSE_SCHEMA
        from literate_ai.contracts import canonical_identity

        f = self.fixture.fixture
        policy = RetainedSecondaryAccessPolicy(
            self.worker.identity,
            RetainedSecondaryTestIdentity("litaitest", 2001, 2001, (2001,)),
            ("tests",),
            (RetainedReadOnlyView("runtime", "inputs", "tools"),),
            ("work",),
        )
        f.plan = replace(
            f.plan, profile=replace(f.plan.profile, secondary_access=policy)
        )
        f.current = f.plan
        private = f.base / "operator"
        private.mkdir(mode=0o700)
        prefix = f.source / "tools" if overlap else f.base / "allocated"
        if not overlap:
            prefix.mkdir(mode=0o700)
        inspector = private / "inspect.py"
        instance = canonical_identity("runtime inspector protocol fixture").uri
        inspector.write_text(
            "import json,sys,hashlib\nr=json.load(sys.stdin)\n"
            "d='sha256:'+hashlib.sha256(json.dumps(r,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()\n"
            f"print(json.dumps({{'schema':{RESPONSE_SCHEMA!r},'request_identity':d,"
            f"'allocation_identity':{instance!r},'workspace_prefix':{str(prefix)!r},"
            "'remaining_seconds':3600,'disposable':True}))\n"
        )
        configure(
            private / "worker-provisioner.json",
            WorkerProvisioner(
                ("never-allocate",),
                enabled=enabled,
                inspection_command=(sys.executable, str(inspector)),
            ),
        )
        return {
            **os.environ,
            "HOME": str(f.base),
            "LITAI_CONFIG_DIR": str(private),
        }, prefix

    def test_private_inspector_routes_runs_into_allocation_and_keeps_diagnostics(self):
        environment, prefix = self.secondary_operator_context()
        f = self.fixture.fixture
        workspace = Mock()
        workspace.plan = f.plan
        workspace.observations = ()
        workspace.secondary_access_evidence.return_value = ()

        def reserve(*args, **kwargs):
            allocation = kwargs["allocation_owner"]
            self.assertEqual(allocation.workspace_prefix, prefix)
            self.assertTrue(kwargs["parent"].is_relative_to(prefix))
            self.assertEqual(kwargs["parent"].stat().st_mode & 0o777, 0o700)
            self.assertNotIn("operator_environment", kwargs)
            workspace.root = kwargs["parent"] / "fixture-run"
            workspace.root.mkdir(mode=0o700)
            workspace._secondary_views.allocation = allocation
            return workspace

        admission = Mock()
        with (
            patch(
                "literate_ai.adapters.retained_project_runtime.RetainedProjectExecutionWorkspace",
                side_effect=reserve,
            ),
            patch(
                "literate_ai.adapters.retained_project_runtime.admit_retained_project",
                return_value=admission,
            ),
        ):
            returned, actual = self.run_project(operator_environment=environment)
        self.assertIs(returned, admission)
        self.assertIs(actual, workspace)
        self.assertEqual(workspace.execute_next.call_count, len(f.plan.profile.actions))
        workspace.close_secondary_access.assert_called_once()
        document = json.loads(
            (workspace.root / "command-diagnostics.json").read_bytes()
        )
        self.assertFalse(document["admission"])
        self.assertTrue(document["secondary_access"]["allocation_observations"])
        self.assertFalse((self.directories.obj_dir / "retained-project-runs").exists())

    def test_private_inspector_refusals_precede_platform_cache_and_session_creation(
        self,
    ):
        environment, prefix = self.secondary_operator_context(overlap=True)
        with (
            patch(
                "literate_ai.adapters.retained_project_runtime.RetainedLocalPlatform"
            ) as platform,
            patch(
                "literate_ai.adapters.retained_project_runtime.bind_cache_directories"
            ) as cache,
            self.assertRaisesRegex(
                ValueError, "allocated-parent-overlaps-or-redirects"
            ),
        ):
            self.run_project(operator_environment=environment)
        platform.assert_not_called()
        cache.assert_not_called()
        self.assertEqual(list(prefix.glob("retained-session-*")), [])

    def test_disabled_private_inspector_refuses_without_cache_effects(self):
        environment, prefix = self.secondary_operator_context(enabled=False)
        with (
            patch(
                "literate_ai.adapters.retained_project_runtime.RetainedLocalPlatform"
            ) as platform,
            patch(
                "literate_ai.adapters.retained_project_runtime.bind_cache_directories"
            ) as cache,
            self.assertRaises(ValueError),
        ):
            self.run_project(operator_environment=environment)
        platform.assert_not_called()
        cache.assert_not_called()
        self.assertEqual(list(prefix.iterdir()), [])

    def test_wrong_actual_os_refuses_before_cache_creation(self):
        from tests.critical.test_retained_project_platform import observation

        worker = replace(
            self.worker, requirements=ExecutionRequirements(os_family="freebsd")
        )
        f = self.fixture.fixture
        f.plan = replace(
            f.plan, profile=replace(f.plan.profile, worker_identity=worker.identity)
        )
        f.current = f.plan
        with patch(
            "literate_ai.adapters.retained_project_platform.probe_worker_capabilities",
            return_value=observation("linux"),
        ):
            with self.assertRaisesRegex(ValueError, "requirements-not-satisfied"):
                self.run_project(worker=worker)
        self.assertFalse(self.directories.obj_dir.exists())

    def test_selected_os_rejects_matching_worker_labels_on_wrong_host(self):
        from literate_ai.contracts.flavors import FlavorAxis
        from tests.critical.test_retained_project_platform import (
            observation,
            platform_lock,
        )

        f = self.fixture.fixture
        self.fixture.locks = (platform_lock(FlavorAxis.PLATFORM_OS, "freebsd"),)
        f.plan = replace(
            f.plan, component_locks=tuple(lock.identity for lock in self.fixture.locks)
        )
        f.current = f.plan
        with patch(
            "literate_ai.adapters.retained_project_platform.probe_worker_capabilities",
            return_value=observation("linux"),
        ):
            with self.assertRaisesRegex(ValueError, "selected-os-mismatch"):
                self.run_project()
        self.assertFalse(self.directories.obj_dir.exists())

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
                replace(m, role="sdk") if m.path == f.runner_path else m
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

    def call_handler(self, args, *, changed_after_first_read=False, command=None):
        """Only authority discovery is a fixture; target execution/admission is real.

        These handler-wiring checks do not qualify installed canonical-project
        discovery, Standard binding or complete public lifecycle packaging.
        """
        f = self.fixture.fixture
        project = SimpleNamespace(
            root=f.source,
            definition=SimpleNamespace(
                test_receipt_policy=f.configured_test_policy,
                lifecycle_driver=StandardProjectLifecycleDriver(
                    f.plan.profile.toolchain_identity, f.configured_test_policy.identity
                ),
            ),
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
            patch("literate_ai.cli.rebuild._project", return_value=project),
            patch(
                "literate_ai.cli.rebuild._resolve_standard_binding",
                return_value=object(),
            ),
        ):
            if command is not None:
                from literate_ai.cli.build_run import build_from_args, test_from_args

                handler = build_from_args if command == "build" else test_from_args
                return handler(args), calls
            return retained_project_from_args(project, object(), args), calls

    def public_arguments(self, command, *, plan=False, authorize=True, component=None):
        from literate_ai.cli.dispatch import _parser

        selected = self.handler_arguments()
        arguments = [
            command,
            "--project",
            str(self.fixture.fixture.source),
            "--retained-project",
            selected.retained_project,
            "--retained-project-profile",
            selected.retained_project_profile,
            "--build-dir",
            selected.build_dir,
            "--obj-dir",
            selected.obj_dir,
        ]
        if plan:
            arguments.append("--retained-project-plan")
        else:
            arguments.append("--allow-host-execution")
            if authorize:
                arguments.extend(
                    [
                        "--authorize-retained-project",
                        selected.authorize_retained_project,
                    ]
                )
        if component is not None:
            arguments.append(component)
        return _parser().parse_args(arguments)

    def test_public_build_and_test_plan_without_commands_or_custody(self):
        for command in ("build", "test"):
            with self.subTest(command=command):
                result, calls = self.call_handler(
                    self.public_arguments(command, plan=True), command=command
                )
                self.assertEqual(result["authority_mode"], "retained-project")
                self.assertFalse(result["admitted"])
                self.assertEqual(
                    result["authorization_identity"],
                    self.fixture.fixture.plan.identity.uri,
                )
                self.assertEqual(len(calls), 1)
                self.assertFalse(self.directories.obj_dir.exists())
                self.assertFalse(self.directories.build_dir.exists())

    def test_public_build_and_test_keep_live_retained_origin_and_exact_custody(self):
        for command in ("build", "test"):
            with self.subTest(command=command):
                result, calls = self.call_handler(
                    self.public_arguments(command), command=command
                )
                self.assertTrue(result["admitted"])
                self.assertGreater(len(calls), 4)
                self.assertFalse(result["conversion_advanced"])
                self.assertFalse(result["package_qualified"])
                self.assertFalse(result["release_authorized"])
                self.assertNotIn("run", result)
                self.assertNotIn("artifact", result)
                self.assertEqual(
                    result["custody_root"],
                    str(self.directories.build_dir / "retained-project-cas"),
                )

    def test_public_build_and_test_do_not_invent_authorization_or_partial_scope(self):
        for command in ("build", "test"):
            for component, authorize in ((None, False), ("components/partial", True)):
                with (
                    self.subTest(command=command, component=component),
                    self.assertRaises(CliFailure),
                ):
                    self.call_handler(
                        self.public_arguments(
                            command, component=component, authorize=authorize
                        ),
                        command=command,
                    )
                self.assertFalse(self.directories.obj_dir.exists())
                self.assertFalse(self.directories.build_dir.exists())

    def test_public_retained_profiles_refuse_ignored_generation_or_capacity_overrides(
        self,
    ):
        for command in ("build", "test"):
            for name, value in (
                ("flavor", ["+unlocked"]),
                ("model", "unselected-model"),
                ("jobs", 2),
                ("jobs", 0),
                ("source_cache_entry", ["sha256:" + "a" * 64]),
            ):
                with self.subTest(command=command, option=name):
                    args = self.public_arguments(command, plan=True)
                    setattr(args, name, value)
                    with self.assertRaises(CliFailure) as failure:
                        self.call_handler(args, command=command)
                    self.assertIn("locked profile", str(failure.exception))
                    self.assertFalse(self.directories.obj_dir.exists())

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
