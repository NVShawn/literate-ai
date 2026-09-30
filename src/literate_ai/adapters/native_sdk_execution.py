"""Fresh, directly scoped SDK imports and revocable per-command execution authority."""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from literate_ai._filesystem import path_is_link_or_reparse, require_safe_directory
from literate_ai.adapters.native_sdk_consumer import (
    MaterializedNativeSdkInput,
    NativeSdkConsumerInputs,
)
from literate_ai.adapters.native_sdk_linked_runtime import (
    LinkedNativeSdkExecutionInputs,
)
from literate_ai.contracts.identity import (
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.security import (
    AuthorizationError,
    BuildAuthorization,
    BuildRequest,
    SecurityProfile,
)

if TYPE_CHECKING:
    from literate_ai.adapters.lifecycle.standard_local import LocalComponentToolBinding
    from literate_ai.adapters.native_sdk_package_runtime import PackagedNativeSdkInputs


def _loader_override(name: str) -> bool:
    return name.upper() in {"PYTHONPATH", "PYTHONHOME"} or name.upper().startswith(
        ("LD_", "DYLD_")
    )


def isolated_sdk_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """Match native inspection's clean loader environment before binding the request."""
    return {
        key: value for key, value in environment.items() if not _loader_override(key)
    }


def _manifest_document(revision, target, values, root):
    return {
        "schema": "literate-ai/native-sdk-execution-inputs@1",
        "consumer_revision": revision.to_dict(),
        "target_identity": target.to_dict(),
        "imports": [
            {
                "root": value.root.relative_to(root).as_posix(),
                "input_identity": value.binding.identity.to_dict(),
                "snapshot": value.binding.build.product.snapshot.to_dict(),
            }
            for value in values
        ],
    }


@dataclass(frozen=True, slots=True)
class NativeSdkCommandScope:
    """Bound launch values; evidence becomes available only after scope validation."""

    argv: tuple[str, ...]
    environment: Mapping[str, str]
    evidence_identity: ContentIdentity | None = None


@dataclass(frozen=True, slots=True)
class NativeSdkExecutionInputs:
    """A live materialization; possession alone never grants permission to run it."""

    owner: (
        NativeSdkConsumerInputs
        | PackagedNativeSdkInputs
        | LinkedNativeSdkExecutionInputs
    )
    revision: ContentIdentity
    target: ContentIdentity
    values: tuple[MaterializedNativeSdkInput, ...]
    manifest: Path
    manifest_identity: ContentIdentity

    @property
    def runtime_identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "consumer": self.revision.to_dict(),
                "target": self.target.to_dict(),
                "inputs": [item.binding.identity.to_dict() for item in self.values],
                "dependencies": [
                    item.dependencies.identity.to_dict() for item in self.values
                ],
            }
        )

    def require_unchanged(self) -> None:
        if not self.values or tuple(
            value.binding for value in self.values
        ) != self.owner.for_consumer(self.revision, target_identity=self.target):
            raise ValueError("SDK execution inputs differ from the selected consumer")
        expected = canonical_identity(
            _manifest_document(
                self.revision, self.target, self.values, self.manifest.parent
            )
        )
        if (
            self.manifest_identity != expected
            or self.manifest.name != f"{expected.digest}.json"
        ):
            raise ValueError("SDK execution manifest differs from its exact inputs")
        require_safe_directory(self.manifest.parent)
        if path_is_link_or_reparse(self.manifest) or not self.manifest.is_file():
            raise ValueError("SDK execution manifest is not a regular file")
        if (
            hashlib.sha256(self.manifest.read_bytes()).hexdigest()
            != self.manifest_identity.digest
        ):
            raise ValueError("SDK execution manifest changed")
        for value in self.values:
            self.owner.reobserve_runtime(value)

    def run(self, argv, *, execute, **arguments):
        with self.command_scope(argv, **arguments) as scope:
            result = execute(scope.argv, dict(scope.environment))
        return result, scope.evidence_identity

    @contextmanager
    def command_scope(
        self,
        argv: tuple[str, ...],
        *,
        tool: LocalComponentToolBinding,
        cwd: Path,
        environment: Mapping[str, str],
        source_identity: ContentIdentity,
        phase: str,
        command_identity: ContentIdentity,
        command_contract_identity: ContentIdentity,
        entrypoint_identity: ContentIdentity | None = None,
        package_identity: ContentIdentity | None = None,
        authorize: Callable[[BuildRequest, ContentIdentity], BuildAuthorization],
        clock: Callable[[], datetime],
        record: Callable[[object], ContentIdentity],
    ) -> Iterator[NativeSdkCommandScope]:
        """Authorize one complete process lifetime, including caller-owned teardown."""
        from literate_ai.adapters.lifecycle.standard_local import (
            LocalComponentToolBinding,
        )

        if not isinstance(tool, LocalComponentToolBinding):
            raise TypeError("SDK execution requires an observed local tool binding")
        argv = tuple(argv)
        if not argv or argv[: len(tool.command)] != tool.command:
            raise ValueError("SDK execution command differs from its bound tool")
        selected_environment = dict(environment)
        # These affect Python or native loader resolution outside the observed SDK
        # graph. Refuse them; silently altering a command's environment changes it.
        if any(_loader_override(key) for key in selected_environment):
            raise ValueError(
                "SDK execution environment has an ambient import or loader override"
            )
        self.require_unchanged()
        tool.require_unchanged()
        command_binding = {
            "phase": phase,
            "command_identity": command_identity.to_dict(),
            "command_contract_identity": command_contract_identity.to_dict(),
            "entrypoint_identity": None
            if entrypoint_identity is None
            else entrypoint_identity.to_dict(),
            "argv_identity": canonical_identity(list(argv)).to_dict(),
            "cwd_identity": canonical_identity(str(cwd)).to_dict(),
            "environment_identity": canonical_identity(selected_environment).to_dict(),
            "manifest": self.manifest_identity.to_dict(),
            "runtime": self.runtime_identity.to_dict(),
        }
        linked_scope = (
            self.owner.execution_scope
            if isinstance(self.owner, LinkedNativeSdkExecutionInputs)
            else None
        )
        if linked_scope is not None:
            command_binding["execution_scope_identity"] = (
                linked_scope.identity.to_dict()
            )
        if package_identity is not None:
            command_binding["package_identity"] = package_identity.to_dict()
        request = BuildRequest(
            effective_revision_digest=self.revision.uri,
            source_bundle_digest=source_identity.uri,
            builder_id=canonical_identity(command_binding).uri,
            toolchain_digest=tool.toolchain_identity.uri,
            sandbox_profile="local-explicit-native-sdk",
            requested_privileges=("execute-native-sdk",),
            allowed_outputs=("stdout", "stderr"),
        )
        grant = authorize(request, self.runtime_identity)
        if not isinstance(grant, BuildAuthorization):
            raise TypeError("SDK execution requires a typed command authorization")

        def require_authority() -> dict[str, object]:
            self.require_unchanged()
            tool.require_unchanged()
            if grant.classification_digest != self.runtime_identity.uri:
                raise ValueError(
                    "SDK execution grant differs from the fresh runtime graph"
                )
            if (
                grant.profile is SecurityProfile.BLOCKED
                or grant.privileges != request.requested_privileges
            ):
                raise AuthorizationError(
                    "security.native_sdk_execution_privilege_mismatch"
                )
            checked_at = clock()
            revocations = self.owner.require_execution_authorized(
                self.revision, request, grant, now=checked_at
            )
            return {
                "checked_at": checked_at.isoformat(),
                "revocations": list(revocations),
            }

        before = require_authority()
        scope = NativeSdkCommandScope(argv, MappingProxyType(selected_environment))
        try:
            yield scope
        finally:
            after = require_authority()
        manifest = _manifest_document(
            self.revision, self.target, self.values, self.manifest.parent
        )
        if record(manifest) != self.manifest_identity:
            raise ValueError("SDK execution manifest retention changed its identity")
        record(request.to_dict())
        record(grant.to_dict())
        for value in self.values:
            record(value.dependencies.to_dict())
            record(value.binding.to_dict())
            record(value.binding.build.selection.to_dict())
            record(value.binding.build.resolution.build_plan.to_dict())
        evidence = record(
            {
                "schema": "literate-ai/native-sdk-command-execution@2"
                if linked_scope is None
                else "literate-ai/native-sdk-command-execution@3",
                **(
                    {}
                    if linked_scope is None
                    else {"execution_scope": linked_scope.to_dict()}
                ),
                "phase": phase,
                "command_binding": command_binding,
                "request": request.to_dict(),
                "authorization": grant.to_dict(),
                "runtime_identity": self.runtime_identity.to_dict(),
                "manifest_identity": self.manifest_identity.to_dict(),
                "dependencies": [value.dependencies.to_dict() for value in self.values],
                "checks": [before, after],
            }
        )
        object.__setattr__(scope, "evidence_identity", evidence)


