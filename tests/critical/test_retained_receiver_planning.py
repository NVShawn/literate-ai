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
import os
import secrets
import sys
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai import action_worker
from literate_ai.adapters.action_capabilities import (
    CAPABILITY_REQUEST_SCHEMA,
    decode_capability_response,
    receiver_code_identity,
)
from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    ActionWireError,
)
from literate_ai.adapters.retained_project_inputs import (
    discover_retained_project_inputs,
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
            workspace="~/litai-receiver-planning",
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
