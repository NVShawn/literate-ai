"""Real SDK products, captured retained commands and live revocation boundaries."""

import hashlib
import json
import os
import shutil
import unittest
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters import retained_project_execution as execution
from literate_ai.adapters.native_sdk_consumer import NativeSdkConsumerInputs
from literate_ai.adapters.native_sdk_qualification import (
    verify_historical_sdk_authority_checks,
)
from literate_ai.adapters.qualification_capture import QualificationCaptureError
from literate_ai.adapters.retained_project_acceptance import ACCEPTANCE_POLICY_PATH
from literate_ai.adapters.retained_project_inputs import (
    discover_retained_project_inputs,
)
from literate_ai.adapters.retained_project_planning import RetainedProjectPlanningResult
from literate_ai.adapters.retained_project_receipt import admit_retained_project
from literate_ai.adapters.retained_unit_inspection import (
    read_retained_unit_planning_inputs,
)
from literate_ai.adapters.retained_unit_planning_evidence import (
    retain_retained_unit_planning_evidence,
)
from literate_ai.adapters.retained_unit_run_evidence import (
    retain_retained_unit_run_evidence,
)
from literate_ai.adapters.retained_unit_run_inspection import reopen_retained_unit_run
from literate_ai.cache_directories import CacheDirectories
from literate_ai.contracts import (
    BlobRef,
    ContentIdentity,
    SourceIntelligenceMode,
    canonical_identity,
    canonical_json_bytes,
    load_current_standard_lifecycle_policy,
)
from literate_ai.contracts.execution_dispatch import (
    ExecutionWorker,
    ExecutionWorkerKind,
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
    RetainedProjectSdkSelectionBinding,
    RetainedProjectSdkSelectionPolicy,
    read_retained_sdk_policy_document,
)
from literate_ai.contracts.retained_unit_policy import RetainedUnitPackagePolicy
from literate_ai.security import (
    AuthorizationError,
    AuthorizationRevocationSet,
    BuildAuthorization,
    BuildRequest,
    SecurityProfile,
)
from tests.critical import test_retained_project_completion as completion
from tests.critical import test_retained_project_execution as commands
from tests.support import fixtures_test_native_sdk_source_build as producer_fixtures
from tests.support.fixtures_test_repository_sources import source_intelligence_policy
from tests.support.fixtures_test_retained_filesystem import select_filesystem_case


