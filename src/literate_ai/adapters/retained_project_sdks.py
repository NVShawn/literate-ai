"""Join retained SDK files to live existing producer and command authority owners."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType

from literate_ai._filesystem import require_safe_directory
from literate_ai.adapters.native_sdk_consumer import NativeSdkConsumerInputs
from literate_ai.adapters.native_sdk_execution import (
    NativeSdkExecutionInputs,
    _manifest_document,
)
from literate_ai.adapters.retained_project_inputs import verify_retained_project_inputs
from literate_ai.adapters.retained_project_metadata import (
    read_retained_project_document,
)
from literate_ai.contracts import (
    ComponentLock,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.contracts.native_sdk_tree_products import NativeSdkTreeSnapshot
from literate_ai.contracts.retained_project import (
    RetainedProjectManifest,
    RetainedProjectMember,
)
from literate_ai.contracts.retained_project_lifecycle import RetainedProjectPlan
from literate_ai.contracts.retained_project_sdks import (
    RetainedProjectSdkSelectionPolicy,
    read_retained_sdk_policy_document,
)


def _inside_prefix(path, prefixes):
    while path:
        if path in prefixes:
            return True
        path = path.rpartition("/")[0]
    return False


def read_retained_sdk_policy(root, manifest, locks):
    members = [m for m in manifest.members if m.role == "sdk-policy"]
    if len(members) != 1:
        raise ValueError("retained.sdk.exact-policy-input-required")
    policy = read_retained_sdk_policy_document(
        read_retained_project_document(root, members[0], error_prefix="retained.sdk")
    )
    policy.require_locks(locks)
    return policy


@dataclass(frozen=True, slots=True)
class RetainedSdkCommandScope:
    evidence_identities: tuple = ()
    argv: tuple[str, ...] | None = None
    environment: Mapping[str, str] | None = None


class RetainedProjectNativeSdkInputs:
    """Exact captured SDK placement, backed by completed native producer services.

    This adapter never builds or acquires a product, invents a grant, or authenticates
    transported producer records. Existing live owners retain all those boundaries.
    """

    def __init__(self, root, manifest, plan, locks, owners, *, parent):
        if (
            not isinstance(plan, RetainedProjectPlan)
            or not isinstance(manifest, RetainedProjectManifest)
            or type(locks) is not tuple
            or any(not isinstance(v, ComponentLock) for v in locks)
            or type(owners) is not tuple
            or any(not isinstance(v, NativeSdkConsumerInputs) for v in owners)
        ):
            raise TypeError("retained.sdk.live-typed-inputs-required")
        if (
            plan.manifest_identity != manifest.identity
            or tuple(lock.identity for lock in locks) != plan.component_locks
            or tuple(owner.snapshot.authority.lock for owner in owners) != locks
        ):
            raise ValueError("retained.sdk.exact-lock-owner-closure-required")
        self.root = Path(root)
        self.manifest = manifest
        self.plan = plan
        self.owners = owners
        self.policy = read_retained_sdk_policy(root, manifest, plan.component_locks)
        selected = {}
        for lock, owner in zip(locks, owners, strict=True):
            owner.require_unchanged()
            for binding in owner.for_scope(lock.root_revision):
                selected[lock.identity, binding.identity] = binding
        if isinstance(self.policy, RetainedProjectSdkSelectionPolicy):
            expected = {
                (
                    v.component_lock_identity,
                    v.selection_identity,
                    v.snapshot_identity,
                ): v.prefix
                for v in self.policy.bindings
            }
            actual = {
                (
                    lock_id,
                    binding.build.selection.identity,
                    binding.build.product.snapshot.identity,
                ): input_id
                for (lock_id, input_id), binding in selected.items()
            }
            if len(actual) != len(selected) or set(actual) != set(expected):
                raise ValueError(
                    "retained.sdk.complete-selected-snapshot-closure-required"
                )
            self._prefixes = {
                (key[0], actual[key]): prefix for key, prefix in expected.items()
            }
        else:
            self._prefixes = {
                (v.component_lock_identity, v.input_identity): v.prefix
                for v in self.policy.bindings
            }
        if set(selected) != set(self._prefixes):
            raise ValueError("retained.sdk.complete-product-bindings-required")
        expected = {}
        expected_directories = set()
        for key, binding in selected.items():
            prefix = self._prefixes[key]
            sdk = binding.build.product.snapshot
            expected_directories.add(prefix)
            if isinstance(sdk, NativeSdkTreeSnapshot):
                if manifest.tree_policy is None or (
                    sdk.tree.case_sensitive and not manifest.tree_policy.case_sensitive
                ):
                    raise ValueError("retained.sdk.selected-tree-policy-required")
                expected_directories.update(
                    prefix + "/" + path for path in sdk.tree.directories
                )
            for item in sdk.files:
                path = prefix + "/" + item.path
                expected[path] = RetainedProjectMember(
                    path,
                    item.blob.size,
                    0o755 if item.executable else 0o644,
                    item.blob.digest,
                    "sdk",
                )
                directory = path.rpartition("/")[0]
                while directory != prefix:
                    expected_directories.add(directory)
                    directory = directory.rpartition("/")[0]
            if isinstance(sdk, NativeSdkTreeSnapshot):
                physical = {item.path: item for item in sdk.files}
                for alias in sdk.tree.aliases:
                    path = prefix + "/" + alias.path
                    if alias.kind == "hardlink":
                        item = physical[alias.target]
                        expected[path] = RetainedProjectMember(
                            path,
                            item.blob.size,
                            0o755 if item.executable else 0o644,
                            item.blob.digest,
                            "sdk",
                            hardlink_target=prefix + "/" + alias.target,
                        )
                    else:
                        content = alias.target.encode("utf-8")
                        expected[path] = RetainedProjectMember(
                            path,
                            len(content),
                            0o120000,
                            hashlib.sha256(content).hexdigest(),
                            "sdk",
                            symlink_target=alias.target,
                        )
        sdk_members = {m.path: m for m in manifest.members if m.role == "sdk"}
        if set(sdk_members) != set(expected):
            raise ValueError("retained.sdk.complete-sdk-file-closure-required")
        if sdk_members != expected:
            raise ValueError("retained.sdk.product-file-mismatch")
        prefixes = set(self._prefixes.values())
        if (
            any(
                _inside_prefix(m.path, prefixes) and m.path not in expected
                for m in manifest.members
            )
            or {p for p in manifest.directories if _inside_prefix(p, prefixes)}
            != expected_directories
        ):
            raise ValueError("retained.sdk.product-directory-closure-mismatch")
        require_safe_directory(parent)
        self.executions = []
        # Materialize and inspect through the existing producer owner, then bind
        # those verified observations to the identical captured retained files.
        with ExitStack() as stack:
            for lock, owner in zip(locks, owners, strict=True):
                groups = sorted(
                    {
                        (
                            b.build.selection.component_revision,
                            b.build.selection.target_identity,
                        )
                        for (lock_id, _), b in selected.items()
                        if lock_id == lock.identity
                    },
                    key=lambda pair: (pair[0].uri, pair[1].uri),
                )
                for revision, target in groups:
                    values = stack.enter_context(
                        owner.materialize(
                            revision, target_identity=target, parent=parent
                        )
                    )
                    placed = tuple(
                        replace(
                            value,
                            root=self.root
                            / self._prefixes[lock.identity, value.binding.identity],
                        )
                        for value in values
                    )
                    for value in placed:
                        owner.reobserve_runtime(value)
                    content = canonical_json_bytes(
                        _manifest_document(revision, target, placed, Path(parent))
                    )
                    identity = canonical_identity(
                        _manifest_document(revision, target, placed, Path(parent))
                    )
                    path = Path(parent) / (identity.digest + ".json")
                    with path.open("xb") as stream:
                        stream.write(content)
                    execution = NativeSdkExecutionInputs(
                        owner, revision, target, placed, path, identity
                    )
                    execution.require_unchanged()
                    self.executions.append(execution)
        self.executions = tuple(self.executions)
        self._last_scopes = ()
        self.require_unchanged()

    @property
    def dependencies(self):
        return tuple(value.dependencies for e in self.executions for value in e.values)

    @property
    def identity(self):
        return canonical_identity(
            {
                "schema": "literate-ai/retained-project-sdk-inputs@1",
                "plan_identity": self.plan.identity.uri,
                "policy_identity": self.policy.identity.uri,
                "inputs": [e.runtime_identity.uri for e in self.executions],
                "lifecycle_receipt": False,
            }
        )

    def require_unchanged(self):
        verify_retained_project_inputs(self.root, self.manifest)
        if (
            read_retained_sdk_policy(
                self.root, self.manifest, self.plan.component_locks
            )
            != self.policy
        ):
            raise ValueError("retained.sdk.policy-changed")
        for owner in self.owners:
            owner.require_unchanged()
        for execution in self.executions:
            execution.require_unchanged()

    def final_checks(self, record):
        self.require_unchanged()
        if len(self._last_scopes) != len(self.executions):
            raise ValueError("retained.sdk.completed-command-authority-required")
        document = {
            "schema": "literate-ai/retained-sdk-final-authority-check@1",
            "plan_identity": self.plan.identity.uri,
            "inputs_identity": self.identity.uri,
            "checks": [scope.require_current() for scope in self._last_scopes],
        }
        identity = record(document)
        if identity != canonical_identity(document):
            raise ValueError("retained.sdk.final-check-retention-mismatch")
        return identity

    @contextmanager
    def command_scope(self, argv, *, action_identity, **arguments):
        self.require_unchanged()
        expected_argv = tuple(argv)
        expected_environment = dict(arguments["environment"])
        with ExitStack() as stack:
            scopes = tuple(
                stack.enter_context(
                    execution.command_scope(
                        argv,
                        source_identity=self.plan.manifest_identity,
                        command_identity=action_identity,
                        command_contract_identity=self.plan.identity,
                        **arguments,
                    )
                )
                for execution in self.executions
            )
            if scopes:
                if any(
                    scope.argv != expected_argv
                    or dict(scope.environment) != expected_environment
                    for scope in scopes
                ):
                    raise ValueError("retained.sdk.command-projection-mismatch")
            result = RetainedSdkCommandScope(
                argv=expected_argv if scopes else None,
                environment=MappingProxyType(expected_environment) if scopes else None,
            )
            yield result
        self.require_unchanged()
        object.__setattr__(
            result,
            "evidence_identities",
            tuple(scope.evidence_identity for scope in scopes),
        )
        self._last_scopes = scopes
