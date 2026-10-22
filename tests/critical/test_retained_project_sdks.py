"""Real SDK products, captured retained commands and live revocation boundaries."""

import hashlib
import json
import os
import shutil
import unittest
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from literate_ai.adapters import retained_project_execution as execution
from literate_ai.adapters.native_sdk_consumer import NativeSdkConsumerInputs
from literate_ai.adapters.retained_project_acceptance import ACCEPTANCE_POLICY_PATH
from literate_ai.adapters.retained_project_inputs import (
    discover_retained_project_inputs,
)
from literate_ai.contracts import (
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.contracts.retained_project_acceptance import (
    RetainedProjectAcceptancePolicy,
)
from literate_ai.contracts.retained_project_lifecycle import RetainedProjectAction
from literate_ai.contracts.retained_project_sbom import (
    project_retained_project_managed_graph,
)
from literate_ai.contracts.retained_project_sdks import (
    RetainedProjectSdkBinding,
    RetainedProjectSdkPolicy,
)
from literate_ai.security import AuthorizationError, BuildAuthorization, SecurityProfile
from tests.critical import test_retained_project_completion as completion
from tests.critical import test_retained_project_execution as commands
from tests.support import fixtures_test_native_sdk_source_build as producer_fixtures
from tests.support.fixtures_test_retained_filesystem import select_filesystem_case


class RetainedSdkPolicyTests(unittest.TestCase):
    def test_policy_preserves_exact_lock_product_pairs_and_nonoverlapping_prefixes(
        self,
    ):
        lock = canonical_identity("lock")
        product = canonical_identity("product")
        binding = RetainedProjectSdkBinding(lock, product, "vendor/math")
        policy = RetainedProjectSdkPolicy((binding,))
        self.assertEqual(RetainedProjectSdkPolicy.from_dict(policy.to_dict()), policy)
        policy.require_locks((lock,))
        with self.assertRaisesRegex(ValueError, "foreign-component-lock"):
            policy.require_locks((product,))
        with self.assertRaises(ValueError):
            RetainedProjectSdkPolicy((binding, binding))
        other = replace(
            binding,
            input_identity=canonical_identity("other"),
            prefix="vendor/math/child",
        )
        ordered = tuple(
            sorted(
                (binding, other),
                key=lambda v: (v.component_lock_identity.uri, v.input_identity.uri),
            )
        )
        with self.assertRaises(ValueError):
            RetainedProjectSdkPolicy(ordered)
        wire = policy.to_dict()
        wire["accepted"] = True
        with self.assertRaises(ValueError):
            RetainedProjectSdkPolicy.from_dict(wire)


@unittest.skipUnless(
    os.name != "nt" and shutil.which("cc") and shutil.which("cmake"),
    "requires native POSIX CMake fixture",
)
class RetainedProjectSdkExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        commands.RetainedProjectExecutionTests.setUpClass()
        cls.addClassCleanup(commands.RetainedProjectExecutionTests.doClassCleanups)

    def setUp(self):
        self.completed = completion.RetainedProjectCompletionTests()
        self.completed.setUp()
        self.addCleanup(self.completed.doCleanups)
        self.fixture = self.completed.fixture
        self.sdk_fixture = producer_fixtures.NativeSdkSourceBuildTests(
            "test_real_policy_build_cache_and_relocated_native_execution"
        )
        self.sdk_fixture.setUp()
        self.addCleanup(self.sdk_fixture.doCleanups)
        self.service = self.sdk_fixture.service()
        self.built = self.service.build()
        self.owner = NativeSdkConsumerInputs(
            snapshot=self.sdk_fixture.snapshot, services=(self.service,)
        )
        self.lock = self.sdk_fixture.snapshot.authority.lock
        (self.binding,) = self.owner.for_scope(self.lock.root_revision)
        for item in self.built.product.snapshot.files:
            path = self.fixture.source / "vendor" / item.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self.owner.read_input_blob(self.binding, item.blob))
            path.chmod(0o755 if item.executable else 0o644)
        sdk_policy = RetainedProjectSdkPolicy(
            (
                RetainedProjectSdkBinding(
                    self.lock.identity, self.binding.identity, "vendor"
                ),
            )
        )
        (self.fixture.source / "sdk-policy.json").write_bytes(
            canonical_json_bytes(sdk_policy.to_dict())
        )
        actions = list(self.fixture.plan.profile.actions)
        actions[0] = replace(actions[0], requires=("sdk-build",))
        library = next(
            path
            for path in self.built.product.snapshot.native_libraries
            if "/vendor_math." in path
        )
        actions.insert(
            0,
            RetainedProjectAction(
                "sdk-build",
                "build",
                "shell",
                ("scale", "{input_root}/vendor/" + library, "{output_root}/scale.txt"),
                (),
                ("native-sdk-use",),
                ("scale.txt",),
                10,
            ),
        )
        self.fixture.plan = replace(
            self.fixture.plan,
            profile=replace(self.fixture.plan.profile, actions=tuple(actions)),
        )
        self.refresh()
        self.grants = []
        self.documents = {}

    def refresh(self):
        self.fixture.manifest = discover_retained_project_inputs(
            self.fixture.source,
            {
                "src": "source",
                "tools": "toolchain",
                "test-policy.json": "test-policy",
                "sdk-policy.json": "sdk-policy",
                "vendor": "sdk",
            },
            self.fixture.manifest.limits,
            executable_paths=("tools/runner",),
        )
        path = self.fixture.source / ACCEPTANCE_POLICY_PATH
        policy = RetainedProjectAcceptancePolicy.from_dict(
            json.loads(path.read_bytes())
        )
        policy = replace(policy, component_locks=(self.lock.identity,))
        content = canonical_json_bytes(policy.to_dict())
        path.write_bytes(content)
        profile = replace(
            self.fixture.plan.profile,
            toolchain_identity=self.fixture.manifest.role_identity(
                {"sdk", "toolchain"}
            ),
            dependency_identity=self.fixture.manifest.role_identity({"dependency"}),
            acceptance_policy_identity=ContentIdentity.parse_uri(
                "sha256:" + hashlib.sha256(content).hexdigest()
            ),
        )
        self.fixture.plan = replace(
            self.fixture.plan,
            manifest_identity=self.fixture.manifest.identity,
            profile=profile,
            component_locks=(self.lock.identity,),
        )
        self.fixture.current = self.fixture.plan
        self.completed.locks = (self.lock,)
        self.completed.graph = project_retained_project_managed_graph(
            self.fixture.plan, (self.lock,), version="1.0.0"
        )

    def record(self, document):
        reference = self.sdk_fixture.fixture.store.put_manifest(document)
        self.documents[reference.identity] = deepcopy(document)
        return ContentIdentity.parse_uri(reference.identity)

    def authorize(self, request, runtime):
        now = datetime.now(UTC)
        grant = BuildAuthorization(
            authorization_id="retained-sdk-fixture:"
            + canonical_identity(request.to_dict()).digest,
            classification_digest=runtime.uri,
            request_digest=canonical_identity(request.to_dict()).uri,
            effective_revision_digest=request.effective_revision_digest,
            actor="fixture-operator",
            reason="Execute exact captured SDK fixture",
            profile=SecurityProfile.CONSTRAINED,
            privileges=request.requested_privileges,
            issued_at=now,
            expires_at=now + timedelta(minutes=5),
        )
        self.grants.append(grant)
        return grant

    def workspace(self, **changes):
        return self.completed.workspace(
            **{
                "native_sdk_owners": (self.owner,),
                "native_sdk_authorize": self.authorize,
                "native_sdk_record": self.record,
                **changes,
            }
        )

    def finish(self):
        workspace = self.workspace()
        for _ in self.fixture.plan.profile.actions:
            workspace.execute_next()
        return workspace, workspace.completed_run()

    def test_real_selected_sdk_executes_in_retained_snapshot_with_boms_and_fresh_grants(
        self,
    ):
        workspace, result = self.finish()
        self.assertEqual((workspace.outputs / "scale.txt").read_bytes(), b"4.5")
        self.assertEqual(len(result.observations), 5)
        self.assertTrue(
            all(len(o.native_sdk_executions) == 1 for o in result.observations)
        )
        self.assertEqual(
            len(result.acceptance_observations[0].native_sdk_executions), 1
        )
        self.assertEqual(len(self.grants), 6)
        self.assertEqual(len(result.resolved_bom.sdk_dependencies), 1)
        dependency = result.resolved_bom.sdk_dependencies[0]
        self.assertEqual(dependency.input_identity, self.binding.identity)
        self.assertIn(
            dependency.repository_resolution.resolved_commit.encode(),
            result.resolved_bom.content,
        )
        final = self.documents[result.native_sdk_final_check_identity.uri]
        self.assertTrue(final["checks"])
        self.assertFalse(result.to_dict()["lifecycle_receipt"])

    def test_private_filesystem_case_preserves_live_sdk_command_scope(self):
        select_filesystem_case(self.completed)
        workspace, result = self.finish()
        (observed,) = result.acceptance_observations
        self.assertEqual(observed.filesystem_observation.entries[0].size, 3)
        self.assertEqual(len(observed.native_sdk_executions), 1)
        self.assertEqual((workspace.outputs / "scale.txt").read_bytes(), b"4.5")
        self.assertEqual(len(self.grants), 6)
        self.assertEqual(list(workspace.objects.glob("verifier-case-*")), [])

    def test_rehashed_substituted_sdk_bytes_are_not_the_selected_producer(self):
        path = (
            self.fixture.source
            / "vendor"
            / self.built.product.snapshot.native_libraries[0]
        )
        path.write_bytes(path.read_bytes() + b"foreign product")
        self.refresh()
        with self.assertRaisesRegex(ValueError, "product-file-mismatch"):
            self.workspace()

    def test_unbound_extra_sdk_files_refuse_even_in_a_self_consistent_manifest(self):
        (self.fixture.source / "vendor/extra.dat").write_bytes(b"unbound")
        self.refresh()
        with self.assertRaisesRegex(ValueError, "complete-sdk-file-closure"):
            self.workspace()

    def test_missing_owner_or_execution_authority_refuses_before_running_commands(self):
        with self.assertRaisesRegex(ValueError, "producer-bindings"):
            self.workspace(native_sdk_owners=None)
        with self.assertRaisesRegex(ValueError, "command-authority"):
            self.workspace(native_sdk_authorize=None)
        with self.assertRaisesRegex(ValueError, "exact-lock-owner-closure"):
            self.workspace(native_sdk_owners=())

    def revoke_latest(self):
        self.sdk_fixture.revocations = self.sdk_fixture.revocations.revoke(
            self.grants[-1].authorization_id,
            actor="fixture-operator",
            reason="stop SDK use",
        )

    def test_revocation_after_native_sdk_use_prevents_successful_command_admission(
        self,
    ):
        workspace = self.workspace()
        original = execution.run_bounded_process

        def execute_then_revoke(*args, **kwargs):
            result = original(*args, **kwargs)
            self.revoke_latest()
            return result

        with patch.object(
            execution, "run_bounded_process", side_effect=execute_then_revoke
        ):
            with self.assertRaises(AuthorizationError):
                workspace.execute_next()
        self.assertEqual((workspace.outputs / "scale.txt").read_bytes(), b"4.5")
        self.assertEqual(workspace.observations, ())
        with self.assertRaisesRegex(ValueError, "run-incomplete"):
            workspace.completed_run()

    def test_revocation_after_independent_acceptance_refuses_the_completed_run(self):
        workspace = self.workspace()
        for _ in self.fixture.plan.profile.actions:
            workspace.execute_next()
        self.revoke_latest()
        with self.assertRaises(AuthorizationError):
            workspace.completed_run()
