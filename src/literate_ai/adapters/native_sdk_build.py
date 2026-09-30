"""Authorized original-source SDK builds for the repository-source pipeline.

This adapter proves build outputs, not native ABI compatibility or consumer
acceptance. Standard SDK admission must still verify those later boundaries.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from literate_ai._filesystem import require_safe_directory
from literate_ai.adapters.builders._process import run_bounded_process
from literate_ai.adapters.builders.python import (
    UNSANDBOXED_HOST_BUILD_PRIVILEGES,
    UNSANDBOXED_HOST_BUILD_PROFILE,
    require_unsandboxed_host_build_authorization,
)
from literate_ai.adapters.lifecycle.standard_local import LocalComponentToolBinding
from literate_ai.adapters.native_sdk_custody import capture_native_sdk
from literate_ai.adapters.native_sdk_runtime import (
    observe_native_sdk_runtime,
)
from literate_ai.adapters.source.git import GitSourceAdapter
from literate_ai.application.repository_sources import (
    RepositoryBuildApproval,
    RepositoryBuildVerification,
    RepositoryCheckout,
)
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.identity import ContentIdentity, canonical_identity
from literate_ai.contracts.native_sdks import NativeSdkBuildLayout, NativeSdkSnapshot
from literate_ai.contracts.repositories import (
    RepositoryBuildOutput,
    RepositoryBuildPlan,
    RepositorySourceLock,
)
from literate_ai.contracts.source import SourceEntryType
from literate_ai.security import (
    BuildAuthorization,
    BuildAuthorizationVerifier,
    BuildRequest,
    FailClosedBuildAuthorizationVerifier,
)
from literate_ai.sources import SourceCapture
from literate_ai.storage.cas import FileSystemCAS


@dataclass(frozen=True, slots=True)
class NativeSdkBuiltProduct:
    """Live builder output; this handle does not grant consumer execution."""

    verification: RepositoryBuildVerification
    snapshot: NativeSdkSnapshot
    snapshot_blob: BlobRef
    runtime_observation: BlobRef
    result_blob: BlobRef


class NativeSdkRepositoryBuilder:
    """Execute an exact authorized plan and retain actual SDK outputs in the CAS."""

    def __init__(
        self,
        *,
        layout: NativeSdkBuildLayout,
        tools: Mapping[str, LocalComponentToolBinding],
        store: FileSystemCAS,
        grant_lookup: Callable[[ContentIdentity], BuildAuthorization],
        environment: Mapping[str, str],
        authorization_verifier: BuildAuthorizationVerifier | None = None,
        timeout_seconds: float = 300,
    ) -> None:
        if not tools or any(
            not isinstance(name, str)
            or not name
            or not isinstance(tool, LocalComponentToolBinding)
            for name, tool in tools.items()
        ):
            raise ValueError("SDK builds require exact named tool bindings")
        if not 0 < timeout_seconds <= 3600:
            raise ValueError("SDK command timeout must be between zero and one hour")
        self.layout = layout
        self.tools = dict(sorted(tools.items()))
        self.store = store
        self.grant_lookup = grant_lookup
        self.environment = dict(environment)
        self.authorization_verifier = (
            authorization_verifier or FailClosedBuildAuthorizationVerifier()
        )
        self.timeout_seconds = timeout_seconds
        self._products: dict[ContentIdentity, NativeSdkBuiltProduct] = {}

    def product(
        self, verification: RepositoryBuildVerification
    ) -> NativeSdkBuiltProduct:
        """Return only an output actually verified by this builder instance."""
        product = self._products.get(verification.build_result)
        if product is None or product.verification != verification:
            raise ValueError("SDK product was not verified by this builder")
        return product

    def request(
        self, lock: RepositorySourceLock, plan: RepositoryBuildPlan
    ) -> BuildRequest:
        if (
            plan.source_lock != lock.identity
            or self.layout.identity not in plan.evidence
        ):
            raise ValueError("SDK plan must bind its exact source lock and layout")
        if plan.expected_outputs != (self.layout.sdk_root,):
            raise ValueError("SDK plan must name its exact SDK output root")
        identities = tuple(tool.toolchain_identity for tool in self.tools.values())
        if plan.toolchains != identities:
            raise ValueError("SDK plan toolchains differ from its exact tool bindings")
        if any(command.argv[0] not in self.tools for command in plan.commands):
            raise ValueError("SDK command requires an explicitly bound tool")
        execution = canonical_identity(
            {
                "plan": plan.identity.to_dict(),
                "layout": self.layout.identity.to_dict(),
                "tools": {
                    name: tool.toolchain_identity.to_dict()
                    for name, tool in self.tools.items()
                },
                "environment": self.environment,
                "timeout_seconds": str(self.timeout_seconds),
            }
        )
        return BuildRequest(
            effective_revision_digest=plan.effective_revision.uri,
            source_bundle_digest=lock.source_snapshot.uri,
            builder_id=f"native-sdk-repository:{execution.uri}",
            toolchain_digest=canonical_identity(
                [item.to_dict() for item in identities]
            ).uri,
            sandbox_profile=UNSANDBOXED_HOST_BUILD_PROFILE,
            requested_privileges=UNSANDBOXED_HOST_BUILD_PRIVILEGES,
            allowed_outputs=plan.expected_outputs,
        )

    def verify(
        self,
        checkout: RepositoryCheckout,
        capture: SourceCapture,
        lock: RepositorySourceLock,
        plan: RepositoryBuildPlan,
        approval: RepositoryBuildApproval,
    ) -> RepositoryBuildVerification:
        request = self.request(lock, plan)
        grant = self.grant_lookup(approval.authorization)
        if (
            approval.source_lock != lock.identity
            or approval.build_plan != plan.identity
            or canonical_identity(grant.to_dict()) != approval.authorization
            or grant.classification_digest != approval.classification.uri
        ):
            raise ValueError("SDK build approval does not bind the exact live grant")

        def require_authorized() -> None:
            self.authorization_verifier.require_build_valid(
                grant, request, now=datetime.now(UTC)
            )
            require_unsandboxed_host_build_authorization(request, grant)

        require_authorized()
        root = checkout.source_root.absolute()
        require_safe_directory(root)

        def require_source() -> None:
            actual = GitSourceAdapter().capture(root)
            if (
                actual.git is None
                or actual.git.dirty
                or actual.git.submodules
                or actual.lfs_pointers
                or actual.git.head_commit != checkout.resolved_commit
                or actual.git.head_commit != lock.resolved_commit
                or actual.snapshot != capture.snapshot
                or actual.git != capture.git
                or actual.snapshot.identity != lock.source_snapshot
                or actual.snapshot.tree_identity != lock.source_tree
            ):
                raise ValueError("SDK original source differs from its exact capture")

        require_source()
        license_entries = tuple(
            item
            for item in capture.snapshot.entries
            if item.path == self.layout.source_license
        )
        if (
            len(license_entries) != 1
            or license_entries[0].entry_type is not SourceEntryType.FILE
            or license_entries[0].identity != self.layout.license_identity
        ):
            raise ValueError(
                "SDK source license must belong to the exact source capture"
            )
        source_license = root / self.layout.source_license
        require_safe_directory(source_license.parent)
        license_blob = self.store.put_file(source_license)
        if license_blob.identity != self.layout.license_identity.uri:
            raise ValueError("SDK source license differs from the selected license")
        sdk = root / self.layout.sdk_root
        if sdk.exists() or sdk.is_symlink():
            raise ValueError("SDK output root must be absent before building")
        for parent in sdk.parents:
            if parent.exists() or parent.is_symlink():
                require_safe_directory(parent)
            if parent == root:
                break
        diagnostics = []
        for command in plan.commands:
            require_authorized()
            require_source()
            for tool in self.tools.values():
                tool.require_unchanged()
            tool = self.tools[command.argv[0]]
            cwd = root / command.working_directory
            require_safe_directory(cwd)
            environment = dict(self.environment)
            environment.update(tool.environment)
            environment.update((item.name, item.value) for item in command.environment)
            result = run_bounded_process(
                (*tool.command, *command.argv[1:]),
                cwd=cwd,
                environment=environment,
                timeout_seconds=self.timeout_seconds,
                stdout_limit_bytes=1024 * 1024,
                stderr_limit_bytes=1024 * 1024,
                error_prefix="builder.native_sdk",
            )
            diagnostics.append(
                {
                    "step_id": command.step_id,
                    "returncode": result.returncode,
                    "stdout": self.store.put_bytes(result.stdout).to_dict(),
                    "stderr": self.store.put_bytes(result.stderr).to_dict(),
                }
            )
            if result.returncode != 0:
                raise ValueError(f"SDK build step failed: {command.step_id}")
        require_authorized()
        require_source()
        for tool in self.tools.values():
            tool.require_unchanged()
        snapshot = capture_native_sdk(
            sdk,
            store=self.store,
            source_lock_identity=lock.identity,
            recipe_identity=plan.identity,
            target_identity=plan.flavor_set,
            license_identity=self.layout.license_identity,
            import_surface=self.layout.import_surface,
            import_root=self.layout.import_root,
            native_libraries=self.layout.native_libraries,
        )
        outputs = {item.path: item.blob for item in snapshot.files}
        if outputs.get(self.layout.output_license) != license_blob:
            raise ValueError("SDK output does not retain the exact source license")
        runtime_observation = observe_native_sdk_runtime(
            snapshot,
            expected_identity=snapshot.identity,
            target_identity=plan.flavor_set,
            operating_system=self.layout.operating_system,
            architecture=self.layout.architecture,
            root=sdk,
            store=self.store,
        )
        require_source()
        require_authorized()
        snapshot_blob = self.store.put_manifest(snapshot.to_dict())
        result_blob = self.store.put_manifest(
            {
                "schema": "literate-ai/native-sdk-repository-build@1",
                "plan": plan.identity.to_dict(),
                "approval": approval.identity.to_dict(),
                "snapshot": snapshot_blob.to_dict(),
                "runtime_observation": runtime_observation.to_dict(),
                "commands": diagnostics,
            }
        )
        verification = RepositoryBuildVerification(
            source_lock=lock.identity,
            build_plan=plan.identity,
            build_approval=approval.identity,
            build_result=ContentIdentity.parse_uri(result_blob.identity),
            expected_outputs=plan.expected_outputs,
            build_outputs=(
                RepositoryBuildOutput(self.layout.sdk_root, snapshot.identity),
            ),
            passed=True,
        )
        self._products[verification.build_result] = NativeSdkBuiltProduct(
            verification, snapshot, snapshot_blob, runtime_observation, result_blob
        )
        return verification
