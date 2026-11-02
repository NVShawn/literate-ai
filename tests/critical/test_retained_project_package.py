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
from unittest.mock import PropertyMock, patch

from literate_ai.adapters.freebsd_packaging import FreeBsdPkgToolBinding
from literate_ai.adapters.native_tar_archives import NativeTarToolBinding
from literate_ai.adapters.retained_project_acceptance import ACCEPTANCE_POLICY_PATH
from literate_ai.adapters.retained_project_inputs import (
    _remove_tree,
    discover_retained_project_inputs,
)
from literate_ai.adapters.retained_project_package import RetainedProjectZipPackage
from literate_ai.adapters.retained_project_receipt import admit_retained_project
from literate_ai.adapters.retained_project_unit_packages import (
    RetainedProjectUnitPackages,
)
from literate_ai.adapters.retained_unit_results import read_retained_unit_results
from literate_ai.application.packaging import PackagingError
from literate_ai.contracts import (
    BlobRef,
    ContentIdentity,
    ContentReference,
    canonical_json_bytes,
)
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
from literate_ai.contracts.retained_project import RetainedProjectLimits
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
from literate_ai.contracts.retained_unit_requests import RetainedUnitPackageRequest
from literate_ai.storage.cas import FileSystemCAS
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
        path = self.fixture.fixture.source / "specs/LICENSE"
        original = path.read_bytes()
        path.write_bytes(original + b"changed license\n")
        try:
            with self.assertRaises(ValueError):
                owner.verify_set(reference, object_root=root)
            self.store.verify(first[0].archive)
        finally:
            path.write_bytes(original)
        owner.require_current()

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
        with self.assertRaisesRegex(TypeError, "live-admission-required"):
            RetainedProjectUnitPackages(self.admission.receipt.to_dict(), **arguments)
        path = self.fixture.fixture.source / "specs/application.package-unit.json"
        original = path.read_bytes()
        path.write_bytes(original + b"\n")
        with self.assertRaises(ValueError):
            owner.require_current()
        self.assertEqual(set(self.store.iter_refs()), before)
        path.write_bytes(original)
        owner.require_current()

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
        self.fixture.locks = (self.lock,)
        roles = {
            "src": "source",
            "tools": "toolchain",
            "test-policy.json": "test-policy",
        }
        if include_specification:
            roles["specs"] = "specification"
        f.manifest = discover_retained_project_inputs(
            f.source,
            roles,
            f.manifest.limits,
            executable_paths=(f.runner_path,),
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
            acceptance_policy_identity=ContentIdentity.parse_uri(
                "sha256:" + hashlib.sha256(content).hexdigest()
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
