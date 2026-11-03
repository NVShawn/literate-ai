"""Operation-local current retained-wrapper context, never scratch permission.

The receiving transaction must separately prove complete source namespace and
exact output ownership before excluding or writing any scratch root. Historical
passed documents, this context's identity and serialized plans grant no rights.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass

from literate_ai.adapters.component_lock_application import (
    current_project_component_lock_bindings,
    current_project_component_lock_identities,
)
from literate_ai.adapters.component_locks import ComponentLockStore
from literate_ai.adapters.conversion_authority import (
    CONVERSION_AUTHORITY_FILE,
    FilesystemConversionAuthorityStore,
)
from literate_ai.adapters.exclusive_directory import directory_node
from literate_ai.adapters.lifecycle_lock import project_lifecycle_lock
from literate_ai.adapters.project_validation import validated_project_authority_identity
from literate_ai.adapters.retained_cargo_review_v2 import RetainedCargoReviewedFilesV2
from literate_ai.adapters.retained_harness_receipts import (
    _implementation_root,
    _load_retained_documents,
    _require_qualified_parity,
    _retained_project_authority_observation,
)
from literate_ai.contracts.identity import canonical_identity, canonical_json_bytes
from literate_ai.projects import (
    PROJECT_FILENAME,
    PinnedInputClosure,
    ProjectConfigurationStore,
)

_CREATION_KEY = object()
_DOCUMENTS = (
    ("inventory", ".literate/harness-inventory.json"),
    ("baseline", ".literate/legacy-harness-baseline.json"),
    ("parity", ".literate/legacy-wrapper-parity.json"),
    ("lift_shift", ".literate/legacy-lift-shift.json"),
)


@dataclass(slots=True)
class _WrapperLifetime:
    active: bool = True


@dataclass(init=False, eq=False, frozen=True)
class RetainedCargoWrapperContextV2:
    """Live context minted only while holding the project's mutation lock.

    This binds current original-source authority and the actual selected wrapper
    lock/catalog. Complete rediscovery and scratch reservations remain separate.
    No decoder or saved observation can reopen an ended operation.
    """

    def __init__(
        self, *, _key, reviewed, revision, conversion, documents, bindings, lifetime
    ):
        if _key is not _CREATION_KEY:
            raise TypeError("retained.cargo.v2-live-wrapper-context-required")
        for name, value in (
            ("reviewed", reviewed),
            ("revision", revision),
            ("conversion", conversion),
            ("bindings", bindings),
            ("_lifetime", lifetime),
            ("implementation", _implementation_root(reviewed.project, documents)),
            ("workspace", reviewed.project.root / reviewed.plan.common.workspace_root),
        ):
            object.__setattr__(self, name, value)
        if not self.workspace.is_relative_to(self.implementation):
            raise ValueError("retained.cargo.v2-workspace-outside-retained-wrapper")
        object.__setattr__(
            self, "_implementation_node", directory_node(self.implementation)
        )
        object.__setattr__(self, "_workspace_node", directory_node(self.workspace))
        object.__setattr__(
            self,
            "_inputs",
            PinnedInputClosure(
                maximum_files=7,
                maximum_file_bytes=16 * 1024**2,
                maximum_total_bytes=112 * 1024**2,
            ),
        )
        root = reviewed.project.root
        self._inputs.pin(root / PROJECT_FILENAME, boundary=root, label=PROJECT_FILENAME)
        for name, relative in _DOCUMENTS:
            content = self._inputs.pin(root / relative, boundary=root, label=relative)
            if content not in {
                documents[name]["bytes"],
                documents[name]["bytes"] + b"\n",
            }:
                raise ValueError("retained.cargo.v2-wrapper-documents-changed")
        self._inputs.pin(
            root / "litai.harness.mk",
            boundary=root,
            label="retained harness wrapper",
            expected_content=documents["wrapper"]["bytes"],
        )
        conversion_bytes = self._inputs.pin(
            root / CONVERSION_AUTHORITY_FILE,
            boundary=root,
            label="retained conversion authority",
        )
        if canonical_json_bytes(json.loads(conversion_bytes)) != canonical_json_bytes(
            conversion.to_dict()
        ):
            raise ValueError("retained.cargo.v2-wrapper-conversion-changed")
        object.__setattr__(
            self,
            "_closures",
            tuple(
                catalog.locked_input_closure(
                    lock,
                    lock_path=ComponentLockStore(catalog.root).path,
                    lock_boundary=ComponentLockStore(catalog.root).storage_root,
                )
                for lock, catalog in bindings
            ),
        )
        source_authority, _ = _retained_project_authority_observation(
            reviewed.project,
            revision,
            documents=documents,
            implementation=self.implementation,
        )
        object.__setattr__(self, "source_authority", source_authority)

    def __copy__(self):
        raise TypeError("retained.cargo.v2-wrapper-context-not-copyable")

    def __deepcopy__(self, memo):
        raise TypeError("retained.cargo.v2-wrapper-context-not-copyable")

    def __reduce_ex__(self, protocol):
        raise TypeError("retained.cargo.v2-wrapper-context-not-serializable")

    def __getstate__(self):
        raise TypeError("retained.cargo.v2-wrapper-context-not-serializable")

    @property
    def identity(self):
        self.require_unchanged()
        return canonical_identity(
            {
                "schema": "literate-ai/retained-cargo-wrapper-context@2",
                "reviewed": self.reviewed.identity.uri,
                "revision": self.revision.uri,
                "conversion": self.conversion.identity.uri,
                "source_authority": self.source_authority.uri,
                "implementation": self.implementation.relative_to(
                    self.reviewed.project.root
                ).as_posix(),
                "locks": [lock.identity.uri for lock, _ in self.bindings],
            }
        )

    def require_unchanged(self):
        if not self._lifetime.active:
            raise ValueError("retained.cargo.v2-wrapper-operation-ended")
        reviewed = self.reviewed
        reviewed.require_unchanged()
        self._inputs.require_unchanged()
        if (
            directory_node(self.implementation) != self._implementation_node
            or directory_node(self.workspace) != self._workspace_node
            or ProjectConfigurationStore(reviewed.project.root).read().definition
            != reviewed.project.definition
            or FilesystemConversionAuthorityStore(reviewed.project.root).load_optional()
            != self.conversion
            or validated_project_authority_identity(
                reviewed.project.root, synchronize_source_intelligence=False
            )
            != self.revision
        ):
            raise ValueError("retained.cargo.v2-wrapper-authority-changed")
        for (lock, catalog), closure in zip(self.bindings, self._closures, strict=True):
            catalog.require_unchanged()
            ComponentLockStore(catalog.root).require_current(lock)
            closure.require_unchanged()
        documents = _load_retained_documents(reviewed.project)
        _require_qualified_parity(documents)
        current, _ = _retained_project_authority_observation(
            reviewed.project,
            self.revision,
            documents=documents,
            implementation=self.implementation,
        )
        if current != self.source_authority:
            raise ValueError("retained.cargo.v2-wrapper-source-changed")
        self._inputs.require_unchanged()
        reviewed.require_unchanged()
        if (
            directory_node(self.implementation) != self._implementation_node
            or directory_node(self.workspace) != self._workspace_node
            or current_project_component_lock_identities(reviewed.project)
            != tuple(lock.identity for lock, _ in self.bindings)
        ):
            raise ValueError("retained.cargo.v2-wrapper-ancestry-or-lock-set-changed")


@contextmanager
def retained_cargo_wrapper_context_v2(reviewed: RetainedCargoReviewedFilesV2):
    """Join current authorities under the lifecycle lock, invalidating on exit.

    The current canonical wrapper coordinate is used directly; custom retained
    layouts require the separately tracked layout resolver, not an alias shim.
    """
    if type(reviewed) is not RetainedCargoReviewedFilesV2:
        raise TypeError("retained.cargo.v2-reviewed-files-required")
    project = reviewed.project
    with project_lifecycle_lock(project.root, operation="retained-cargo.v2"):
        reviewed.require_unchanged()
        if (
            ProjectConfigurationStore(project.root).read().definition
            != project.definition
        ):
            raise ValueError("retained.cargo.v2-wrapper-project-changed")
        revision = validated_project_authority_identity(
            project.root, synchronize_source_intelligence=False
        )
        conversion = FilesystemConversionAuthorityStore(project.root).load_optional()
        if (
            conversion is None
            or conversion.project_id != project.definition.project_id
            or conversion.release_authority != "original-source"
        ):
            raise ValueError("retained.cargo.v2-original-authority-required")
        documents = _load_retained_documents(project)
        _require_qualified_parity(documents)
        bindings = current_project_component_lock_bindings(project)
        wrapper = project.root / "components/legacy-project-wrapper"
        selected = tuple((lock, c) for lock, c in bindings if c.root == wrapper)
        if len(selected) != 1:
            raise ValueError("retained.cargo.v2-current-wrapper-lock-required")
        lock, catalog = selected[0]
        root_revision = next(
            node.revision
            for node in lock.nodes
            if node.revision.identity == lock.root_revision
        )
        if (
            catalog.project is None
            or catalog.project.root != project.root
            or catalog.root_authoring != root_revision.definition
        ):
            raise ValueError("retained.cargo.v2-wrapper-owner-mismatch")
        lifetime = _WrapperLifetime()
        context = RetainedCargoWrapperContextV2(
            _key=_CREATION_KEY,
            reviewed=reviewed,
            revision=revision,
            conversion=conversion,
            documents=documents,
            bindings=bindings,
            lifetime=lifetime,
        )
        try:
            context.require_unchanged()
            yield context
            context.require_unchanged()
        finally:
            lifetime.active = False
