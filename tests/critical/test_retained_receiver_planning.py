"""Real retained-file replanning at receiver publication; test trust is explicit.

Only the external Standard distribution binding is a test authority.
Project validation and original adoption use their actual public owners.
Catalog locks, retained source/profile/SDK-policy reads, public plan parsing and
capability publication are real. No native execution or SDK admission is claimed.
"""

import copy
import hashlib
import io
import json
import multiprocessing
import os
import pickle
import secrets
import sys
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai import action_worker
from literate_ai import retained_package_receiver as receiver
from literate_ai.adapters.action_capabilities import (
    CAPABILITY_REQUEST_SCHEMA,
    decode_capability_response,
    receiver_code_identity,
)
from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    ActionWireError,
    decode_attested_action_response,
    encode_action_request,
    record_identity,
)
from literate_ai.adapters.action_retained_unit_package_worker import (
    ConfiguredRetainedUnitPackageWorker,
    retained_unit_package_action,
)
from literate_ai.adapters.component_lock_application import (
    current_project_component_locks,
)
from literate_ai.adapters.retained_package_capability import (
    RegisteredRetainedPackageCapabilityProbe,
)
from literate_ai.adapters.retained_project_inputs import (
    discover_retained_project_inputs,
)
from literate_ai.adapters.retained_project_platform import (
    RetainedReceiverPlatform,
    _compose_receiver_current_plan,
    _receiver_platform_current_plan,
)
from literate_ai.application.action_dag_scheduler import (
    LifecycleActionDispatchRequest,
    LifecycleActionWorker,
)
from literate_ai.cli.retained_unit_execution import (
    _CANONICAL_AUTHORITY_PLAN,
    _CanonicalRetainedAuthorityPlan,
)
from literate_ai.contracts import (
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.contracts.authoring_markdown import (
    parse_authoring_markdown,
    render_authoring_markdown,
)
from literate_ai.contracts.execution_dispatch import (
    ExecutionWorker,
    ExecutionWorkerCatalog,
    ExecutionWorkerKind,
)
from literate_ai.contracts.projects import StandardProjectLifecycleDriver
from literate_ai.contracts.retained_project import RetainedProjectLimits
from literate_ai.contracts.retained_project_sdks import (
    RetainedProjectSdkSelectionPolicy,
)
from literate_ai.contracts.retained_project_tests import (
    RetainedProjectExpectedCase,
    RetainedProjectTestPolicy,
)
from literate_ai.contracts.retained_unit_policy import RetainedUnitPackagePolicy
from literate_ai.contracts.retained_unit_requests import RetainedUnitPackageRequest
from literate_ai.contracts.retained_unit_results import RetainedUnitPackageSetResult
from literate_ai.retained_package_receiver import (
    CONFIG_SCHEMA,
    VERIFICATION_CONFIG_SCHEMA,
    _configured_worker,
    _Registration,
)
from literate_ai.storage.cas import FileSystemCAS
from tests.support.fixtures_current_component_locks import (
    initialize_current_component_lock_fixture,
)
from tests.support.fixtures_retained_project_sbom import retained_project_sbom_fixture
from tests.support.fixtures_test_cli_component_locks import _run


class RetainedReceiverPlanningTests(unittest.TestCase):
    def setUp(self):
        initialize_current_component_lock_fixture(self, canonical_project=True)
        self.project = replace(
            self.project,
            definition=replace(
                self.project.definition,
                lifecycle_driver=StandardProjectLifecycleDriver(
                    canonical_identity("test Standard policy"),
                    self.project.definition.test_receipt_policy.identity,
                ),
            ),
        )
        (self.root / "literate.project.json").write_bytes(
            canonical_json_bytes(self.project.definition.to_dict())
        )
        component = self.component
        authoring, body = parse_authoring_markdown(
            (component / "component.md").read_bytes(),
            source=(component / "component.md").as_posix(),
        )
        request = RetainedUnitPackageRequest(
            "application",
            (),
            True,
            "specs/LICENSE",
            "application",
            "1.0",
            "misc/application",
            "/usr/local",
            (),
        )
        (component / "specs/unit.package-unit.json").write_bytes(
            canonical_json_bytes(request.to_dict())
        )
        (component / "specs/LICENSE").write_text("Fixture license.\n")
        authoring["specification_roots"].append("specs/unit.package-unit.json")
        (component / "component.md").write_bytes(
            render_authoring_markdown(authoring, body)
        )
        status, record, error = _run(
            [
                "lock",
                str(component),
                "--target",
                "host",
                "--flavor",
                "+python",
                "--flavor",
                "+macos",
                "--json",
            ]
        )
        self.assertEqual(status, 0, (record, error))
        self.worker = ExecutionWorker(
            "receiver",
            ExecutionWorkerKind.SSH,
            endpoint="fixture@unused.invalid",
            workspace="~/retained-receiver-planning",
        )
        self.catalog = self.root.parent / (self.root.name + "-workers.json")
        self.addCleanup(self.catalog.unlink, missing_ok=True)
        self.catalog.write_bytes(
            canonical_json_bytes(ExecutionWorkerCatalog((self.worker,)).to_dict())
        )
        for name in ("original", "sdk", "build", "tests", "manual"):
            (self.root / name).mkdir(exist_ok=True)
        (self.root / "original/asset.bin").write_bytes(b"original\x00source")
        tool = self.root / "sdk/tool"
        tool.write_bytes(b"#!/bin/sh\nexit 0\n")
        tool.chmod(0o755)
        (self.root / "tests/case.txt").write_text("original test")
        (self.root / "manual/manual.txt").write_text("original documentation")
        self.profile_path = self.root / "build/profile.json"
        self.profile_path.write_text("{}")
        (self.root / "literate.release.json").write_bytes(
            (Path(__file__).resolve().parents[2] / "literate.release.json").read_bytes()
        )
        tests = RetainedProjectTestPolicy(
            self.project.definition.test_receipt_policy.identity,
            (RetainedProjectExpectedCase("case", "test"),),
            (("test", "test-results/report.json"),),
        )
        test_bytes = canonical_json_bytes(tests.to_dict())
        (self.root / "test-policy.json").write_bytes(test_bytes)
        (self.root / "sdk-policy.json").write_bytes(
            canonical_json_bytes(RetainedProjectSdkSelectionPolicy(()).to_dict())
        )
        roots = {
            "original": "source",
            "sdk": "toolchain",
            "build": "build-definition",
            "tests": "test-definition",
            "manual": "docs-definition",
            "greeting/specs": "specification",
            "test-policy.json": "test-policy",
            "sdk-policy.json": "sdk-policy",
            "literate.release.json": "release-policy",
        }
        limits = RetainedProjectLimits(100, 65536, 1024**2)
        initial = discover_retained_project_inputs(
            self.root, roots, limits, executable_paths=("sdk/tool",)
        )
        template, _ = retained_project_sbom_fixture()
        profile = replace(
            template.profile,
            worker_identity=self.worker.identity,
            toolchain_identity=initial.role_identity({"sdk", "toolchain"}),
            dependency_identity=initial.role_identity({"dependency"}),
            test_policy_identity=canonical_identity(tests.to_dict()),
            release_policy_identity=ContentIdentity.parse_uri(
                "sha256:"
                + hashlib.sha256(
                    (self.root / "literate.release.json").read_bytes()
                ).hexdigest()
            ),
            tools=(
                (
                    "tool",
                    "sdk/tool",
                    ContentIdentity.parse_uri(
                        "sha256:" + hashlib.sha256(tool.read_bytes()).hexdigest()
                    ),
                ),
            ),
            actions=tuple(
                replace(a, outputs=("test-results",)) if a.stage == "test" else a
                for a in template.profile.actions
            ),
        )
        self.profile_path.write_bytes(canonical_json_bytes(profile.to_dict()))
        manifest = discover_retained_project_inputs(
            self.root, roots, limits, executable_paths=("sdk/tool",)
        )
        (self.root / "manifest.json").write_bytes(
            canonical_json_bytes(manifest.to_dict())
        )
        policy = RetainedUnitPackagePolicy(
            limits,
            limits,
            4,
            8,
            1024**2,
            16 * 1024**2,
            16 * 1024**2,
            8 * 1024**2,
            16 * 1024**2,
        )
        (self.root / "unit-policy.json").write_bytes(
            canonical_json_bytes(policy.to_dict())
        )
        tools = {
            "schema": "literate-ai/retained-unit-native-tool-observations@1",
            "pkg": {
                "schema": "literate-ai/freebsd-pkg-tool@1",
                "executable_sha256": "1" * 64,
                "version": "2.0",
                "abi": "FreeBSD:16:amd64",
                "architecture": "freebsd:16:x86:64",
                "configuration": "empty",
                "plugins": False,
            },
            "tar": {
                "schema": "literate-ai/native-libarchive-tar-tool@1",
                "executable_sha256": "2" * 64,
                "version": "bsdtar 3.7.0 - libarchive 3.7.0",
            },
        }
        (self.root / "tools.json").write_bytes(canonical_json_bytes(tools))
        self.build, self.obj = (
            self.root / "private-build",
            self.root / "private-objects",
        )
        self.build.mkdir()
        self.obj.mkdir()
        self.store = FileSystemCAS(self.build / "retained-project-cas")
        self.config_path = self.root.parent / (self.root.name + "-receiver.json")
        self.addCleanup(self.config_path.unlink, missing_ok=True)
        self.registration = {
            "schema": CONFIG_SCHEMA,
            "project": str(self.root),
            "worker_config": str(self.catalog),
            "worker": self.worker.worker_id,
            "target": "host",
            "retained_project": "manifest.json",
            "retained_project_profile": "build/profile.json",
            "policy": "unit-policy.json",
            "tools": "tools.json",
            "authorization_identity": canonical_identity("unbound preview").uri,
            "build_dir": str(self.build),
            "obj_dir": str(self.obj),
            "child_environment": {},
        }
        self.config_path.write_bytes(canonical_json_bytes(self.registration))
        binding = SimpleNamespace(
            require_unchanged=lambda: None,
            trust_binding=SimpleNamespace(to_dict=lambda: {"test-authority": True}),
        )
        self.enterContext(
            patch(
                "literate_ai.cli.rebuild._resolve_standard_binding",
                return_value=binding,
            )
        )
        status, document, error = _run(
            ["project", "documentation-review", str(self.root), "--record", "--json"]
        )
        self.assertEqual(status, 0, (document, error))
        from tests.support.fixtures_test_repository_orchestration import git

        git(self.root, "init", "-q")
        git(self.root, "add", "--all")
        git(
            self.root,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.test",
            "commit",
            "--quiet",
            "-m",
            "Original planning fixture",
        )
        status, document, error = _run(
            ["project", "adopt-original", "--project", str(self.root), "--json"]
        )
        self.assertEqual(status, 0, (document, error))
        status, document, error = _run(
            [
                "project",
                "adopt-original",
                "--project",
                str(self.root),
                "--apply",
                "--acknowledge",
                "--expected-plan-identity",
                document["result"]["plan_identity"],
                "--json",
            ]
        )
        self.assertEqual(status, 0, (document, error))

    def public_plan(self, operation="execution-plan", extra=()):
        status, document, error = _run(
            [
                "package",
                "retained-units",
                operation,
                "--project",
                str(self.root),
                "--retained-project",
                "manifest.json",
                "--retained-project-profile",
                "build/profile.json",
                "--policy",
                "unit-policy.json",
                "--tools",
                "tools.json",
                "--worker",
                "receiver",
                "--worker-config",
                str(self.catalog),
                *extra,
                "--json",
            ]
        )
        self.assertEqual(status, 0, (document, error))
        return document["result"]

    def configured(self, preview, verification=None):
        value = copy.deepcopy(self.registration)
        value["authorization_identity"] = preview["authorization_identity"]
        if verification is not None:
            value["schema"] = VERIFICATION_CONFIG_SCHEMA
            value["verification"] = {"selection": verification, "source": None}
        self.config_path.write_bytes(canonical_json_bytes(value))
        return _configured_worker(_Registration(self.config_path))

    def describe(self, worker):
        deadline = ActionDispatchDeadline(datetime.now(UTC) + timedelta(seconds=30))
        content = canonical_json_bytes(
            {
                "schema": CAPABILITY_REQUEST_SCHEMA,
                "worker_identity": self.worker.identity.uri,
                "nonce": secrets.token_hex(16),
                "deadline": deadline.to_dict(),
            }
        )
        incoming = io.TextIOWrapper(io.BytesIO(content), encoding="utf-8")
        outgoing_bytes = io.BytesIO()
        outgoing = io.TextIOWrapper(outgoing_bytes, encoding="utf-8")
        with (
            patch.object(sys, "stdin", incoming),
            patch.object(sys, "stdout", outgoing),
            patch.object(sys, "stderr", io.StringIO()),
            patch.dict(
                os.environ, {"LITAI_ACTION_WORKER_IDENTITY": self.worker.identity.uri}
            ),
        ):
            status = action_worker.main(
                [
                    "--describe",
                    "--cas",
                    str(self.store.root),
                    "--workspace",
                    str(self.obj),
                ],
                package_worker=worker,
            )
        outgoing.flush()
        return status, outgoing_bytes.getvalue(), content

    def require_capability(self, worker):
        status, raw, challenge = self.describe(worker)
        self.assertEqual(status, 0, raw)
        return decode_capability_response(raw, challenge, receiver_code_identity())

    def require_publication_refusal(self, worker):
        status, raw, challenge = self.describe(worker)
        self.assertEqual(status, 2, raw)
        self.assertEqual(
            json.loads(raw)["schema"], "literate-ai/action-observation-failure@1"
        )
        with self.assertRaises(ActionWireError):
            decode_capability_response(raw, challenge, receiver_code_identity())

    def execute_guard_request(self, worker, *, expect_unattested_refusal=False):
        """Use the real public wire/receiver guards, not native execution proof."""
        observed = self.require_capability(worker)
        deadline = ActionDispatchDeadline(datetime.now(UTC) + timedelta(seconds=120))
        payload = record_identity(worker.execution_request_bytes)
        request = LifecycleActionDispatchRequest(
            canonical_identity("actual retained planning guard fixture"),
            retained_unit_package_action(
                worker.retained_plan_identity, worker.worker.worker_id, payload
            ),
            LifecycleActionWorker(
                worker.worker.worker_id,
                worker.worker.identity,
                worker.catalog_identity,
                observed.identity,
            ),
            0,
            (),
            deadline.identity,
            (payload,),
        )
        content = encode_action_request(
            request,
            deadline,
            {payload: worker.execution_request_bytes},
            capability=observed.capability_identity,
        )
        incoming = io.TextIOWrapper(io.BytesIO(content), encoding="utf-8")
        output_bytes = io.BytesIO()
        outgoing = io.TextIOWrapper(output_bytes, encoding="utf-8")
        with (
            patch.object(sys, "stdin", incoming),
            patch.object(sys, "stdout", outgoing),
            patch.object(sys, "stderr", io.StringIO()),
            patch.dict(
                os.environ, {"LITAI_ACTION_WORKER_IDENTITY": self.worker.identity.uri}
            ),
        ):
            status = action_worker.main(
                ["--cas", str(self.store.root), "--workspace", str(self.obj)],
                package_worker=worker,
            )
        outgoing.flush()
        self.assertEqual(status, 0, output_bytes.getvalue())
        outcome, record, attested = decode_attested_action_response(
            output_bytes.getvalue(), request
        )
        self.assertEqual(outcome.failure_code, "action_source.custody_unavailable")
        self.assertIsNone(outcome.result_identity)
        self.assertIsNone(record)
        if expect_unattested_refusal:
            # Persistent source drift prevents the closing fresh capability
            # measurement from attesting even this refusal. Require the exact
            # correlated failure wire; unsigned success is never acceptable.
            self.assertIsNone(attested)
            self.assertEqual(
                json.loads(output_bytes.getvalue()),
                {
                    "schema": "literate-ai/lifecycle-action-wire-response@1",
                    "request_identity": request.identity.uri,
                    "worker_identity": request.worker.identity.uri,
                    "result_identity": None,
                    "result_record": None,
                    "failure_code": "action_source.custody_unavailable",
                },
            )
        else:
            self.assertEqual(attested, observed.capability_identity)

    def test_outer_cli_guard_reopens_source_once_and_preserves_custom_measurements(
        self,
    ):
        # This portable public-wire control stops before workspace creation.
        # Native OS/tool observations are the explicit external test boundary;
        # no FreeBSD tool execution, SDK admission or PACKAGE success is claimed.
        for measurement in ("canonical", "custom"):
            for changed in (False, True):
                with self.subTest(measurement=measurement, changed=changed):
                    self._assert_outer_cli_guard_control(measurement, changed)

    def _assert_outer_cli_guard_control(self, measurement, changed):
        from literate_ai.cli import retained_unit_execution as unit_execution

        worker = self.configured(self.public_plan())
        path = self.root / "original/asset.bin"
        original = path.read_bytes()
        source_stat = path.stat()
        source_inode = (source_stat.st_dev, source_stat.st_ino)
        armed = []
        reads = []
        boundaries = []
        mutations = []
        measurements = []
        actual_open = os.open
        tools = json.loads((self.root / "tools.json").read_bytes())

        def observed_open(*args, **kwargs):
            descriptor = actual_open(*args, **kwargs)
            # Observe genuine external file opens by the actual
            # inode, independent of planner/helper implementation.
            if armed:
                observed = os.fstat(descriptor)
                if (observed.st_dev, observed.st_ino) == source_inode:
                    reads.append("original source opened")
            return descriptor

        def native_observations(*_args):
            boundaries.append("native tool observations")
            armed.append(True)
            if changed and measurement == "canonical":
                path.write_bytes(original + b"changed before fresh guard")
            return tools

        def before_workspace(*_args, **_kwargs):
            boundaries.append("before workspace creation")
            raise RuntimeError("portable outer CLI guard control ended")

        def run(context):
            args = copy.copy(_Registration(self.config_path).args)
            args.retained_unit_command = "execute"
            args.authorize_retained_project = worker.retained_plan_identity.uri
            args.authorize_unit_packages = worker.execution_authorization.uri
            args.allow_host_execution = True
            with (
                patch.object(
                    unit_execution,
                    "platform",
                    SimpleNamespace(system=lambda: "FreeBSD"),
                ),
                patch.object(
                    unit_execution.FreeBsdPkgToolBinding,
                    "discover",
                    return_value=object(),
                ),
                patch.object(
                    unit_execution.NativeTarToolBinding,
                    "discover",
                    return_value=object(),
                ),
                patch.object(
                    unit_execution,
                    "retained_unit_tool_observations",
                    side_effect=native_observations,
                ),
                patch.object(
                    unit_execution,
                    "select_cache_directories",
                    side_effect=before_workspace,
                ),
                patch.object(os, "open", side_effect=observed_open),
            ):
                unit_execution.execute_retained_unit_package_from_args(
                    args, receiver_context=context
                )
            self.fail("outer CLI guard unexpectedly returned a package")

        class CustomMeasurementWorker(ConfiguredRetainedUnitPackageWorker):
            def execute(inner, *args, **kwargs):
                actual_measure = kwargs["measure_capability"]

                def measure():
                    observed = actual_measure()
                    if armed:
                        measurements.append("actual measurement completed")
                    if armed and changed and len(measurements) == 2:
                        # The constructor and CLI bookend have each run
                        # the custom registration guard and actual measurement.
                        # Change bytes after the second measurement, so the
                        # independent authority fallback must reopen/refuse.
                        path.write_bytes(original + b"changed after measure")
                        mutations.append("changed after fresh measurement")
                    return observed

                kwargs["measure_capability"] = measure
                return super().execute(*args, **kwargs)

        selected_worker = (
            replace(worker, run_retained=run)
            if measurement == "canonical"
            else CustomMeasurementWorker(
                worker.worker,
                worker.catalog_identity,
                worker.retained_plan_identity,
                worker.registration_identity,
                worker.execution_authorization,
                worker.execution_request_bytes,
                run,
                worker.require_current,
                poll_guard=worker.poll_guard,
                verification_selection=worker.verification_selection,
            )
        )
        try:
            self.execute_guard_request(
                selected_worker, expect_unattested_refusal=changed
            )
            self.assertEqual(
                boundaries,
                ["native tool observations"]
                + ([] if changed else ["before workspace creation"]),
            )
            # Intact source proves the scan budget using real inode opens:
            # two canonical scans; the unchanged custom route has two full
            # registration guards, two measurements and an authority fallback.
            # A changed source may be refused by its metadata before opening it;
            # require the same budget, not completion of a refused scan.
            read_budget = 2 if measurement == "canonical" else 5
            if changed:
                self.assertLessEqual(len(reads), read_budget)
                if measurement == "custom":
                    self.assertGreaterEqual(len(reads), 4)
            else:
                self.assertEqual(len(reads), read_budget)
            self.assertEqual(len(measurements), 2 if measurement == "custom" else 0)
            self.assertEqual(
                mutations,
                ["changed after fresh measurement"]
                if changed and measurement == "custom"
                else [],
            )
        finally:
            path.write_bytes(original)
        armed.clear()
        self.require_capability(worker)

    def test_actual_transient_changes_refuse_inside_fresh_receiver_preparation(self):
        for path in (
            self.root / "original/asset.bin",
            self.profile_path,
            self.catalog,
            self.root / "sdk-policy.json",
            self.config_path,
        ):
            with self.subTest(input=path.name):
                preview = self.public_plan()
                worker = self.configured(preview)
                original = path.read_bytes()
                changed = original + (b"changed" if path.suffix == ".bin" else b" ")
                if path == self.catalog:
                    value = json.loads(original)
                    value["workers"][0]["endpoint"] = "fixture@changed.invalid"
                    changed = canonical_json_bytes(value)
                reached = []
                original_prepare = receiver._prepare_retained_unit_execution
                original_registered = _Registration.require_unchanged

                def prepare_changed(
                    *args,
                    path=path,
                    changed=changed,
                    reached=reached,
                    original_prepare=original_prepare,
                    original=original,
                    **kwargs,
                ):
                    path.write_bytes(changed)
                    reached.append("changed observed by actual authority guard")
                    try:
                        return original_prepare(*args, **kwargs)
                    finally:
                        path.write_bytes(original)

                def registration_changed(
                    config,
                    path=path,
                    changed=changed,
                    reached=reached,
                    original_registered=original_registered,
                    original=original,
                ):
                    path.write_bytes(changed)
                    reached.append("changed observed by actual authority guard")
                    try:
                        return original_registered(config)
                    finally:
                        path.write_bytes(original)

                def run(
                    context,
                    path=path,
                    original_prepare=original_prepare,
                    registration_changed=registration_changed,
                    prepare_changed=prepare_changed,
                    reached=reached,
                ):
                    replanner = _compose_receiver_current_plan(
                        context,
                        original_prepare(
                            _Registration(self.config_path).args
                        ).planning.plan,
                        lambda: (
                            original_prepare(
                                _Registration(self.config_path).args
                            ).planning.plan
                        ),
                    )
                    mutation = (
                        patch.object(
                            _Registration,
                            "require_unchanged",
                            registration_changed,
                        )
                        if path == self.config_path
                        else patch.object(
                            receiver,
                            "_prepare_retained_unit_execution",
                            side_effect=prepare_changed,
                        )
                    )
                    with mutation:
                        replanner()
                    reached.append("unsafe result after transient source change")
                    raise RuntimeError("unreachable successful guard")

                self.execute_guard_request(replace(worker, run_retained=run))
                self.assertEqual(
                    reached, ["changed observed by actual authority guard"]
                )
                self.assertEqual(path.read_bytes(), original)
                self.require_capability(worker)

    def test_custom_measurements_keep_independent_source_replanning(self):
        for mode in ("cached", "subclass"):
            with self.subTest(measurement=mode):
                worker = self.configured(self.public_plan())
                path = self.root / "original/asset.bin"
                original = path.read_bytes()
                armed = []
                reached = []

                class CustomMeasurementWorker(ConfiguredRetainedUnitPackageWorker):
                    def execute(
                        inner,
                        *args,
                        mode=mode,
                        armed=armed,
                        path=path,
                        original=original,
                        reached=reached,
                        **kwargs,
                    ):
                        original_measure = kwargs["measure_capability"]

                        def measure():
                            observed = original_measure()
                            if armed:
                                path.write_bytes(original + b"transient")
                                reached.append("changed after canonical measurement")
                            return observed

                        class CustomProbe(RegisteredRetainedPackageCapabilityProbe):
                            def __init__(inner):
                                pass

                            def __call__(inner):
                                return measure()

                        kwargs["measure_capability"] = (
                            CustomProbe() if mode == "subclass" else measure
                        )
                        return super().execute(*args, **kwargs)

                def run(
                    context,
                    armed=armed,
                    reached=reached,
                    path=path,
                    original=original,
                ):
                    config = _Registration(self.config_path)
                    plan = receiver._prepare_retained_unit_execution(
                        config.args
                    ).planning.plan
                    replanner = _compose_receiver_current_plan(
                        context,
                        plan,
                        lambda: (
                            receiver._prepare_retained_unit_execution(
                                config.args
                            ).planning.plan
                        ),
                    )
                    armed.append(True)
                    try:
                        replanner()
                        reached.append("unsafe result without independent replanning")
                    finally:
                        path.write_bytes(original)
                    raise RuntimeError("unreachable successful guard")

                custom = CustomMeasurementWorker(
                    worker.worker,
                    worker.catalog_identity,
                    worker.retained_plan_identity,
                    worker.registration_identity,
                    worker.execution_authorization,
                    worker.execution_request_bytes,
                    run,
                    worker.require_current,
                    poll_guard=worker.poll_guard,
                    verification_selection=worker.verification_selection,
                )
                self.execute_guard_request(custom)
                self.assertEqual(reached, ["changed after canonical measurement"])
                self.assertEqual(path.read_bytes(), original)
                armed.clear()
                self.require_capability(worker)

    def test_custom_authority_refusal_survives_canonical_measurement(self):
        worker = self.configured(self.public_plan())
        reached = []

        def run(context):
            prepared = receiver._prepare_retained_unit_execution(
                _Registration(self.config_path).args
            )

            def refusing_authority():
                # This independent observer can impose assertions beyond the
                # canonical plan. Its refusal must reach the actual public wire.
                reached.append("independent authority observed")
                raise ValueError("retained.unit.independent-authority-refused")

            replanner = _compose_receiver_current_plan(
                context, prepared.planning.plan, refusing_authority
            )
            replanner()
            reached.append("unsafe authority callback bypass")

        self.execute_guard_request(replace(worker, run_retained=run))
        self.assertEqual(reached, ["independent authority observed"])
        self.require_capability(worker)

    def test_different_authority_parameters_keep_independent_selection_check(self):
        worker = self.configured(self.public_plan())
        reached = []

        def run(context):
            prepared = receiver._prepare_retained_unit_execution(
                _Registration(self.config_path).args
            )
            # Worker, catalog and plan stay identical. The original CLI
            # selection carries a different parameter, so canonical reuse must
            # fall back to its real authority/selection check and refuse.
            differing = replace(
                prepared,
                selected=replace(
                    prepared.selected, parameters=(("independent_parameter", "1"),)
                ),
            )
            authority = _CanonicalRetainedAuthorityPlan(
                differing, _seal=_CANONICAL_AUTHORITY_PLAN
            )
            replanner = _compose_receiver_current_plan(
                context, prepared.planning.plan, authority
            )
            reached.append("independent parameter selection check")
            replanner()
            reached.append("unsafe parameter mismatch bypass")

        self.execute_guard_request(replace(worker, run_retained=run))
        self.assertEqual(reached, ["independent parameter selection check"])
        self.require_capability(worker)

    def test_original_plan_callbacks_refuse_copy_and_foreign_process(self):
        worker = self.configured(self.public_plan())
        with self.assertRaises(TypeError):
            copy.copy(worker.require_current)
        with self.assertRaises(TypeError):
            pickle.dumps(worker.require_current)
        reached = []

        def run(context):
            probe = context._measure_capability
            with self.assertRaises(TypeError):
                copy.copy(probe)
            with self.assertRaises(TypeError):
                pickle.dumps(probe)
            reached.append("transport refused")
            if hasattr(os, "fork"):
                fork = multiprocessing.get_context("fork")
                parent, child = fork.Pipe(duplex=False)

                def foreign_process():
                    refused = 0
                    for callback in (worker.require_current, probe):
                        try:
                            callback()
                        except ValueError:
                            refused += 1
                    child.send(refused)
                    child.close()

                process = fork.Process(target=foreign_process)
                process.start()
                child.close()
                try:
                    self.assertTrue(parent.poll(10), "foreign-process check timed out")
                    self.assertEqual(parent.recv(), 2)
                    process.join(10)
                    self.assertEqual(process.exitcode, 0)
                finally:
                    if process.is_alive():
                        process.terminate()
                        process.join(10)
                    parent.close()
            # This portable control deliberately produces no PACKAGE result.
            raise RuntimeError("original-process lifetime control completed")

        self.execute_guard_request(replace(worker, run_retained=run))
        self.assertEqual(reached, ["transport refused"])
        self.require_capability(worker)

    @unittest.skipUnless(sys.platform == "darwin", "fixture selects actual macOS host")
    def test_public_receiver_plan_composes_with_actual_host_and_fresh_source(self):
        worker = self.configured(self.public_plan())
        path = self.root / "original/asset.bin"
        original = path.read_bytes()
        reached = []

        def run(context):
            config = _Registration(self.config_path)
            prepared = receiver._prepare_retained_unit_execution(config.args)
            plan = prepared.planning.plan
            replanner = _compose_receiver_current_plan(
                context,
                plan,
                _CanonicalRetainedAuthorityPlan(
                    prepared, _seal=_CANONICAL_AUTHORITY_PLAN
                ),
            )
            platform = RetainedReceiverPlatform(
                context,
                current_project_component_locks(prepared.context[0]),
                plan,
            )
            current = _receiver_platform_current_plan(platform, replanner)
            self.assertEqual(current(), plan)
            with self.assertRaises(TypeError):
                copy.copy(current)
            try:
                path.write_bytes(original + b"changed")
                with self.assertRaises(ValueError):
                    current()
            finally:
                path.write_bytes(original)
            self.assertEqual(current(), plan)
            reached.append("actual host plan and source-change refusal")
            raise RuntimeError("portable physical-host guard control completed")

        self.execute_guard_request(replace(worker, run_retained=run))
        self.assertEqual(reached, ["actual host plan and source-change refusal"])
        self.require_capability(worker)

    def test_public_execution_and_verification_plans_publish_same_current_intent(self):
        first = self.public_plan()
        self.assertEqual(first, self.public_plan())
        worker = self.configured(first)
        self.assertEqual(
            self.require_capability(worker).worker_identity, self.worker.identity
        )
        # A bounded root assertion is sufficient for read-only verify-plan;
        # it is not a produced archive, receipt or admitted verification result.
        shard = self.store.put_manifest(
            {"unadmitted-test-assertion": True}, media_type="application/json"
        )
        index = RetainedUnitPackageSetResult(
            *(canonical_identity(n) for n in range(4)),
            (shard,),
            1,
            1,
            8 * 1024**2,
            512 * 1024**2,
            2 * 1024**2,
            1024**3,
        )
        reference = self.store.put_manifest(
            index.to_dict(), media_type="application/json"
        )
        verify = self.public_plan(
            "verify-plan",
            ("--custody-root", str(self.store.root), "--result", reference.identity),
        )
        self.assertEqual(verify["request"]["operation"], "verify")
        self.assertFalse(verify["executed"])
        selection = {
            k: verify["request"][k]
            for k in ("verification_mode", "result_index", "source_custody_identity")
        }
        self.assertEqual(
            self.require_capability(self.configured(verify, selection)).worker_identity,
            self.worker.identity,
        )
        self.assertEqual(
            verify,
            self.public_plan(
                "verify-plan",
                (
                    "--custody-root",
                    str(self.store.root),
                    "--result",
                    reference.identity,
                ),
            ),
        )

    def test_actual_input_mutations_refuse_before_and_after_capability_encoding(self):
        paths = (
            self.root / "original/asset.bin",
            self.profile_path,
            self.catalog,
            self.root / "sdk-policy.json",
            self.config_path,
        )
        for during_encoding in (False, True):
            for path in paths:
                with self.subTest(input=path.name, during_encoding=during_encoding):
                    worker = self.configured(self.public_plan())
                    original = path.read_bytes()
                    changed = original + (b"changed" if path.suffix == ".bin" else b" ")
                    if path == self.catalog:
                        value = json.loads(original)
                        value["workers"][0]["endpoint"] = "fixture@changed.invalid"
                        changed = canonical_json_bytes(value)
                    encoder = action_worker.encode_capability_response

                    def encode_then_change(
                        *args, encoder=encoder, path=path, changed=changed, **kwargs
                    ):
                        result = encoder(*args, **kwargs)
                        path.write_bytes(changed)
                        return result

                    try:
                        if during_encoding:
                            with patch.object(
                                action_worker,
                                "encode_capability_response",
                                side_effect=encode_then_change,
                            ):
                                self.require_publication_refusal(worker)
                        else:
                            # Reusing a successfully published worker must still
                            # reopen actual authority on the next publication.
                            self.require_capability(worker)
                            path.write_bytes(changed)
                            self.require_publication_refusal(worker)
                    finally:
                        path.write_bytes(original)
