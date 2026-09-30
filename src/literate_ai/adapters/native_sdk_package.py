"""Retain verified SDK inputs as package resources without granting execution."""

from __future__ import annotations

import hashlib
import shutil
import stat
from pathlib import Path

from literate_ai._filesystem import require_safe_directory, stat_is_link_or_reparse
from literate_ai.adapters.native_sdk_consumer import NativeSdkConsumerInputs
from literate_ai.contracts.executable_components.packages import (
    PackageFileKind,
    PackageInput,
    RuntimeRequirement,
    RuntimeRequirementKind,
)
from literate_ai.contracts.identity import ContentIdentity, canonical_json_bytes
from literate_ai.storage import BlobRef


class NativeSdkPackageResources:
    """One exact locked closure, retaining portable manifests and original file modes.

    Packaging reads live verified producer custody. Verification of a completed
    directory uses the retained exact file table and requires no producer store.
    Neither operation authorizes native code or supplies external system libraries.
    """

    def __init__(
        self,
        owner: NativeSdkConsumerInputs,
        *,
        component_lock_identity: ContentIdentity,
        root_revision: ContentIdentity,
        target_identity: ContentIdentity,
        consumer_revisions: tuple[ContentIdentity, ...] | None = None,
    ) -> None:
        if not isinstance(owner, NativeSdkConsumerInputs):
            raise TypeError("SDK package resources require live consumer inputs")
        if owner.snapshot.authority.lock.identity != component_lock_identity:
            raise ValueError("SDK package lock differs from consumer inputs")
        self._owner = owner
        self.bindings = owner.for_scope(root_revision)
        if consumer_revisions is not None:
            known = {
                node.revision.identity for node in owner.snapshot.authority.lock.nodes
            }
            if (
                len(set(consumer_revisions)) != len(consumer_revisions)
                or not set(consumer_revisions) <= known
            ):
                raise ValueError("SDK package consumers differ from the locked project")
            self.bindings = tuple(
                b
                for b in self.bindings
                if b.build.selection.component_revision in consumer_revisions
            )
        selected_ids = {b.identity for b in self.bindings}
        self.revocation_sources = tuple(
            item
            for item in owner.execution_revocation_sources(root_revision)
            if item[0] in selected_ids
        )
        self._metadata: dict[BlobRef, bytes] = {}
        self._sources = {}
        resources = []
        requirements = []
        consumers = []
        for binding in self.bindings:
            sdk = binding.build.product.snapshot
            consumer_revision = binding.build.selection.component_revision
            if sdk.target_identity != target_identity:
                raise ValueError("SDK package target differs from consumer inputs")
            prefix = f"native-sdks/{binding.identity.digest}"
            requirements.extend(
                (
                    RuntimeRequirement(
                        f"sdk-files-{binding.identity.digest}",
                        RuntimeRequirementKind.SHARED_LIBRARY,
                        "retained native SDK file set",
                        sdk.identity,
                        True,
                    ),
                    RuntimeRequirement(
                        f"sdk-loader-{binding.identity.digest}",
                        RuntimeRequirementKind.ENVIRONMENT,
                        "native SDK loader environment requiring fresh host validation",
                        ContentIdentity.parse_uri(
                            binding.build.product.runtime_observation.identity
                        ),
                        False,
                    ),
                )
            )
            for item in sdk.files:
                resources.append(
                    PackageInput(
                        f"{prefix}/sdk/{item.path}",
                        "native-sdk-file",
                        PackageFileKind.RESOURCE,
                        binding.identity,
                        target_identity,
                        item.blob,
                        item.executable,
                    )
                )
                self._sources.setdefault(item.blob, binding)
            for name, value in (
                ("snapshot", sdk.to_dict()),
                ("binding", binding.to_dict()),
            ):
                resources.append(
                    self._manifest(
                        f"{prefix}/{name}.json",
                        value,
                        binding.identity,
                        target_identity,
                    )
                )
            consumers.append(
                {
                    "consumer_revision": consumer_revision.to_dict(),
                    "input_identity": binding.identity.to_dict(),
                    "import_root": f"{prefix}/sdk/{sdk.import_root}",
                    "snapshot": f"{prefix}/snapshot.json",
                    "binding": f"{prefix}/binding.json",
                }
            )
        if consumers:
            resources.append(
                self._manifest(
                    "native-sdks/inputs.json",
                    {
                        "schema": "literate-ai/native-sdk-package-inputs@1",
                        "component_lock_identity": component_lock_identity.to_dict(),
                        "root_revision": root_revision.to_dict(),
                        "target_identity": target_identity.to_dict(),
                        "consumers": sorted(
                            consumers, key=lambda item: item["import_root"]
                        ),
                    },
                    component_lock_identity,
                    target_identity,
                )
            )
        self.inputs = tuple(sorted(resources, key=lambda item: item.path))
        self.runtime_requirements = tuple(
            sorted(requirements, key=lambda item: item.requirement_id)
        )
        owner.require_unchanged()

    def _manifest(self, path, value, source, target) -> PackageInput:
        content = canonical_json_bytes(value)
        blob = BlobRef(hashlib.sha256(content).hexdigest(), len(content))
        self._metadata[blob] = content
        return PackageInput(
            path, "native-sdk-manifest", PackageFileKind.RESOURCE, source, target, blob
        )

    def owns_blob(self, reference: BlobRef) -> bool:
        return reference in self._metadata or reference in self._sources

    def read_blob(self, reference: BlobRef) -> bytes:
        if reference in self._metadata:
            return self._metadata[reference]
        if reference not in self._sources:
            raise ValueError("blob is not a declared SDK package resource")
        return self._owner.read_input_blob(self._sources[reference], reference)

    def materialize(self, package_root: Path) -> None:
        self._owner.require_unchanged()
        require_safe_directory(package_root)
        if not self.inputs:
            return
        namespace = package_root / "native-sdks"
        # mkdir without exist_ok refuses an existing file, directory or dangling link.
        namespace.mkdir()
        try:
            for item in self.inputs:
                path = package_root / item.path
                path.parent.mkdir(parents=True, exist_ok=True)
                require_safe_directory(path.parent)
                with path.open("xb") as stream:
                    stream.write(self.read_blob(item.blob))
                path.chmod(0o755 if item.executable else 0o644)
            self._owner.require_unchanged()
            self.verify_materialized(package_root)
        except BaseException:
            shutil.rmtree(namespace)
            raise

    def verify_materialized(self, package_root: Path) -> None:
        """Verify this exact SDK resource table after relocation, without CAS access."""
        require_safe_directory(package_root)
        if not self.inputs:
            return
        namespace = package_root / "native-sdks"
        require_safe_directory(namespace)
        expected = {item.path: item for item in self.inputs}
        found = set()
        for path in namespace.rglob("*"):
            info = path.lstat()
            if stat_is_link_or_reparse(info):
                raise ValueError(
                    "SDK package contains a symbolic link or reparse point"
                )
            if stat.S_ISDIR(info.st_mode):
                require_safe_directory(path)
                continue
            name = path.relative_to(package_root).as_posix()
            item = expected.get(name)
            if item is None or not stat.S_ISREG(info.st_mode):
                raise ValueError("SDK package contains an unexpected file")
            require_safe_directory(path.parent)
            content = path.read_bytes()
            if (
                len(content) != item.blob.size
                or hashlib.sha256(content).hexdigest() != item.blob.digest
                or bool(info.st_mode & 0o111) != item.executable
            ):
                raise ValueError("SDK package file bytes or mode changed")
            found.add(name)
        if found != set(expected):
            raise ValueError("SDK package is missing declared files")