class RetainedSdkPolicyTests(unittest.TestCase):
    def test_selected_snapshot_policy_is_explicit_and_does_not_reinterpret_legacy(self):
        lock = canonical_identity("lock")
        selection = canonical_identity("recipe selection")
        snapshot = canonical_identity("snapshot")
        binding = RetainedProjectSdkSelectionBinding(
            lock, selection, snapshot, "vendor/math"
        )
        policy = RetainedProjectSdkSelectionPolicy((binding,))
        wire = policy.to_dict()
        self.assertEqual(read_retained_sdk_policy_document(wire), policy)
        with self.assertRaises(ValueError):
            RetainedProjectSdkPolicy.from_dict(wire)
        legacy = RetainedProjectSdkPolicy(
            (RetainedProjectSdkBinding(lock, snapshot, "vendor/math"),)
        )
        self.assertEqual(read_retained_sdk_policy_document(legacy.to_dict()), legacy)
        with self.assertRaises(ValueError):
            RetainedProjectSdkSelectionPolicy.from_dict(legacy.to_dict())
        with self.assertRaises(ValueError):
            RetainedProjectSdkSelectionPolicy(
                (
                    binding,
                    replace(binding, snapshot_identity=canonical_identity("different")),
                )
            )
        with self.assertRaises(ValueError):
            RetainedProjectSdkSelectionPolicy(
                (
                    binding,
                    replace(
                        binding,
                        selection_identity=canonical_identity("other"),
                        prefix="vendor/math/child",
                    ),
                )
            )
        for field in ("accepted", "input_identity"):
            altered = deepcopy(wire)
            altered["bindings"][0][field] = True
            with self.assertRaises(ValueError):
                read_retained_sdk_policy_document(altered)
        with self.assertRaisesRegex(ValueError, "foreign-component-lock"):
            policy.require_locks((snapshot,))

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
            executable_paths=(self.fixture.runner_path,),
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

    def select_snapshot_policy(self, **changes):
        binding = RetainedProjectSdkSelectionBinding(
            self.lock.identity,
            self.built.selection.identity,
            self.built.product.snapshot.identity,
            "vendor",
        )
        policy = RetainedProjectSdkSelectionPolicy((replace(binding, **changes),))
        (self.fixture.source / "sdk-policy.json").write_bytes(
            canonical_json_bytes(policy.to_dict())
        )
        self.refresh()

    def test_selected_snapshot_accepts_new_live_producer_and_retains_fresh_grants(self):
        second_service = self.sdk_fixture.service()
        second = second_service.build()
        second_owner = NativeSdkConsumerInputs(
            snapshot=self.sdk_fixture.snapshot, services=(second_service,)
        )
        (second_binding,) = second_owner.for_scope(self.lock.root_revision)
        self.assertNotEqual(second_binding.identity, self.binding.identity)
        self.assertEqual(
            second.product.snapshot.identity, self.built.product.snapshot.identity
        )
        self.select_snapshot_policy()
        workspace = self.workspace(native_sdk_owners=(second_owner,))
        for _ in self.fixture.plan.profile.actions:
            workspace.execute_next()
        result = workspace.completed_run()
        self.assertEqual((workspace.outputs / "scale.txt").read_bytes(), b"4.5")
        self.assertEqual(
            result.resolved_bom.sdk_dependencies[0].input_identity,
            second_binding.identity,
        )
        self.assertEqual(len(self.grants), 6)
        self.assertTrue(
            self.documents[result.native_sdk_final_check_identity.uri]["checks"]
        )

    def test_legacy_policy_still_refuses_another_producer_run(self):
        second_service = self.sdk_fixture.service()
        second_service.build()
        second_owner = NativeSdkConsumerInputs(
            snapshot=self.sdk_fixture.snapshot, services=(second_service,)
        )
        with self.assertRaisesRegex(ValueError, "complete-product-bindings-required"):
            self.workspace(native_sdk_owners=(second_owner,))

    def test_selected_policy_refuses_changed_snapshot_or_recipe(self):
        for name in ("snapshot_identity", "selection_identity"):
            with self.subTest(name=name):
                self.select_snapshot_policy(**{name: canonical_identity("foreign")})
                with self.assertRaisesRegex(
                    ValueError, "complete-selected-snapshot-closure-required"
                ):
                    self.workspace()

    def public_runtime_arguments(self, **selection_changes):
        """Supply discovery and local Git routing; use real SDK production owners."""
        self.select_snapshot_policy(**selection_changes)
        f = self.fixture
        worker = ExecutionWorker("local", ExecutionWorkerKind.LOCAL)
        trust = {"fixture": "reviewed installed Standard binding"}
        binding = SimpleNamespace(
            require_unchanged=lambda: None,
            trust_binding=SimpleNamespace(to_dict=lambda: trust),
            policy=load_current_standard_lifecycle_policy(),
        )
        f.plan = replace(
            f.plan,
            standard_binding_identity=canonical_identity(trust),
            profile=replace(f.plan.profile, worker_identity=worker.identity),
        )
        f.current = f.plan
        recipe = self.sdk_fixture.recipe_fixture
        project = SimpleNamespace(
            root=f.source,
            definition=SimpleNamespace(
                source_intelligence=source_intelligence_policy(
                    "none", SourceIntelligenceMode.OFF
                )
            ),
            roots=lambda kind: (recipe.flavors,),
            flavor_selectors_for=lambda root, selectors: (
                "+python",
                "+" + self.sdk_fixture.fixture.layout.operating_system,
            ),
        )
        planning = RetainedProjectPlanningResult(
            f.plan,
            project_retained_project_managed_graph(
                f.plan, (self.lock,), version="1.0.0"
            ),
        )
        return (f.source, f.manifest, planning), {
            "component_locks": (self.lock,),
            "worker": worker,
            "current_plan": lambda: f.current,
            "test_receipt_policy": f.configured_test_policy,
            "cache_directories": CacheDirectories(
                f.source, f.base / "generated", f.base / "objects"
            ),
            "authorization": f.plan.identity.uri,
            "allow_host_execution": True,
            "operator_environment": dict(os.environ),
            "sdk_project": project,
            "sdk_binding": binding,
        }

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
        # Inspect only the genuine recorded grants; this does not restore their
        # original live scopes or authorize a command on this controller.
        observations = (*result.observations, *result.acceptance_observations)
        last_scopes = []
        for observation in observations:
            scopes = []
            for identity in observation.native_sdk_executions:
                value = self.documents[identity.uri]
                grant = BuildAuthorization.from_dict(value["authorization"])
                request = BuildRequest.from_dict(value["request"])
                inputs = tuple(
                    ContentIdentity.from_dict(v["input_identity"])
                    for v in value["dependencies"]
                )
                after = verify_historical_sdk_authority_checks(
                    value["checks"], grant, request, inputs
                )
                # A self-consistent record digest cannot hide missing input
                # coverage or a recorded revocation of this actual command grant.
                missing = deepcopy(value["checks"])
                missing[0]["revocations"] = []
                with self.assertRaises(QualificationCaptureError):
                    verify_historical_sdk_authority_checks(
                        missing, grant, request, inputs
                    )
                revoked = deepcopy(value["checks"])
                revoked[0]["revocations"][0]["state"] = (
                    AuthorizationRevocationSet()
                    .revoke(
                        grant.authorization_id,
                        actor="independent-verifier",
                        reason="Refuse revoked recorded command scope",
                    )
                    .to_dict()
                )
                with self.assertRaises(AuthorizationError):
                    verify_historical_sdk_authority_checks(
                        revoked, grant, request, inputs
                    )
                scopes.append((grant, request, inputs, after))
            last_scopes = scopes
        self.assertEqual(len(final["checks"]), len(last_scopes))
        for check, (grant, request, inputs, after) in zip(
            final["checks"], last_scopes, strict=True
        ):
            verify_historical_sdk_authority_checks(
                [check],
                grant,
                request,
                inputs,
                expected_count=1,
                not_before=after,
            )
            changed = deepcopy(check)
            changed["checked_at"] = (after - timedelta(seconds=1)).isoformat()
            with self.assertRaises(QualificationCaptureError):
                verify_historical_sdk_authority_checks(
                    [changed],
                    grant,
                    request,
                    inputs,
                    expected_count=1,
                    not_before=after,
                )

        # Reopen the actual SDK-bearing accepted run using only retained bytes.
        # This preserves immutable facts; no decoded grant becomes a live owner.
        store = self.sdk_fixture.fixture.store
        admission = admit_retained_project(workspace, store)
        run = admission.run
        planning = RetainedProjectPlanningResult(
            run.source_bom.plan, run.source_bom.managed_graph
        )
        policy = RetainedUnitPackagePolicy(
            run.source_bom.manifest.limits,
            run.source_bom.manifest.limits,
            10,
            100,
            64 * 1024**2,
            1024**3,
            1024**3,
            1024**3,
            1024**3,
            max_provider_memory_bytes=1024**3,
        )
        evidence_reference = retain_retained_unit_run_evidence(
            admission,
            maximum_bytes=policy.max_provider_metadata_bytes,
            maximum_memory_bytes=policy.max_provider_memory_bytes,
        )
        inputs = read_retained_unit_planning_inputs(
            admission.source,
            run.source_bom.manifest,
            run.source_bom.plan,
            run.source_bom.component_locks,
            store.put_manifest(
                admission.receipt.to_dict(), media_type="application/json"
            ),
            store,
            planning_evidence_reference=retain_retained_unit_planning_evidence(
                admission
            ),
        )

        def reopen(reference, selected=inputs):
            admission.require_current()
            result = reopen_retained_unit_run(
                selected,
                planning,
                reference,
                policy=policy,
                test_receipt_policy=run.configured_test_policy,
                admission_guard=lambda: None,
                current_sdk_snapshots=(self.sdk_fixture.snapshot,),
            )
            admission.require_current()
            return result

        reopened = reopen(evidence_reference)
        self.assertEqual(reopened.to_dict(), run.to_dict())
        evidence = store.get_manifest(evidence_reference)
        self.assertTrue(evidence["sdk_metadata_records"])
        recipe_identity = self.built.selection.recipe.identity.uri
        self.assertIn(
            recipe_identity,
            {BlobRef.from_dict(v).identity for v in evidence["sdk_metadata_records"]},
        )
        changed = deepcopy(evidence)
        changed["sdk_metadata_records"] = [
            v
            for v in changed["sdk_metadata_records"]
            if BlobRef.from_dict(v).identity != recipe_identity
        ]
        with self.assertRaisesRegex(ValueError, "run-record-missing"):
            reopen(store.put_manifest(changed, media_type="application/json"))
        changed = deepcopy(evidence)
        changed["sdk_metadata_records"].append(
            store.put_manifest({"unreachable": True}).to_dict()
        )
        with self.assertRaisesRegex(ValueError, "exact-sdk-metadata-closure"):
            reopen(store.put_manifest(changed, media_type="application/json"))

        # Rehash the complete assertion chain, including the grant's request
        # digest. An immutable checksum alone must not authorize different argv.
        first = run.observations[0]
        execution_identity = first.native_sdk_executions[0]
        references = {
            BlobRef.from_dict(v).identity: BlobRef.from_dict(v)
            for v in evidence["sdk_metadata_records"]
        }
        execution_document = store.get_manifest(references[execution_identity.uri])
        altered_execution = deepcopy(execution_document)
        altered_execution["retained_command_context"]["argv"].append(
            "unreviewed-argument"
        )
        altered_execution["command_binding"]["argv_identity"] = canonical_identity(
            altered_execution["retained_command_context"]["argv"]
        ).to_dict()
        altered_execution["request"]["builder_id"] = canonical_identity(
            altered_execution["command_binding"]
        ).uri
        altered_execution["authorization"]["request_digest"] = canonical_identity(
            altered_execution["request"]
        ).uri
        updates = {
            canonical_identity(execution_document[name]).uri: store.put_manifest(
                altered_execution[name], media_type="application/json"
            )
            for name in ("request", "authorization")
        }
        altered_execution_ref = store.put_manifest(
            altered_execution, media_type="application/json"
        )
        updates[execution_identity.uri] = altered_execution_ref
        altered_observation = replace(
            first,
            native_sdk_executions=(
                ContentIdentity.parse_uri(altered_execution_ref.identity),
                *first.native_sdk_executions[1:],
            ),
        )
        altered_run = replace(
            run, observations=(altered_observation, *run.observations[1:])
        )
        altered_observation_ref = store.put_manifest(
            altered_observation.to_dict(), media_type="application/json"
        )
        altered_run_ref = store.put_manifest(
            altered_run.to_dict(), media_type="application/json"
        )
        removed = {first.identity.uri, run.identity.uri, execution_identity.uri}
        records = {
            ref.identity: ref
            for ref in admission.receipt.records
            if ref.identity not in removed
        }
        for ref in (altered_observation_ref, altered_run_ref, altered_execution_ref):
            records[ref.identity] = ref
        altered_receipt = replace(
            admission.receipt,
            roots=tuple(
                (name, altered_run_ref if name == "completed-run" else ref)
                for name, ref in admission.receipt.roots
            ),
            records=tuple(records[key] for key in sorted(records)),
        )
        altered_planning = store.get_manifest(
            retain_retained_unit_planning_evidence(admission)
        )
        altered_planning["retained_receipt_identity"] = altered_receipt.identity.uri
        altered_inputs = read_retained_unit_planning_inputs(
            admission.source,
            run.source_bom.manifest,
            run.source_bom.plan,
            run.source_bom.component_locks,
            store.put_manifest(
                altered_receipt.to_dict(), media_type="application/json"
            ),
            store,
            planning_evidence_reference=store.put_manifest(
                altered_planning, media_type="application/json"
            ),
        )
        altered_evidence = deepcopy(evidence)
        altered_evidence["retained_receipt_identity"] = altered_receipt.identity.uri
        altered_evidence["sdk_metadata_records"] = [
            updates.get(
                BlobRef.from_dict(value).identity, BlobRef.from_dict(value)
            ).to_dict()
            for value in evidence["sdk_metadata_records"]
        ]
        with self.assertRaisesRegex(ValueError, "historical-command-context-differs"):
            reopen(
                store.put_manifest(altered_evidence, media_type="application/json"),
                altered_inputs,
            )

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