@contextmanager
def prepare_native_sdk_execution(
    owner: NativeSdkConsumerInputs
    | PackagedNativeSdkInputs
    | LinkedNativeSdkExecutionInputs,
    revision: ContentIdentity,
    *,
    target: ContentIdentity,
    parent: Path,
) -> Iterator[NativeSdkExecutionInputs]:
    """Prepare direct SDK imports in an owned temporary directory."""
    from literate_ai.adapters.native_sdk_package_runtime import PackagedNativeSdkInputs

    if not isinstance(
        owner,
        (
            NativeSdkConsumerInputs,
            PackagedNativeSdkInputs,
            LinkedNativeSdkExecutionInputs,
        ),
    ):
        raise TypeError("SDK execution requires live consumer inputs")
    require_safe_directory(parent)
    root = Path(tempfile.mkdtemp(prefix=".native-sdk-execution-", dir=parent))
    try:
        with owner.materialize(revision, target_identity=target, parent=root) as values:
            if not values:
                raise ValueError("SDK execution requires selected consumer inputs")
            content = canonical_json_bytes(
                _manifest_document(revision, target, values, root)
            )
            identity = ContentIdentity.parse_uri(
                "sha256:" + hashlib.sha256(content).hexdigest()
            )
            manifest = root / f"{identity.digest}.json"
            manifest.write_bytes(content)
            yield NativeSdkExecutionInputs(
                owner, revision, target, values, manifest, identity
            )
    finally:
        shutil.rmtree(root)
