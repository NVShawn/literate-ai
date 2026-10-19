"""Project live retained custody through the existing exact ZIP provider."""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

from literate_ai.adapters.exclusive_directory import directory_node
from literate_ai.adapters.packaging import NativeZipPackageAdapter
from literate_ai.adapters.retained_project_inputs import _remove_tree
from literate_ai.adapters.retained_project_receipt import AdmittedRetainedProject
from literate_ai.application.packaging import verify_package_result
from literate_ai.contracts import BlobRef, ContentIdentity, canonical_json_bytes
from literate_ai.contracts.executable_components.packages import (
    PackageFileKind,
    PackageInput,
)
from literate_ai.contracts.flavors import FlavorAxis
from literate_ai.contracts.retained_project_package import RetainedProjectPackagePlan

_EVIDENCE = ".literate-ai"


class RetainedProjectZipPackage:
    """No source promotion, native runtime execution or publication authority.

    The package plan/result use a distinct origin while the existing ZIP provider
    owns construction and independent archive verification. This owner requires
    live local admission even when a parsed plan/result has matching identities.
    """

    def __init__(self, admission, *, provider_lock_identity, provider_revision):
        if not isinstance(admission, AdmittedRetainedProject):
            raise TypeError("retained.package.live-admission-required")
        self.admission = admission
        admission.require_current()
        run = admission.run
        locks = run.source_bom.component_locks
        lock = next((v for v in locks if v.identity == provider_lock_identity), None)
        if lock is None:
            raise ValueError("retained.package.selected-provider-lock-required")
        node = next(v for v in lock.nodes if v.revision.identity == lock.root_revision)
        if not any(
            value.value == "zip" and value.flavor_revision == provider_revision
            for slot in node.target_flavor_selection.slots
            if slot.slot.axis is FlavorAxis.PACKAGING
            for value in slot.selected
        ):
            raise ValueError("retained.package.locked-zip-provider-required")
        if any(
            v.target_profile_identity != lock.target_profile_identity for v in locks
        ):
            raise ValueError("retained.package.mixed-target-locks")
        self.adapter = NativeZipPackageAdapter()
        self._metadata = {}
        target = lock.target_profile_identity
        inputs = []
        payload = admission.payload_manifest
        ancestors = set()
        for member in payload.members:
            parent = member.path.rpartition("/")[0]
            while parent:
                ancestors.add(parent)
                parent = parent.rpartition("/")[0]
        if set(payload.directories) != ancestors:
            raise ValueError("retained.package.empty-payload-directories-not-qualified")

        def metadata(value):
            content = canonical_json_bytes(value)
            reference = BlobRef(
                hashlib.sha256(content).hexdigest(),
                len(content),
                media_type="application/json",
            )
            self._metadata[reference] = content
            return reference

        def append(path, role, reference, source, *, executable=False, artifact=False):
            inputs.append(
                PackageInput(
                    path,
                    role,
                    PackageFileKind.ARTIFACT if artifact else PackageFileKind.RESOURCE,
                    source,
                    target,
                    reference,
                    executable,
                )
            )

        for member in payload.members:
            if member.path.split("/", 1)[0].casefold() == _EVIDENCE:
                raise ValueError("retained.package.reserved-evidence-path")
            append(
                member.path,
                "retained-payload",
                BlobRef(member.sha256, member.size),
                run.output_manifest.identity,
                executable=member.mode == 0o755,
                artifact=True,
            )
        receipt = metadata(admission.receipt.to_dict())
        append(
            f"{_EVIDENCE}/receipt.json",
            "retained-receipt",
            receipt,
            admission.receipt.identity,
        )
        for name, reference in admission.receipt.roots:
            append(
                f"{_EVIDENCE}/{name}.json",
                "retained-evidence",
                reference,
                ContentIdentity.parse_uri(reference.identity),
            )
        for reference in admission.receipt.records:
            append(
                f"{_EVIDENCE}/records/{reference.digest}.blob",
                "retained-run-record",
                reference,
                ContentIdentity.parse_uri(reference.identity),
            )
        source_files = {
            m.sha256: m
            for m in run.source_bom.manifest.members
            if m.symlink_target is None
        }
        specifications = set()
        for selected in locks:
            reference = metadata(selected.to_dict())
            append(
                f"{_EVIDENCE}/locks/{selected.identity.digest}.json",
                "component-lock",
                reference,
                selected.identity,
            )
            for selected_node in selected.nodes:
                for specification in selected_node.revision.specifications:
                    member = source_files.get(specification.identity.digest)
                    if member is None:
                        raise ValueError(
                            "retained.package.authored-specification-missing"
                        )
                    if member.sha256 in specifications:
                        continue
                    specifications.add(member.sha256)
                    append(
                        f"{_EVIDENCE}/specifications/{member.sha256}.md",
                        "component-specification",
                        BlobRef(member.sha256, member.size),
                        specification.identity,
                    )
        self._plan = RetainedProjectPackagePlan(
            run.source_bom.plan.identity,
            admission.receipt.identity,
            provider_lock_identity,
            provider_revision,
            target,
            self.adapter.packager_identity,
            tuple(sorted(inputs, key=lambda v: v.path)),
        )
        admission.require_current()

    @property
    def plan(self):
        return self._plan

    def require_current(self):
        self.admission.require_current()
        if self.plan.packager_identity != self.adapter.packager_identity:
            raise ValueError("retained.package.packager-changed")
        for item in self.plan.inputs:
            if item.blob not in self._metadata:
                self.admission.store.verify(item.blob)

    def construct(self, workspace):
        """Return verified result and retain the archive in the existing CAS."""
        self.require_current()
        workspace = Path(workspace).absolute()
        directory_node(workspace)
        for reference, content in self._metadata.items():
            if (
                self.admission.store.put_bytes(content, media_type=reference.media_type)
                != reference
            ):
                raise ValueError("retained.package.metadata-retention-mismatch")
        stage = Path(tempfile.mkdtemp(prefix="retained-package-", dir=workspace))
        owned = directory_node(stage)
        try:
            for item in self.plan.inputs:
                path = stage / item.path
                path.parent.mkdir(parents=True, exist_ok=True)
                self.admission.store.copy_to(item.blob, path)
                path.chmod(0o755 if item.executable else 0o644)
            self.require_current()
            result = self.adapter.package(self.plan, materialized_root=stage)
            for artifact in result.artifacts:
                reference = self.admission.store.put_bytes(
                    self.adapter.read_created_blob(artifact.blob),
                    media_type=artifact.blob.media_type,
                )
                if reference != artifact.blob:
                    raise ValueError("retained.package.archive-retention-mismatch")
            self.verify(result)
            self.admission.store.put_manifest(self.plan.to_dict())
            self.admission.store.put_manifest(result.to_dict())
            self.require_current()
            return result
        finally:
            if stage.exists() and directory_node(stage) == owned:
                _remove_tree(stage)

    def verify(self, result):
        """Reopen exact source/CAS and inspect the archive; execute nothing."""
        self.require_current()
        verify_package_result(
            self.plan, result, read_blob=self.admission.store.get_bytes
        )
        self.adapter.verify_bytes(
            result, self.admission.store.get_bytes(result.artifacts[0].blob)
        )
        self.require_current()
        return result
