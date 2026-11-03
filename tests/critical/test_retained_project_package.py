"""Real retained runs use the existing ZIP provider without source promotion."""

import hashlib
import io
import json
import os
import platform
import shutil
import subprocess
import unittest
import zipfile
from dataclasses import replace
from functools import partial
from pathlib import Path
from unittest.mock import PropertyMock, patch

from literate_ai.adapters.component_lock_application import current_component_lock
from literate_ai.adapters.freebsd_packaging import FreeBsdPkgToolBinding
from literate_ai.adapters.native_tar_archives import NativeTarToolBinding
from literate_ai.adapters.repository_lineage import FilesystemRepositoryLineageStore
from literate_ai.adapters.retained_project_acceptance import ACCEPTANCE_POLICY_PATH
from literate_ai.adapters.retained_project_inputs import (
    _remove_tree,
    discover_retained_project_inputs,
)
from literate_ai.adapters.retained_project_package import RetainedProjectZipPackage
from literate_ai.adapters.retained_project_planning import RetainedProjectPlanningResult
from literate_ai.adapters.retained_project_receipt import admit_retained_project
from literate_ai.adapters.retained_project_unit_packages import (
    RetainedProjectUnitPackages,
    plan_retained_unit_packages,
)
from literate_ai.adapters.retained_unit_inspection import (
    read_retained_unit_planning_inputs,
)
from literate_ai.adapters.retained_unit_link import link_retained_unit_packages
from literate_ai.adapters.retained_unit_planning_evidence import (
    retain_retained_unit_planning_evidence,
)
from literate_ai.adapters.retained_unit_results import read_retained_unit_results
from literate_ai.adapters.retained_unit_run_evidence import (
    retain_retained_unit_run_evidence,
)
from literate_ai.adapters.retained_unit_run_inspection import reopen_retained_unit_run
from literate_ai.adapters.retained_unit_specifications import (
    bind_retained_unit_specifications,
)
from literate_ai.application.packaging import PackagingError
from literate_ai.cli import main as cli_main
from literate_ai.contracts import (
    BlobRef,
    ContentIdentity,
    ContentReference,
    RepositoryLineage,
    RepositoryParentSelection,
    canonical_json_bytes,
)
from literate_ai.contracts.authoring_markdown import render_authoring_markdown
from literate_ai.contracts.component_locking import (
    ComponentLock,
    ComponentLockNode,
    ordered_specification_set_identity,
)
from literate_ai.contracts.executable_components import (
    NodeFlavorSlotResolution,
    NodeTargetFlavorSelection,
    SelectedNodeFlavor,
)
from literate_ai.contracts.executable_components.packages import (
    PackagePlan,
    PackageResult,
)
from literate_ai.contracts.flavors import FlavorAxis, FlavorCardinality, FlavorSlot
from literate_ai.contracts.projects import ProjectDefinition
from literate_ai.contracts.retained_project import (
    RetainedProjectLimits,
    RetainedProjectTreePolicy,
)
from literate_ai.contracts.retained_project_acceptance import (
    RetainedProjectAcceptancePolicy,
)
from literate_ai.contracts.retained_project_package import (
    RetainedProjectPackagePlan,
    RetainedProjectPackageResult,
)
from literate_ai.contracts.retained_project_sbom import (
    project_retained_project_managed_graph,
)
from literate_ai.contracts.retained_unit_policy import RetainedUnitPackagePolicy
from literate_ai.contracts.retained_unit_requests import RetainedUnitPackageRequest
from literate_ai.projects import discover_project
from literate_ai.storage.cas import BlobIntegrityError, FileSystemCAS
from tests.critical import test_retained_project_completion as completed
from tests.critical import test_retained_project_execution as commands
from tests.support import fixtures_test_component_lock_contracts as locked


def package_lock(specification, provider="zip", extra_specifications=()):
    authoring = locked.component_authoring("retained-package")
    specification_roots = tuple(
        sorted(
            ("specs/retained-package.md", *(item.uri for item in extra_specifications))
        )
    )
    authoring = replace(
        authoring,
        specification_roots=specification_roots,
        flavor_slots=(
            *authoring.flavor_slots,
            FlavorSlot(
                "packaging",
                FlavorAxis.PACKAGING,
                FlavorCardinality.EXACTLY_ONE,
                "native-package-provider",
            ),
        ),
    )
    reference = ContentReference(
        "specification",
        "specs/retained-package.md",
        ContentIdentity.parse_uri(
            "sha256:" + hashlib.sha256(specification).hexdigest()
        ),
    )
    revision = locked.locked_revision(
        "retained-package", selected_flavor="rust", authoring=authoring
    )
    specifications = tuple(
        sorted((reference, *extra_specifications), key=lambda value: value.uri)
    )
    revision = replace(
        revision,
        specifications=specifications,
        specification_set_identity=ordered_specification_set_identity(
            authoring.specification_provider, specifications
        ),
        selected_flavor_revisions=tuple(
            sorted(
                (locked.identity("flavor-rust"), locked.identity("flavor-" + provider)),
                key=lambda v: v.uri,
            )
        ),
    )
    target, selection = (
        locked.identity("package-target"),
        locked.identity("selection-policy"),
    )
    selected = NodeTargetFlavorSelection(
        revision.identity,
        "host",
        target,
        selection,
        tuple(
            NodeFlavorSlotResolution(
                slot,
                (
                    SelectedNodeFlavor(
                        provider if slot.axis is FlavorAxis.PACKAGING else "rust",
                        locked.identity(
                            "flavor-" + provider
                            if slot.axis is FlavorAxis.PACKAGING
                            else "flavor-rust"
                        ),
                    ),
                ),
            )
            for slot in authoring.flavor_slots
        ),
    )
    return ComponentLock(
        "host",
        target,
        selection,
        locked.identity("resolver"),
        revision.identity,
        (ComponentLockNode(revision, selected, ()),),
        (),
        (authoring,),
    )


