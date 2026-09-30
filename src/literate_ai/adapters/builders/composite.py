"""Typed consumption boundary for authorized composite build requests."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Never, Protocol, runtime_checkable

from literate_ai.application import (
    ArtifactAssemblyError,
    realize_manifest,
    validate_composite_build_request,
)
from literate_ai.contracts import (
    ArtifactExport,
    ArtifactMaterializationPlan,
    BuildPrivilege,
    ComponentBuildManifest,
    CompositeBuildRequest,
    ContentIdentity,
    canonical_identity,
)
from literate_ai.ports import BuildInputConsumption, BuildInputConsumptionAwareBuilder
from literate_ai.security import (
    BuildAuthorization,
    BuildAuthorizationVerifier,
    BuildRequest,
    FailClosedBuildAuthorizationVerifier,
)

from .python import BuildError, require_unsandboxed_host_build_authorization

_PRIVILEGE_PROJECTION = {
    BuildPrivilege.READ_MATERIALIZED_SOURCE: frozenset({"host-filesystem"}),
    BuildPrivilege.WRITE_OBJECT_DIRECTORY: frozenset({"host-filesystem"}),
    BuildPrivilege.EXECUTE_BUILD_TOOLS: frozenset(
        {"compiler", "processes", "sandbox-escape"}
    ),
    BuildPrivilege.READ_DECLARED_ENVIRONMENT: frozenset({"devices", "secrets"}),
    BuildPrivilege.NETWORK_ACCESS: frozenset({"network", "package-manager"}),
}


@runtime_checkable
class CompositeBuildDelegate(Protocol):
    """A native builder that consumes the complete typed build authority."""

    builder_id: str
    build_system_resolver_identity: ContentIdentity
    build_system_toolchain_identity: ContentIdentity
    language_runtime_identity: ContentIdentity

    def build_composite(
        self,
        composite_request: CompositeBuildRequest,
        manifest: ComponentBuildManifest,
        materialization_plan: ArtifactMaterializationPlan,
        request: BuildRequest,
        authorization: BuildAuthorization,
        artifact: Mapping[str, object],
        consumption: BuildInputConsumption | None = None,
    ) -> Mapping[str, object]: ...


class _LegacyBuildDelegate(Protocol):
    builder_id: str

    def build(
        self,
        request: Mapping[str, object],
        authorization: Mapping[str, object],
    ) -> Mapping[str, object]: ...


def _fail(message: str) -> Never:
    raise BuildError("builder.composite_request_mismatch", message)


def _project_privileges(privileges: tuple[BuildPrivilege, ...]) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                ambient
                for privilege in privileges
                for ambient in _PRIVILEGE_PROJECTION[privilege]
            }
        )
    )


def _artifact_files(artifact: Mapping[str, object]) -> dict[str, bytes]:
    raw_files = artifact.get("files")
    if not isinstance(raw_files, Mapping) or not raw_files:
        _fail("typed composite artifact has no files")
    files: dict[str, bytes] = {}
    for path, content in raw_files.items():
        if not isinstance(path, str) or not isinstance(content, str):
            _fail("typed composite artifact files must be UTF-8 text")
        files[path] = content.encode("utf-8")
    return files


def validate_composite_build_invocation(
    composite_request: CompositeBuildRequest,
    manifest: ComponentBuildManifest,
    materialization_plan: ArtifactMaterializationPlan,
    request: BuildRequest,
    authorization: BuildAuthorization,
    artifact: Mapping[str, object],
    *,
    build_system_resolver_identity: ContentIdentity,
    build_system_toolchain_identity: ContentIdentity,
    language_runtime_identity: ContentIdentity,
) -> CompositeBuildRequest:
    """Revalidate every typed and legacy authority before any build tool runs."""

    if materialization_plan.native_sdk_input_identities:
        _fail("native SDK input materialization is unavailable in this builder")
    try:
        validate_composite_build_request(
            composite_request,
            manifest,
            materialization_plan,
            build_system_resolver_identity=build_system_resolver_identity,
            language_compiler_identity=ContentIdentity.parse_uri(
                request.toolchain_digest
            ),
            language_runtime_identity=language_runtime_identity,
        )
    except (ArtifactAssemblyError, ValueError) as exc:
        _fail(str(exc))
    if (
        composite_request.build_system_toolchain_identity
        != build_system_toolchain_identity
    ):
        _fail("selected build-system toolchain identity differs")
    if composite_request.source_tree_identity.uri != request.source_bundle_digest:
        _fail("security request source identity differs")
    expected_authorization = canonical_identity(authorization.to_dict())
    if composite_request.authorization_identity != expected_authorization:
        _fail("security authorization identity differs")
    if any(
        action.action.authorization_identity != expected_authorization
        for action in composite_request.sub_actions
    ):
        _fail("sub-action authorization identity differs")
    if any(
        action.action.target_identity != composite_request.target_identity
        for action in composite_request.sub_actions
    ):
        _fail("sub-action target identity differs")
    projected = _project_privileges(composite_request.requested_privileges)
    if projected != tuple(sorted(request.requested_privileges)):
        _fail("security request privileges differ")
    if projected != tuple(sorted(authorization.privileges)):
        _fail("security authorization privileges differ")
    if composite_request.declared_output_ids != tuple(sorted(request.allowed_outputs)):
        _fail("security request outputs differ")

    files = _artifact_files(artifact)
    entries = {entry.path: entry.blob for entry in materialization_plan.entries}
    if set(files) != set(entries):
        _fail("artifact files differ from the materialization plan")
    for path, content in files.items():
        blob = entries[path]
        if (
            len(content) != blob.size
            or hashlib.sha256(content).hexdigest() != blob.digest
        ):
            _fail(f"artifact blob differs from materialization plan: {path}")
    return composite_request


def _realize_composite_output(
    manifest: ComponentBuildManifest, result: Mapping[str, object]
) -> dict[str, object]:
    raw = result.get("artifact_exports")
    if raw is None:
        exports = manifest.exports
    elif isinstance(raw, (list, tuple)):
        try:
            exports = tuple(
                item
                if type(item) is ArtifactExport
                else ArtifactExport.from_dict(
                    item, path=f"build result artifact_exports[{index}]"
                )
                for index, item in enumerate(raw)
            )
        except (TypeError, ValueError) as exc:
            _fail(f"build result artifact exports are invalid: {exc}")
    else:
        _fail("build result artifact_exports must be an array")
    if not exports:
        _fail(
            "declaration-only composite builds require realized artifact_exports "
            "from the native delegate"
        )
    try:
        realized = realize_manifest(manifest, exports)
    except ArtifactAssemblyError as exc:
        _fail(str(exc))
    return {
        **result,
        "artifact_exports": [item.to_dict() for item in realized.exports],
        "component_build_manifest": realized.to_dict(),
    }


class CompositeNativeBuildAdapter:
    """Make a legacy native builder consume a revalidated typed invocation."""

    def __init__(
        self,
        delegate: _LegacyBuildDelegate,
        *,
        build_system_resolver_identity: ContentIdentity,
        build_system_toolchain_identity: ContentIdentity,
        language_runtime_identity: ContentIdentity,
        authorization_verifier: BuildAuthorizationVerifier | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(getattr(delegate, "builder_id", None), str):
            raise ValueError("composite native delegate must expose builder_id")
        self.delegate = delegate
        self.build_system_resolver_identity = build_system_resolver_identity
        self.build_system_toolchain_identity = build_system_toolchain_identity
        self.language_runtime_identity = language_runtime_identity
        self.authorization_verifier = (
            authorization_verifier or FailClosedBuildAuthorizationVerifier()
        )
        self.clock = clock or (lambda: datetime.now(UTC))

    @property
    def builder_id(self) -> str:
        return self.delegate.builder_id

    def build_composite(
        self,
        composite_request: CompositeBuildRequest,
        manifest: ComponentBuildManifest,
        materialization_plan: ArtifactMaterializationPlan,
        request: BuildRequest,
        authorization: BuildAuthorization,
        artifact: Mapping[str, object],
        consumption: BuildInputConsumption | None = None,
    ) -> Mapping[str, object]:
        validate_composite_build_invocation(
            composite_request,
            manifest,
            materialization_plan,
            request,
            authorization,
            artifact,
            build_system_resolver_identity=self.build_system_resolver_identity,
            build_system_toolchain_identity=self.build_system_toolchain_identity,
            language_runtime_identity=self.language_runtime_identity,
        )
        self.authorization_verifier.require_build_valid(
            authorization, request, now=self.clock()
        )
        require_unsandboxed_host_build_authorization(request, authorization)
        if request.builder_id != self.builder_id:
            raise BuildError(
                "builder.identity_mismatch", "build request selected another delegate"
            )
        request_document = {**request.to_dict(), "artifact": artifact}
        authorization_document = authorization.to_dict()
        if consumption is not None and isinstance(
            self.delegate, BuildInputConsumptionAwareBuilder
        ):
            result = self.delegate.build_with_input_consumption(
                request_document, authorization_document, consumption
            )
        else:
            result = self.delegate.build(request_document, authorization_document)
        return _realize_composite_output(manifest, result)


__all__ = [
    "CompositeBuildDelegate",
    "CompositeNativeBuildAdapter",
    "validate_composite_build_invocation",
]
