"""Join explicit tree package inputs to completed live SDK producer ownership."""

from __future__ import annotations

import hashlib
from dataclasses import replace

from literate_ai.adapters.native_sdk_consumer import NativeSdkConsumerInputs
from literate_ai.contracts.executable_components.packages import (
    PackageFileKind,
    PackageInput,
    PackagePlan,
    RuntimeRequirement,
    RuntimeRequirementKind,
)
from literate_ai.contracts.identity import (
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.contracts.native_sdk_tree_products import NativeSdkTreeSnapshot
from literate_ai.contracts.native_sdk_trees import NativeSdkTree
from literate_ai.contracts.package_tree_inputs import PackageTreeInput
from literate_ai.contracts.tree_packages import TreePackagePlan
from literate_ai.storage import BlobRef


class NativeSdkTreePackageResources:
    """Separate capability; legacy file-only package resources cannot consume it."""

    def __init__(
        self, owner, *, component_lock_identity, root_revision, target_identity
    ):
        if not isinstance(owner, NativeSdkConsumerInputs):
            raise TypeError("SDK tree package requires actual live consumer inputs")
        if owner.snapshot.authority.lock.identity != component_lock_identity:
            raise ValueError("SDK tree package lock differs from live owner")
        owner.require_unchanged()
        self.owner = owner
        self.component_lock_identity = component_lock_identity
        self.root_revision = root_revision
        self.target_identity = target_identity
        self.bindings = owner.for_scope(root_revision)
        if not self.bindings:
            raise ValueError("SDK tree package requires selected completed products")
        metadata, sources, trees, requirements, consumers = {}, {}, [], [], []
        inputs = []

        def document(path, value, source):
            content = canonical_json_bytes(value)
            blob = BlobRef(hashlib.sha256(content).hexdigest(), len(content))
            metadata[blob] = content
            inputs.append(
                PackageInput(
                    path,
                    "native-sdk-manifest",
                    PackageFileKind.RESOURCE,
                    source,
                    target_identity,
                    blob,
                )
            )

        for binding in self.bindings:
            sdk = binding.build.product.snapshot
            if sdk.target_identity != target_identity:
                raise ValueError(
                    "SDK tree package target differs from completed product"
                )
            prefix = "native-sdks/" + binding.identity.digest
            if isinstance(sdk, NativeSdkTreeSnapshot):
                tree = sdk.tree
            else:
                directories = set()
                for value in sdk.files:
                    parent = value.path.rpartition("/")[0]
                    while parent:
                        directories.add(parent)
                        parent = parent.rpartition("/")[0]
                tree = NativeSdkTree(tuple(sorted(directories)), sdk.files, ())
            value = PackageTreeInput(
                prefix + "/sdk",
                "native-sdk-tree",
                binding.identity,
                target_identity,
                tree,
            )
            trees.append(value)
            for item in sdk.files:
                sources.setdefault(item.blob, binding)
            document(prefix + "/snapshot.json", sdk.to_dict(), binding.identity)
            document(prefix + "/binding.json", binding.to_dict(), binding.identity)
            requirements.extend(
                (
                    RuntimeRequirement(
                        "sdk-files-" + binding.identity.digest,
                        RuntimeRequirementKind.SHARED_LIBRARY,
                        "retained complete native SDK tree",
                        sdk.identity,
                        True,
                    ),
                    RuntimeRequirement(
                        "sdk-loader-" + binding.identity.digest,
                        RuntimeRequirementKind.ENVIRONMENT,
                        "native SDK loader environment requiring fresh host validation",
                        ContentIdentity.parse_uri(
                            binding.build.product.runtime_observation.identity
                        ),
                        False,
                    ),
                )
            )
            consumers.append(
                {
                    "consumer_revision": (
                        binding.build.selection.component_revision.to_dict()
                    ),
                    "input_identity": binding.identity.to_dict(),
                    "tree_input_identity": value.identity.to_dict(),
                    "import_root": prefix + "/sdk/" + sdk.import_root,
                    "snapshot": prefix + "/snapshot.json",
                    "binding": prefix + "/binding.json",
                }
            )
        document(
            "native-sdks/tree-inputs.json",
            {
                "schema": "literate-ai/native-sdk-tree-package-inputs@1",
                "component_lock_identity": component_lock_identity.to_dict(),
                "root_revision": root_revision.to_dict(),
                "target_identity": target_identity.to_dict(),
                "consumers": sorted(consumers, key=lambda v: v["import_root"]),
            },
            component_lock_identity,
        )
        self.metadata_inputs = tuple(sorted(inputs, key=lambda v: v.path))
        self.trees = tuple(sorted(trees, key=lambda v: v.prefix))
        self.runtime_requirements = tuple(
            sorted(requirements, key=lambda v: v.requirement_id)
        )
        self._metadata, self._sources = metadata, sources
        self._projection_identity = canonical_identity(self._projection())
        self.require_current()

    def _projection(self):
        return {
            "lock": self.component_lock_identity.to_dict(),
            "root": self.root_revision.to_dict(),
            "target": self.target_identity.to_dict(),
            "trees": [v.to_dict() for v in self.trees],
            "metadata": [v.to_dict() for v in self.metadata_inputs],
            "requirements": [v.to_dict() for v in self.runtime_requirements],
        }

    def require_current(self):
        if canonical_identity(self._projection()) != self._projection_identity:
            raise ValueError("SDK tree package projection changed after live selection")
        self.owner.require_unchanged()
        if (
            self.owner.snapshot.authority.lock.identity != self.component_lock_identity
            or self.owner.for_scope(self.root_revision) != self.bindings
        ):
            raise ValueError("SDK tree package completed product ownership changed")

    def populate_store(self, store):
        """Retain selected verified bytes through the actual live owner."""
        self.require_current()
        for reference, content in self._metadata.items():
            if store.put_bytes(content, media_type=reference.media_type) != reference:
                raise ValueError("SDK tree package metadata bytes changed")
        for reference, binding in self._sources.items():
            content = self.owner.read_input_blob(binding, reference)
            if store.put_bytes(content, media_type=reference.media_type) != reference:
                raise ValueError("SDK tree package source bytes changed")
        self.require_current()

    def make_plan(self, base, *, limits, case_sensitive=False):
        self.require_current()
        if not isinstance(base, PackagePlan) or (
            base.component_lock_identity,
            base.root_component_revision,
            base.target_identity,
        ) != (self.component_lock_identity, self.root_revision, self.target_identity):
            raise ValueError("SDK tree package base plan differs from locked scope")
        combined = replace(
            base,
            inputs=tuple(
                sorted((*base.inputs, *self.metadata_inputs), key=lambda v: v.path)
            ),
            runtime_requirements=tuple(
                sorted(
                    (*base.runtime_requirements, *self.runtime_requirements),
                    key=lambda v: v.requirement_id,
                )
            ),
        )
        return TreePackagePlan(combined, self.trees, limits, case_sensitive)

    def package(
        self, base, *, adapter, store, object_root, limits, case_sensitive=False
    ):
        from literate_ai.adapters.freebsd_tree_packaging import (
            FreeBsdTreePackageAdapter,
        )
        from literate_ai.adapters.tree_packaging import TreeTarPackageAdapter

        if not isinstance(adapter, (TreeTarPackageAdapter, FreeBsdTreePackageAdapter)):
            raise TypeError("SDK tree package requires an explicit tree provider")
        self.populate_store(store)
        plan = self.make_plan(base, limits=limits, case_sensitive=case_sensitive)
        result = adapter.package(plan, store=store, object_root=object_root)
        self.require_current()
        return plan, result