@unittest.skipUnless(
    os.name != "nt" and shutil.which("cc"), "requires native POSIX fixture"
)
class RetainedProjectPackageTests(unittest.TestCase):
    def test_actual_nested_lock_uses_exact_captured_declaration_not_identical_copy(
        self,
    ):
        """Only the registered Component path can supply a locked unit declaration."""
        for captured in (True, False):
            with self.subTest(nested_declaration_captured=captured):
                self.fixture = completed.RetainedProjectCompletionTests()
                self.fixture.setUp()
                self.addCleanup(self.fixture.doCleanups)
                self.package_provider = "pkg"
                request = RetainedUnitPackageRequest(
                    "application",
                    (),
                    True,
                    "specs/LICENSE",
                    "retained-application",
                    "1.0.0",
                    "misc/retained-application",
                    "/usr/local",
                    (),
                )
                self.finish(unit_request=request, registered_component=captured)
                arguments = self.unit_plan_arguments()
                source = self.admission.source
                project = discover_project(source)
                self.assertIsNotNone(project)

                context = partial(
                    bind_retained_unit_specifications,
                    project,
                    source,
                    self.admission.run.source_bom.manifest,
                    self.admission.run.source_bom.component_locks,
                    maximum_units=arguments["max_units"],
                    maximum_memory_bytes=256 * 1024**2,
                )

                if not captured:
                    with self.assertRaisesRegex(
                        ValueError, "locked-specification-source-mismatch"
                    ):
                        context()
                    continue
                owner = RetainedProjectUnitPackages(
                    self.admission, specification_context=context(), **arguments
                )
                owner.require_current()
                self.assertEqual(len(owner.projection.graph.partition.definitions), 1)
                evidence = owner.retain_planning_evidence()
                receipt = self.admission.receipt
                inputs = read_retained_unit_planning_inputs(
                    source,
                    self.admission.run.source_bom.manifest,
                    self.admission.run.source_bom.plan,
                    self.admission.run.source_bom.component_locks,
                    BlobRef(
                        receipt.identity.digest,
                        len(canonical_json_bytes(receipt.to_dict())),
                        media_type="application/json",
                    ),
                    self.store,
                    planning_evidence_reference=evidence,
                )
                self.assertEqual(
                    plan_retained_unit_packages(
                        inputs, specification_context=context(), **arguments
                    ).identity,
                    owner.projection.identity,
                )

    def test_actual_output_links_require_policy_and_bind_package_custody(self):
        """Native output aliases must enter the verified lifecycle before packaging."""
        for selected in (False, True):
            with self.subTest(tree_policy=selected):
                case = RetainedProjectPackageTests()
                case.setUp()
                self.addCleanup(case.doCleanups)
                case.package_provider = "pkg"
                request = RetainedUnitPackageRequest(
                    "application",
                    (),
                    True,
                    "specs/LICENSE",
                    "retained-tree",
                    "1.0.0",
                    "misc/retained-tree",
                    "/usr/local",
                    (),
                )
                if not selected:
                    with self.assertRaisesRegex(
                        ValueError, "Retained input is redirected"
                    ):
                        case.finish(
                            unit_request=request,
                            tree_outputs=True,
                            selected_tree_policy=False,
                        )
                    continue
                workspace = case.finish(unit_request=request, tree_outputs=True)
                manifest = case.admission.payload_manifest
                self.assertEqual(manifest.tree_policy, RetainedProjectTreePolicy())
                members = {member.path: member for member in manifest.members}
                self.assertEqual(members["z-link"].symlink_target, "app.bin")
                self.assertEqual(members["z-hardlink"].hardlink_target, "app.bin")
                self.assertEqual(members["z-directory"].symlink_target, "empty")
                self.assertIn("empty", manifest.directories)
                owner = RetainedProjectUnitPackages(
                    case.admission, **case.unit_plan_arguments()
                )
                handoff = link_retained_unit_packages(owner)
                handoff.require_current(owner)
                path = workspace.outputs / "z-hardlink"
                content, mode = path.read_bytes(), path.stat().st_mode & 0o777
                path.unlink()
                path.write_bytes(content)
                path.chmod(mode)
                with self.assertRaisesRegex(ValueError, "[Hh]ard link topology"):
                    workspace.completed_run()
                # Package custody owns independently retained bytes and topology.
                handoff.require_current(owner)
                retained = case.store.path_for(
                    BlobRef(members["app.bin"].sha256, members["app.bin"].size)
                )
                retained.chmod(0o644)
                retained.write_bytes(b"changed sealed custody")
                with self.assertRaises(BlobIntegrityError):
                    handoff.require_current(owner)

    @unittest.skipUnless(platform.system() == "FreeBSD", "requires native FreeBSD")
    def test_live_native_unit_construction_reopens_without_installing(self):
        self.package_provider = "pkg"
        request = RetainedUnitPackageRequest(
            "application",
            (),
            True,
            "specs/LICENSE",
            "retained-application",
            "1.0.0",
            "misc/retained-application",
            "/usr/local",
            (),
        )
        self.finish(unit_request=request, documentation_path="literal%25-λ.txt")
        arguments = self.unit_plan_arguments()
        arguments.update(
            pkg_tool=FreeBsdPkgToolBinding.discover(),
            tar_tool=NativeTarToolBinding.discover(),
        )
        owner = RetainedProjectUnitPackages(self.admission, **arguments)
        root = self.fixture.fixture.base
        first = owner.construct(object_root=root)
        reference = owner.construct_set(object_root=root)
        index, repeated = read_retained_unit_results(
            owner.projection, reference, self.store, **owner.tree_policy
        )
        self.assertEqual(repeated, first)
        self.assertEqual(owner.verify_set(reference, object_root=root), index)
        self.assertEqual(len(first), 1)
        self.store.verify(first[0].archive)
        self.assertGreater(first[0].archive.size, 0)
        # A second real lifecycle has a distinct receipt and projection. Verify
        # the original bytes through the public owner instead of constructing a
        # replacement archive or reusing the first admission's package scope.
        from literate_ai.adapters.retained_original_unit_context import (
            OriginalUnitMetadataBudget,
            read_original_unit_context,
        )
        from literate_ai.adapters.retained_original_unit_evidence import (
            OriginalRetainedUnitEvidence,
        )

        old = self.admission
        policy = RetainedUnitPackagePolicy(
            **{
                key: value
                for key, value in arguments.items()
                if key not in {"pkg_tool", "tar_tool"}
            }
        )
        planning = RetainedProjectPlanningResult(
            old.run.source_bom.plan, old.run.source_bom.managed_graph
        )
        request = {
            "authority_mode": "retained-project",
            "operation": "execute",
            "retained_plan_identity": planning.plan.identity.uri,
            "release_authorized": False,
        }
        evidence = OriginalRetainedUnitEvidence(
            planning.plan.identity,
            planning.plan.profile.worker_identity,
            self.store.put_manifest(request, media_type="application/json"),
            self.store.put_manifest(
                old.receipt.to_dict(), media_type="application/json"
            ),
            owner.retain_planning_evidence(),
            retain_retained_unit_run_evidence(
                old,
                maximum_bytes=policy.max_provider_metadata_bytes,
                maximum_memory_bytes=policy.max_provider_memory_bytes,
            ),
            link_retained_unit_packages(owner).reference,
            reference,
        )
        context = read_original_unit_context(
            evidence,
            source=old.source,
            manifest=old.run.source_bom.manifest,
            planning=planning,
            component_locks=old.run.source_bom.component_locks,
            store=OriginalUnitMetadataBudget(self.store, policy, old.require_current),
            policy=policy,
            pkg_tool=arguments["pkg_tool"],
            tar_tool=arguments["tar_tool"],
            test_receipt_policy=old.run.configured_test_policy,
            admission_guard=old.require_current,
            expected_request=request,
            expected_worker_identity=planning.plan.profile.worker_identity,
        )
        workspace, _run = self.fixture.finish()
        fresh_admission = admit_retained_project(workspace, self.store)
        fresh = RetainedProjectUnitPackages(fresh_admission, **arguments)
        self.assertNotEqual(fresh_admission.receipt.identity, old.receipt.identity)
        self.assertNotEqual(fresh.projection.identity, owner.projection.identity)
        handoff = link_retained_unit_packages(fresh)
        self.assertEqual(
            fresh.verify_original_set(
                context,
                object_root=root,
                link_handoff=handoff,
                fresh_workspace_roots=(),
            ),
            index,
        )
        original_index, original_archives = read_retained_unit_results(
            owner.projection, reference, self.store, **owner.tree_policy
        )
        self.assertEqual((original_index, original_archives), (index, first))
        archive_path = self.store.path_for(first[0].archive)
        archive_bytes = archive_path.read_bytes()
        archive_path.write_bytes(b"corrupt original archive")
        try:
            with self.assertRaises((BlobIntegrityError, PackagingError, ValueError)):
                fresh.verify_original_set(
                    context,
                    object_root=root,
                    link_handoff=handoff,
                    fresh_workspace_roots=(),
                )
        finally:
            archive_path.write_bytes(archive_bytes)
        path = self.fixture.fixture.source / "specs/LICENSE"
        original = path.read_bytes()
        path.write_bytes(original + b"changed license\n")
        try:
            with self.assertRaises(ValueError):
                owner.verify_set(reference, object_root=root)
            with self.assertRaises(ValueError):
                fresh.verify_original_set(
                    context,
                    object_root=root,
                    link_handoff=handoff,
                    fresh_workspace_roots=(),
                )
            self.store.verify(first[0].archive)
        finally:
            path.write_bytes(original)
        owner.require_current()

    def test_return_evidence_reopens_real_run_and_rejects_rebound_acceptance(self):
        """Rehashing return metadata cannot replace canonical completion checks."""
        self.finish()
        admission, store = self.admission, self.store
        run = admission.run
        planning = RetainedProjectPlanningResult(
            run.source_bom.plan, run.source_bom.managed_graph
        )
        arguments = self.unit_plan_arguments()
        policy = RetainedUnitPackagePolicy(
            **{
                key: value
                for key, value in arguments.items()
                if key not in {"pkg_tool", "tar_tool"}
            }
        )
        receipt_ref = store.put_manifest(
            admission.receipt.to_dict(), media_type="application/json"
        )
        planning_ref = retain_retained_unit_planning_evidence(admission)
        evidence_ref = retain_retained_unit_run_evidence(
            admission,
            maximum_bytes=policy.max_provider_metadata_bytes,
            maximum_memory_bytes=policy.max_provider_memory_bytes,
        )

        def inputs(receipt_reference, planning_reference):
            return read_retained_unit_planning_inputs(
                admission.source,
                run.source_bom.manifest,
                run.source_bom.plan,
                run.source_bom.component_locks,
                receipt_reference,
                store,
                planning_evidence_reference=planning_reference,
            )

        def reopen(selected, evidence):
            return reopen_retained_unit_run(
                selected,
                planning,
                evidence,
                policy=policy,
                test_receipt_policy=run.configured_test_policy,
                admission_guard=admission.require_current,
            )

        selected = inputs(receipt_ref, planning_ref)
        reopened = reopen(selected, evidence_ref)
        self.assertEqual(reopened.to_dict(), run.to_dict())
        self.assertEqual(reopened.identity, run.identity)
        self.assertFalse(reopened.to_dict()["sdk_admitted"])
        self.assertFalse(reopened.to_dict()["lifecycle_receipt"])
        evidence = store.get_manifest(evidence_ref)
        selected_refs = [
            BlobRef.from_dict(evidence["tool_observation"]),
            BlobRef.from_dict(evidence["export_observation"]),
            *(BlobRef.from_dict(v) for v in evidence["sdk_dependencies"]),
            *(
                BlobRef.from_dict(v)
                for v in evidence["command_output_manifests"]
                if v is not None
            ),
            evidence_ref,
        ]
        unique = {ref.identity: ref.size for ref in selected_refs}
        before = set(store.iter_refs())
        with self.assertRaisesRegex(
            ValueError, "run-evidence-metadata-policy-exceeded"
        ):
            retain_retained_unit_run_evidence(
                admission,
                maximum_bytes=sum(unique.values()) - 1,
                maximum_memory_bytes=policy.max_provider_memory_bytes,
            )
        self.assertEqual(set(store.iter_refs()), before)
        # Keep hashes, receipt roots and observation joins internally consistent
        # while removing actual independent acceptance. A historical reader may
        # reopen those assertions; canonical completion must refuse them.
        for label, field, values, expected in (
            (
                "missing-acceptance",
                "acceptance_observations",
                [],
                "complete-run-observations-required",
            ),
            (
                "repeated-command",
                "command_observations",
                [run.observations[0].identity.uri] * len(run.observations),
                "unique-run-observations-required",
            ),
        ):
            with self.subTest(case=label):
                incomplete = run.to_dict()
                incomplete[field] = values
                incomplete_ref = store.put_manifest(
                    incomplete, media_type="application/json"
                )
                old_ref = admission.receipt.root("completed-run")
                records = {
                    r.identity: r for r in admission.receipt.records if r != old_ref
                }
                records[incomplete_ref.identity] = incomplete_ref
                forged = replace(
                    admission.receipt,
                    roots=tuple(
                        (name, incomplete_ref if name == "completed-run" else ref)
                        for name, ref in admission.receipt.roots
                    ),
                    records=tuple(records[name] for name in sorted(records)),
                )
                forged_ref = store.put_manifest(
                    forged.to_dict(), media_type="application/json"
                )
                references = []
                for ref in (planning_ref, evidence_ref):
                    document = store.get_manifest(ref)
                    document["retained_receipt_identity"] = forged.identity.uri
                    references.append(
                        store.put_manifest(document, media_type="application/json")
                    )
                with self.assertRaisesRegex(ValueError, expected):
                    reopen(inputs(forged_ref, references[0]), references[1])
        admission.require_current()

    def test_unit_plan_requires_live_admission_and_locked_output_license_intent(self):
        self.package_provider = "pkg"
        request = RetainedUnitPackageRequest(
            "application",
            (),
            True,
            "specs/LICENSE",
            "retained-application",
            "1.0.0",
            "misc/retained-application",
            "/usr/local",
            (),
        )
        self.finish(unit_request=request)
        arguments = self.unit_plan_arguments()
        before = set(self.store.iter_refs())
        owner = RetainedProjectUnitPackages(self.admission, **arguments)
        owner.require_current()
        projection = owner.projection
        self.assertEqual(set(self.store.iter_refs()), before)
        evidence = owner.retain_planning_evidence()
        before = set(self.store.iter_refs())
        receipt = self.admission.receipt
        reference = BlobRef(
            receipt.identity.digest,
            len(canonical_json_bytes(receipt.to_dict())),
            media_type="application/json",
        )
        inputs = read_retained_unit_planning_inputs(
            self.admission.source,
            self.admission.run.source_bom.manifest,
            self.admission.run.source_bom.plan,
            self.admission.run.source_bom.component_locks,
            reference,
            self.store,
            planning_evidence_reference=evidence,
        )
        self.assertEqual(
            plan_retained_unit_packages(inputs, **arguments).identity,
            projection.identity,
        )
        with self.assertRaisesRegex(TypeError, "live-admission-required"):
            RetainedProjectUnitPackages(inputs, **arguments)
        self.assertEqual(set(self.store.iter_refs()), before)
        # Rehashing a receipt cannot substitute unrelated BOM or verifier bytes
        # behind the unchanged completed run's observation identities.
        for name in (
            "source-bom",
            "resolved-bom",
            "acceptance-policy-bytes",
            "completed-run",
        ):
            original_ref = receipt.root(name)
            replacement = self.store.put_bytes(
                canonical_json_bytes([] if name == "completed-run" else {}),
                media_type=original_ref.media_type,
            )
            records = {ref.identity: ref for ref in (*receipt.records, replacement)}
            forged = replace(
                receipt,
                roots=tuple(
                    (key, replacement if key == name else value)
                    for key, value in receipt.roots
                ),
                records=tuple(records[key] for key in sorted(records)),
            )
            forged_ref = self.store.put_manifest(
                forged.to_dict(), media_type="application/json"
            )
            joins = self.store.get_manifest(evidence)
            joins["retained_receipt_identity"] = forged.identity.uri
            forged_evidence = self.store.put_manifest(
                joins, media_type="application/json"
            )
            with self.subTest(root=name), self.assertRaises(ValueError):
                read_retained_unit_planning_inputs(
                    self.admission.source,
                    self.admission.run.source_bom.manifest,
                    self.admission.run.source_bom.plan,
                    self.admission.run.source_bom.component_locks,
                    forged_ref,
                    self.store,
                    planning_evidence_reference=forged_evidence,
                )
        before = set(self.store.iter_refs())
        self.assertEqual(projection.graph.index.unit_count, 1)
        definition = projection.graph.partition.definitions[0]
        self.assertEqual(definition.receipt_identity, self.admission.receipt.identity)
        self.assertEqual(definition.component_lock_identity, self.lock.identity)
        params = projection.parameters[0]
        license_input = next(
            value for value in params.evidence if value.role == "license"
        )
        self.assertEqual(
            self.store.get_bytes(license_input.blob),
            (self.fixture.fixture.source / "specs/LICENSE").read_bytes(),
        )
        self.assertFalse(projection.set_plan.to_dict()["release_authorized"])
        # Only the original live owner can use the completed LINK handoff.
        # A serialized proof remains an assertion even when its CAS bytes verify.
        handoff = link_retained_unit_packages(owner)
        handoff.require_current(owner)
        proof = self.store.get_manifest(handoff.reference)
        self.assertTrue(proof["link_complete"])
        self.assertFalse(proof["release_authorized"])
        with self.assertRaisesRegex(TypeError, "live-link-handoff-required"):
            owner.construct(object_root=self.fixture.fixture.base, link_handoff=proof)
        foreign = RetainedProjectUnitPackages(self.admission, **arguments)
        with self.assertRaisesRegex(ValueError, "link-handoff-selection-differs"):
            handoff.require_current(foreign)
        proof_path = self.store.path_for(handoff.reference)
        proof_bytes = proof_path.read_bytes()
        proof_path.write_bytes(b"{}")
        try:
            with self.assertRaises(BlobIntegrityError):
                handoff.require_current(owner)
        finally:
            proof_path.write_bytes(proof_bytes)
        handoff.require_current(owner)
        before = set(self.store.iter_refs())
        with self.assertRaisesRegex(TypeError, "live-admission-required"):
            RetainedProjectUnitPackages(self.admission.receipt.to_dict(), **arguments)
        path = self.fixture.fixture.source / "specs/application.package-unit.json"
        original = path.read_bytes()
        path.write_bytes(original + b"\n")
        with self.assertRaises(ValueError):
            owner.require_current()
        with self.assertRaises(ValueError):
            handoff.require_current(owner)
        self.assertEqual(set(self.store.iter_refs()), before)
        path.write_bytes(original)
        owner.require_current()
        handoff.require_current(owner)

    def unit_plan_arguments(self):
        root = self.fixture.fixture.base
        # These are unexecuted observations for a read-only plan, never a native
        # production fixture. Missing tool paths prove planning launches neither.
        return dict(
            pkg_tool=FreeBsdPkgToolBinding(
                root / "unexecuted-pkg",
                "0" * 64,
                "2.8.4",
                "FreeBSD:16:amd64",
                "freebsd:16:x86:64",
            ),
            tar_tool=NativeTarToolBinding(
                root / "unexecuted-tar", "1" * 64, "bsdtar 3.5.3 - libarchive 3.7.4"
            ),
            limits=RetainedProjectLimits(500, 64 * 1024**2, 128 * 1024**2),
            per_unit_limits=RetainedProjectLimits(500, 64 * 1024**2, 128 * 1024**2),
            max_units=10,
            max_dependencies=100,
            max_metadata_bytes=1024**2,
            max_staging_bytes=256 * 1024**2,
            max_transport_bytes=256 * 1024**2,
            max_archive_bytes=64 * 1024**2,
            max_total_archive_bytes=128 * 1024**2,
            max_descriptor_bytes=4096,
            max_descriptor_total_bytes=65536,
            max_index_bytes=4096,
            max_metadata_memory_bytes=1024**2,
            max_closure_steps=1000,
            max_closure_memory_bytes=1024**2,
        )

    def test_unit_plan_rejects_unlocked_copy_of_locked_json(self):
        self.package_provider = "pkg"
        request = RetainedUnitPackageRequest(
            "application",
            (),
            True,
            "specs/LICENSE",
            "retained-application",
            "1.0.0",
            "misc/retained-application",
            "/usr/local",
            (),
        )
        self.finish(
            unit_request=request,
            unit_request_path="specs/locked.json",
            copy_unit_request=True,
        )
        before = set(self.store.iter_refs())
        with self.assertRaisesRegex(
            ValueError, "locked-package-unit-specifications-required"
        ):
            RetainedProjectUnitPackages(self.admission, **self.unit_plan_arguments())
        self.assertEqual(set(self.store.iter_refs()), before)

    @classmethod
    def setUpClass(cls):
        commands.RetainedProjectExecutionTests.setUpClass()
        cls.addClassCleanup(commands.RetainedProjectExecutionTests.doClassCleanups)

    def setUp(self):
        self.fixture = completed.RetainedProjectCompletionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def finish(
        self,
        *,
        include_specification=True,
        collision=False,
        empty_directory=False,
        unit_request=None,
        unit_request_path="specs/application.package-unit.json",
        copy_unit_request=False,
        documentation_path="docs.txt",
        tree_outputs=False,
        selected_tree_policy=True,
        registered_component=None,
    ):
        f = self.fixture.fixture
        specification = b"# Retained package\nPreserve original bytes and behavior.\n"
        (f.source / "specs").mkdir()
        (f.source / "specs/retained-package.md").write_bytes(specification)
        extra = ()
        if unit_request is not None:
            content = canonical_json_bytes(unit_request.to_dict())
            (f.source / unit_request_path).write_bytes(content)
            if copy_unit_request:
                (f.source / "specs/copied.package-unit.json").write_bytes(content)
            (f.source / "specs/LICENSE").write_bytes(
                b"Fixture copyright. SPDX-License-Identifier: BSD-2-Clause\n"
            )
            extra = (
                ContentReference(
                    "specification",
                    unit_request_path,
                    ContentIdentity.parse_uri(
                        "sha256:" + hashlib.sha256(content).hexdigest()
                    ),
                ),
            )
        self.lock = package_lock(
            specification,
            provider=getattr(self, "package_provider", "zip"),
            extra_specifications=extra,
        )
        if registered_component is not None:
            self.lock = self.registered_unit_lock(
                f.source, unit_request_path, registered_component
            )
        self.fixture.locks = (self.lock,)
        roles = {
            "src": "source",
            "tools": "toolchain",
            "test-policy.json": "test-policy",
        }
        if include_specification:
            roles["specs"] = "specification"
        if registered_component:
            roles["components"] = "specification"
        f.manifest = discover_retained_project_inputs(
            f.source,
            roles,
            f.manifest.limits,
            executable_paths=(f.runner_path,),
            tree_policy=(
                RetainedProjectTreePolicy()
                if tree_outputs and selected_tree_policy
                else None
            ),
        )
        path = f.source / ACCEPTANCE_POLICY_PATH
        policy = RetainedProjectAcceptancePolicy.from_dict(
            json.loads(path.read_bytes())
        )
        policy = replace(policy, component_locks=(self.lock.identity,))
        content = canonical_json_bytes(policy.to_dict())
        path.write_bytes(content)
        profile = replace(
            f.plan.profile,
            toolchain_identity=f.manifest.role_identity({"sdk", "toolchain"}),
            dependency_identity=f.manifest.role_identity({"dependency"}),
            acceptance_policy_identity=ContentIdentity.parse_uri(
                "sha256:" + hashlib.sha256(content).hexdigest()
            ),
        )
        if tree_outputs:
            actions = list(profile.actions)
            actions[0] = replace(
                actions[0],
                argv=("export-tree", "{output_root}/app.bin"),
                outputs=("app.bin", "empty", "z-directory", "z-hardlink", "z-link"),
            )
            profile = replace(
                profile,
                actions=tuple(actions),
                package_inventory=(
                    "app.bin",
                    "docs.txt",
                    "empty",
                    "z-directory",
                    "z-hardlink",
                    "z-link",
                ),
            )
        if documentation_path != "docs.txt":
            actions = list(profile.actions)
            actions[2] = replace(
                actions[2],
                outputs=(documentation_path,),
                argv=("docs", "{output_root}/" + documentation_path),
            )
            actions[3] = replace(
                actions[3], argv=("check", "{output_root}/" + documentation_path)
            )
            profile = replace(
                profile,
                actions=tuple(actions),
                package_inventory=("app.bin", documentation_path),
            )
        if collision:
            actions = list(profile.actions)
            actions[2] = replace(
                actions[2],
                outputs=(".LITERATE-AI",),
                argv=("docs", "{output_root}/.LITERATE-AI"),
            )
            actions[3] = replace(
                actions[3], argv=("check", "{output_root}/.LITERATE-AI")
            )
            profile = replace(
                profile,
                actions=tuple(actions),
                package_inventory=("app.bin", ".LITERATE-AI"),
            )
        if empty_directory:
            actions = list(profile.actions)
            actions[2] = replace(
                actions[2],
                outputs=("empty-dir",),
                argv=("directory", "{output_root}/empty-dir"),
            )
            actions[3] = replace(actions[3], argv=("empty",))
            profile = replace(
                profile,
                actions=tuple(actions),
                package_inventory=("app.bin", "empty-dir"),
            )
        f.plan = replace(
            f.plan,
            profile=profile,
            component_locks=(self.lock.identity,),
            manifest_identity=f.manifest.identity,
        )
        f.current = f.plan
        self.fixture.graph = project_retained_project_managed_graph(
            f.plan, self.fixture.locks, version="1.0.0"
        )
        workspace, _ = self.fixture.finish()
        self.store = FileSystemCAS(f.base / "package-cas")
        self.admission = admit_retained_project(workspace, self.store)
        return workspace

    def registered_unit_lock(self, source, declaration, nested_captured):
        """Real public lock authority for a nested retained Component."""
        repository = Path(__file__).resolve().parents[2]
        component = source / "components/retained-package"
        (component / "specs").mkdir(parents=True)
        original = source / declaration
        (component / "specs/application.package-unit.json").write_bytes(
            original.read_bytes()
        )
        if nested_captured:
            original.rename(original.with_name("copied.package-unit.json"))
        # An equal-content original-root copy is not the registered declaration.
        (component / "specs/retained-package.md").write_text(
            "# Retained application\n\n"
            "### Requirement: Preserve original native behavior\n\n"
            "The application SHALL preserve its retained C source "
            "and original tests.\n\n"
            "#### Scenario: Complete the retained lifecycle\n\n"
            "- **WHEN** original tests and independent acceptance run\n"
            "- **THEN** exact accepted products retain original source provenance\n"
        )
        document = {
            "namespace": "qualification",
            "version": "1.0.0",
            "display_name": "Retained application",
            "profiles": ["retained-contract"],
            "sample": True,
            "provides": [
                {"name": "qualification.retained-application", "version": "1.0.0"},
                {"name": "sample.portable-app", "version": "1.0.0"},
            ],
            "requires": [],
            "specification_provider": "openspec",
            "specification_roots": [
                "specs/application.package-unit.json",
                "specs/retained-package.md",
            ],
            "authoring_inputs": [
                {
                    "kind": "specification-to-source-skill",
                    "uri": (
                        "skills/specification-to-source/"
                        "portable-application-implementation/SKILL.md"
                    ),
                }
            ],
            "workflow_definition": "workflows/sample-host.md",
            "routing_policy": "routing/sample-host.json",
            "flavor_slots": [
                {
                    "slot_id": name,
                    "axis": axis,
                    "cardinality": "exactly-one",
                    "capability_contract": "sample.portable-app",
                }
                for name, axis in [("os", "platform.os"), ("packaging", "packaging")]
            ],
            "entrypoints": [],
            "acceptance_contracts": [],
            "source_dependencies": [],
        }
        (component / "component.md").write_bytes(
            render_authoring_markdown(
                document,
                "# Retained application\n\n"
                "Preserve original native bytes and acceptance.\n",
            )
        )
        for leaf in ("os-freebsd", "package-pkg"):
            shutil.copytree(repository / "flavors" / leaf, source / "flavors" / leaf)
        for relative in [
            "workflows/sample-host.md",
            "routing/sample-host.json",
            "skills/specification-to-source/portable-application-implementation/SKILL.md",
            "skills/specification-to-source/portable-specification-planning/SKILL.md",
            "skills/agent/package-artifacts/SKILL.md",
        ]:
            path = source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(repository / relative, path)
        definition = ProjectDefinition.from_dict(
            json.loads((repository / "literate.project.json").read_text())
        )
        (source / "SKILL.md").write_text(
            "---\nname: retained-package-fixture\n"
            "description: Preserve original native source and acceptance.\n"
            "---\n# Retained source fixture\n"
        )
        definition = replace(
            definition,
            component_roots=("components",),
            flavor_roots=("flavors",),
            skill_roots=("skills",),
            workflow_roots=("workflows",),
            routing_roots=("routing",),
            lifecycle_driver=None,
            test_receipt=None,
            test_receipt_policy=None,
            default_flavor_selectors=(),
            component_flavor_selectors={},
        )
        (source / "literate.project.json").write_text(json.dumps(definition.to_dict()))
        selection = RepositoryParentSelection.root()
        FilesystemRepositoryLineageStore(source).replace(
            selection, RepositoryLineage(selection, (), ()), expected_absent=True
        )
        output, errors = io.StringIO(), io.StringIO()
        status = cli_main(
            [
                "--json",
                "lock",
                str(component),
                "--target",
                "freebsd",
                "--flavor",
                "+os-freebsd",
                "--flavor",
                "+package-pkg",
            ],
            stdout=output,
            stderr=errors,
        )
        self.assertEqual(status, 0, output.getvalue() + errors.getvalue())
        return current_component_lock(component)

    def provider(self, admission=None, **changes):
        return RetainedProjectZipPackage(
            admission or self.admission,
            **{
                "provider_lock_identity": self.lock.identity,
                "provider_revision": locked.identity("flavor-zip"),
                **changes,
            },
        )

    def test_real_archive_is_repeatable_and_contains_original_provenance(self):
        self.finish()
        before = set(self.store.iter_refs())
        provider = self.provider()
        executable = next(
            item for item in provider.plan.inputs if item.path == "app.bin"
        )
        self.assertTrue(executable.executable)
        self.assertEqual(set(self.store.iter_refs()), before)
        result = provider.construct(self.fixture.fixture.base)
        self.assertEqual(result, provider.construct(self.fixture.fixture.base))
        self.assertEqual(
            RetainedProjectPackagePlan.from_dict(provider.plan.to_dict()), provider.plan
        )
        self.assertEqual(
            RetainedProjectPackageResult.from_dict(result.to_dict()), result
        )
        with self.assertRaises(ValueError):
            PackagePlan.from_dict(provider.plan.to_dict())
        with self.assertRaises(ValueError):
            PackageResult.from_dict(result.to_dict())
        content = self.store.get_bytes(result.artifacts[0].blob)
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = archive.namelist()
            self.assertIn("app.bin", names)
            self.assertNotIn("test-result.json", names)
            self.assertTrue(
                any(v.startswith(".literate-ai/specifications/") for v in names)
            )
            self.assertEqual(
                archive.read(".literate-ai/source-bom.json"),
                self.admission.run.source_bom.content,
            )
            self.assertEqual(
                archive.read(".literate-ai/resolved-bom.json"),
                self.admission.run.resolved_bom.content,
            )
            self.assertEqual(
                json.loads(archive.read(".literate-ai/receipt.json")),
                self.admission.receipt.to_dict(),
            )
            for reference in self.admission.receipt.records:
                self.assertEqual(
                    archive.read(f".literate-ai/records/{reference.digest}.blob"),
                    self.store.get_bytes(reference),
                )
            target = self.fixture.fixture.base / "retained-archive-app"
            target.write_bytes(archive.read("app.bin"))
            target.chmod(0o755)
        observed = subprocess.run(
            (str(target), "echo"),
            input=b"archive\x00payload",
            capture_output=True,
            check=True,
            timeout=10,
        )
        self.assertEqual(observed.stdout, b"archive\x00payload")
        self.assertFalse(list(self.fixture.fixture.base.glob("retained-package-*")))
        for value in (provider.plan, result):
            reference = BlobRef(
                value.identity.digest,
                len(canonical_json_bytes(value.to_dict())),
                media_type="application/json",
            )
            self.assertEqual(self.store.get_manifest(reference), value.to_dict())

    def test_linked_payload_cannot_be_silently_flattened(self):
        self.finish()
        manifest = self.admission.payload_manifest
        original = manifest.members[0]
        alias = replace(original, path="zz-hardlink", hardlink_target=original.path)
        from literate_ai.contracts.retained_project import RetainedProjectTreePolicy

        linked = replace(
            manifest,
            roots=tuple(sorted((*manifest.roots, alias.path))),
            members=tuple(sorted((*manifest.members, alias), key=lambda m: m.path)),
            tree_policy=RetainedProjectTreePolicy(),
        )
        with patch.object(
            type(self.admission),
            "payload_manifest",
            new_callable=PropertyMock,
            return_value=linked,
        ):
            with self.assertRaisesRegex(ValueError, "payload-links-not-qualified"):
                self.provider()
            with self.assertRaisesRegex(ValueError, "payload-links-not-qualified"):
                self.admission.materialize_payload(
                    self.fixture.fixture.base / "linked-payload"
                )
        self.assertFalse((self.fixture.fixture.base / "linked-payload").exists())

    def test_wrong_provider_or_lock_and_decoded_receipt_do_not_grant_packaging(self):
        self.finish()
        for change in (
            {"provider_revision": locked.identity("wrong-provider")},
            {"provider_lock_identity": locked.identity("foreign-lock")},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.provider(**change)
        with self.assertRaises(TypeError):
            self.provider(self.admission.receipt)

    def test_missing_authored_specification_and_reserved_payload_paths_refuse(self):
        self.finish(include_specification=False)
        with self.assertRaisesRegex(ValueError, "authored-specification-missing"):
            self.provider()

    def test_case_aliased_evidence_collision_refuses(self):
        self.finish(collision=True)
        with self.assertRaisesRegex(ValueError, "reserved-evidence-path"):
            self.provider()

    def test_empty_selected_payload_directories_are_not_silently_dropped(self):
        self.finish(empty_directory=True)
        self.assertIn("empty-dir", self.admission.payload_manifest.directories)
        with self.assertRaisesRegex(
            ValueError, "empty-payload-directories-not-qualified"
        ):
            self.provider()

    def test_scratch_retirement_and_relocated_cas_preserve_package_evidence(self):
        workspace = self.finish()
        provider = self.provider()
        result = provider.construct(self.fixture.fixture.base)
        _remove_tree(workspace.root)
        moved_root = self.store.root.with_name("relocated-package-cas")
        self.store.root.rename(moved_root)
        moved = self.admission.relocated(FileSystemCAS(moved_root, create=False))
        reopened = self.provider(moved)
        self.assertEqual(reopened.plan, provider.plan)
        self.assertEqual(reopened.verify(result), result)

    def test_stale_original_authority_blocks_construction_and_verification(self):
        self.finish()
        provider = self.provider()
        result = provider.construct(self.fixture.fixture.base)
        (self.fixture.fixture.source / "src/main.txt").write_bytes(b"changed")
        for action in (
            lambda: provider.construct(self.fixture.fixture.base),
            lambda: provider.verify(result),
        ):
            with self.assertRaises(ValueError):
                action()

    def test_rehashed_archive_tampering_fails_independent_member_verification(self):
        self.finish()
        provider = self.provider()
        result = provider.construct(self.fixture.fixture.base)
        stream = io.BytesIO()
        with (
            zipfile.ZipFile(
                io.BytesIO(self.store.get_bytes(result.artifacts[0].blob))
            ) as original,
            zipfile.ZipFile(stream, "w") as changed,
        ):
            for info in original.infolist():
                body = original.read(info)
                changed.writestr(
                    info,
                    b"X" * len(body) if info.filename == "app.bin" else body,
                    compresslevel=9,
                )
        reference = self.store.put_bytes(
            stream.getvalue(), media_type="application/zip"
        )
        corrupted = replace(
            result, artifacts=(replace(result.artifacts[0], blob=reference),)
        )
        with self.assertRaises(PackagingError):
            provider.verify(corrupted)
