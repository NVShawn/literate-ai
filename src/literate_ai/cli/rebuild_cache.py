"""Outer-CLI ownership of source-cache negotiation and publication."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from literate_ai.adapters.cache.filesystem import SourceCacheError, SourceCacheResolver
from literate_ai.adapters.cache.rebuild import (
    SOURCE_CACHE_CONTROL_ENVIRONMENT,
    SOURCE_CACHE_CONTROL_IDENTITY_ENVIRONMENT,
    SOURCE_CACHE_DECISION_ENVIRONMENT,
    SOURCE_CACHE_DECISION_IDENTITY_ENVIRONMENT,
    SOURCE_CACHE_DERIVATION_MANIFEST_ENVIRONMENT,
    SOURCE_CACHE_LIFECYCLE_ENVIRONMENT,
    SOURCE_CACHE_PLANNING_MODE,
    SOURCE_CACHE_PLANNING_MODE_ENVIRONMENT,
    SOURCE_CACHE_PLANNING_REQUEST_IDENTITY_ENVIRONMENT,
    SOURCE_CACHE_PROTOCOL_DIRECTORY,
    SOURCE_CACHE_PUBLICATION_ENVIRONMENT,
    ProtocolDirectoryIdentity,
    RebuildSourceCacheProtocolError,
    protocol_directory_identity,
    read_rebuild_source_cache_control,
    read_rebuild_source_cache_decision,
    read_rebuild_source_cache_derivation_manifest,
    read_rebuild_source_cache_lifecycle,
    read_rebuild_source_cache_publication,
    source_cache_derivation_manifest_path,
    source_cache_protocol_path_present,
    source_cache_protocol_paths,
    write_rebuild_source_cache_control,
    write_rebuild_source_cache_decision,
)
from literate_ai.contracts.generation_cache import SourceCacheMode, SourceCacheRootKind
from literate_ai.contracts.identity import ContentIdentity
from literate_ai.contracts.rebuild_cache import (
    RebuildSourceCacheControl,
    RebuildSourceCacheDecision,
    RebuildSourceCacheDerivationManifest,
    RebuildSourceCacheLifecycleBinding,
    RebuildSourceCacheOperatorRoot,
    RebuildSourceCachePublicationOffer,
)
from literate_ai.contracts.testing import ProjectTestReceipt
from literate_ai.projects import LoadedProject
from literate_ai.storage import FileSystemCAS, StorageSafetyError

from .errors import CliFailure

MAX_REBUILD_SOURCE_CACHE_PUBLICATION_BLOBS = 8192
MAX_REBUILD_SOURCE_CACHE_PUBLICATION_BYTES = 512 * 1024 * 1024
STANDARD_LOCAL_SOURCE_CACHE_TARGET_ID = "standard-local"
STANDARD_LOCAL_SOURCE_CACHE_REFERENCE = "standard-local-source-cache"


@dataclass(frozen=True, slots=True)
class RebuildSourceCacheInputs:
    """Normalized cache-related CLI inputs before request identity construction."""

    force_regeneration: bool
    requested_entry_identities: tuple[ContentIdentity, ...]
    operator_roots: tuple[RebuildSourceCacheOperatorRoot, ...]

    def identity_material(self, project: LoadedProject) -> dict[str, object]:
        configuration = project.definition.source_cache
        return {
            "configuration": (
                None if configuration is None else configuration.to_dict()
            ),
            "force_regeneration": self.force_regeneration,
            "requested_entry_identities": [
                item.uri for item in self.requested_entry_identities
            ],
            "operator_roots": [item.to_dict() for item in self.operator_roots],
        }


@dataclass(frozen=True, slots=True)
class RebuildSourceCacheProtocol:
    """Prepared external protocol paths and their exact CLI-owned control."""

    runtime_root: Path
    control_path: Path
    decision_path: Path
    lifecycle_path: Path
    publication_path: Path
    derivation_manifest_path: Path
    control: RebuildSourceCacheControl
    runtime_root_identity: ProtocolDirectoryIdentity
    protocol_root_identity: ProtocolDirectoryIdentity

    def require_directories_unchanged(self) -> None:
        """Require the runtime and protocol paths to retain their pinned directories."""

        if (
            protocol_directory_identity(self.runtime_root) != self.runtime_root_identity
            or protocol_directory_identity(self.control_path.parent)
            != self.protocol_root_identity
        ):
            raise RebuildSourceCacheProtocolError(
                "source-cache.protocol-directory-changed",
                "external runtime or source-cache protocol directory changed",
            )

    def add_environment(self, environment: dict[str, str]) -> None:
        self.require_directories_unchanged()
        environment[SOURCE_CACHE_CONTROL_ENVIRONMENT] = str(self.control_path)
        environment[SOURCE_CACHE_CONTROL_IDENTITY_ENVIRONMENT] = (
            self.control.identity.uri
        )
        environment[SOURCE_CACHE_DECISION_ENVIRONMENT] = str(self.decision_path)
        environment[SOURCE_CACHE_LIFECYCLE_ENVIRONMENT] = str(self.lifecycle_path)
        environment[SOURCE_CACHE_PUBLICATION_ENVIRONMENT] = str(self.publication_path)
        environment[SOURCE_CACHE_DERIVATION_MANIFEST_ENVIRONMENT] = str(
            self.derivation_manifest_path
        )
        if source_cache_protocol_path_present(
            self.decision_path,
            directory_identity=self.protocol_root_identity,
        ):
            decision = read_rebuild_source_cache_decision(
                self.decision_path,
                directory_identity=self.protocol_root_identity,
            )
            environment[SOURCE_CACHE_DECISION_IDENTITY_ENVIRONMENT] = (
                decision.identity.uri
            )
        self.require_directories_unchanged()


@dataclass(frozen=True, slots=True)
class RebuildSourceCachePlanningProtocol:
    """Pinned output namespace for the driver's non-executing planning pass."""

    runtime_root: Path
    manifest_path: Path
    runtime_root_identity: ProtocolDirectoryIdentity
    protocol_root_identity: ProtocolDirectoryIdentity

    def require_directories_unchanged(self) -> None:
        if (
            protocol_directory_identity(self.runtime_root) != self.runtime_root_identity
            or protocol_directory_identity(self.manifest_path.parent)
            != self.protocol_root_identity
        ):
            raise RebuildSourceCacheProtocolError(
                "source-cache.protocol-directory-changed",
                "external runtime or source-cache planning directory changed",
            )

    def add_environment(
        self,
        environment: dict[str, str],
        *,
        planning_request_identity: ContentIdentity,
    ) -> None:
        self.require_directories_unchanged()
        environment[SOURCE_CACHE_PLANNING_MODE_ENVIRONMENT] = SOURCE_CACHE_PLANNING_MODE
        environment[SOURCE_CACHE_DERIVATION_MANIFEST_ENVIRONMENT] = str(
            self.manifest_path
        )
        environment[SOURCE_CACHE_PLANNING_REQUEST_IDENTITY_ENVIRONMENT] = (
            planning_request_identity.uri
        )

    def load_manifest(self) -> RebuildSourceCacheDerivationManifest:
        self.require_directories_unchanged()
        result = read_rebuild_source_cache_derivation_manifest(
            self.manifest_path,
            directory_identity=self.protocol_root_identity,
        )
        self.require_directories_unchanged()
        return result


def parse_rebuild_source_cache_inputs(
    project: LoadedProject,
    *,
    force_regeneration: bool,
    entry_values: Sequence[str],
    root_values: Sequence[str],
    standard_local_root: Path | None = None,
) -> RebuildSourceCacheInputs:
    """Validate cache CLI flags without opening or mutating a cache target.

    The initialized project's one canonical disposable cache is already bound by the
    rebuild runtime root.  Custom operator roots remain explicit operator authority.
    """

    if not isinstance(force_regeneration, bool):
        raise CliFailure(
            "rebuild.source_cache_flag_invalid",
            "force regeneration must be a boolean CLI flag",
        )
    try:
        entries = tuple(
            sorted(
                (ContentIdentity.parse_uri(value) for value in entry_values),
                key=lambda item: item.uri,
            )
        )
    except (TypeError, ValueError) as exc:
        raise CliFailure(
            "rebuild.source_cache_entry_invalid",
            "every explicit source-cache entry must be a sha256 content identity",
        ) from exc
    if len({item.uri for item in entries}) != len(entries):
        raise CliFailure(
            "rebuild.source_cache_entry_duplicate",
            "explicit source-cache entry identities must be unique",
        )
    if force_regeneration and entries:
        raise CliFailure(
            "rebuild.source_cache_selection_conflict",
            "--force-regeneration cannot be combined with --source-cache-entry",
        )
    bindings: dict[str, RebuildSourceCacheOperatorRoot] = {}
    for raw in root_values:
        reference, separator, configured_path = raw.partition("=")
        if not separator or not reference or not configured_path:
            raise CliFailure(
                "rebuild.source_cache_root_invalid",
                "source-cache roots must use BINDING=/absolute/path form",
            )
        if reference in bindings:
            raise CliFailure(
                "rebuild.source_cache_root_duplicate",
                "each source-cache root binding may be supplied only once",
            )
        path = _normalized_external_root(configured_path)
        try:
            bindings[reference] = RebuildSourceCacheOperatorRoot(reference, str(path))
        except (TypeError, ValueError) as exc:
            raise CliFailure(
                "rebuild.source_cache_root_invalid",
                "source-cache root binding is not portable or absolute",
            ) from exc
    configuration = project.definition.source_cache
    required = (
        set()
        if configuration is None or configuration.mode is SourceCacheMode.OFF
        else {
            target.root_reference
            for target in configuration.targets
            if target.root_kind is SourceCacheRootKind.OPERATOR_BOUND
        }
    )
    standard_local_is_required = configuration is not None and any(
        target.target_id == STANDARD_LOCAL_SOURCE_CACHE_TARGET_ID
        and target.root_kind is SourceCacheRootKind.OPERATOR_BOUND
        and target.root_reference == STANDARD_LOCAL_SOURCE_CACHE_REFERENCE
        for target in configuration.targets
    )
    if (
        standard_local_is_required
        and STANDARD_LOCAL_SOURCE_CACHE_REFERENCE not in bindings
    ):
        if standard_local_root is None:
            raise CliFailure(
                "rebuild.source_cache_root_binding_mismatch",
                "the standard local source cache requires the rebuild runtime root",
            )
        bindings[STANDARD_LOCAL_SOURCE_CACHE_REFERENCE] = (
            RebuildSourceCacheOperatorRoot(
                STANDARD_LOCAL_SOURCE_CACHE_REFERENCE,
                str(standard_local_root),
            )
        )
    if set(bindings) != required:
        raise CliFailure(
            "rebuild.source_cache_root_binding_mismatch",
            "--source-cache-root must bind every and only configured operator root",
        )
    if configuration is None and entries:
        raise CliFailure(
            "rebuild.source_cache_unconfigured",
            "--source-cache-entry requires a configured project source cache",
        )
    if configuration is not None and not configuration.mode.can_read and entries:
        raise CliFailure(
            "rebuild.source_cache_read_disabled",
            "--source-cache-entry requires a cache mode with read authority",
        )
    return RebuildSourceCacheInputs(
        force_regeneration,
        entries,
        tuple(bindings[key] for key in sorted(bindings)),
    )


def prepare_rebuild_source_cache_planning_protocol(
    *, runtime_root: Path
) -> RebuildSourceCachePlanningProtocol:
    """Create and pin the one-shot namespace for a read-only planning pass."""

    protocol_directory = runtime_root / SOURCE_CACHE_PROTOCOL_DIRECTORY
    manifest_path = source_cache_derivation_manifest_path(runtime_root)
    try:
        protocol_directory.mkdir(mode=0o700)
        runtime_identity = protocol_directory_identity(runtime_root)
        protocol_identity = protocol_directory_identity(protocol_directory)
        if source_cache_protocol_path_present(
            manifest_path,
            directory_identity=protocol_identity,
        ):
            raise OSError
    except (OSError, RebuildSourceCacheProtocolError) as exc:
        raise CliFailure(
            "rebuild.source_cache_planning_protocol_failed",
            "source-cache planning namespace could not be established",
        ) from exc
    protocol = RebuildSourceCachePlanningProtocol(
        runtime_root=runtime_root,
        manifest_path=manifest_path,
        runtime_root_identity=runtime_identity,
        protocol_root_identity=protocol_identity,
    )
    try:
        protocol.require_directories_unchanged()
    except RebuildSourceCacheProtocolError as exc:
        raise CliFailure(
            "rebuild.source_cache_planning_protocol_failed",
            "source-cache planning namespace changed while it was established",
        ) from exc
    return protocol


def prepare_rebuild_source_cache_protocol(
    project: LoadedProject,
    *,
    planning_protocol: RebuildSourceCachePlanningProtocol,
    derivation_manifest: RebuildSourceCacheDerivationManifest,
    lifecycle_request_identity: ContentIdentity,
    project_revision_identity: ContentIdentity,
    inputs: RebuildSourceCacheInputs,
) -> RebuildSourceCacheProtocol:
    """Write the exact CLI-owned control before invoking the project driver."""

    control = RebuildSourceCacheControl(
        lifecycle_request_identity=lifecycle_request_identity,
        project_revision_identity=project_revision_identity,
        configuration=project.definition.source_cache,
        derivation_manifest=derivation_manifest,
        component_lock_identities=derivation_manifest.component_lock_identities,
        force_regeneration=inputs.force_regeneration,
        requested_entry_identities=inputs.requested_entry_identities,
        operator_roots=inputs.operator_roots,
    )
    control_path, decision_path, lifecycle_path, publication_path = (
        source_cache_protocol_paths(planning_protocol.runtime_root)
    )
    try:
        planning_protocol.require_directories_unchanged()
        runtime_identity = planning_protocol.runtime_root_identity
        protocol_identity = planning_protocol.protocol_root_identity
        write_rebuild_source_cache_control(
            control_path,
            control,
            directory_identity=protocol_identity,
        )
        configuration = control.configuration
        if configuration is None or configuration.mode is SourceCacheMode.OFF:
            write_rebuild_source_cache_decision(
                decision_path,
                RebuildSourceCacheDecision.fresh(control),
                directory_identity=protocol_identity,
            )
    except (OSError, RebuildSourceCacheProtocolError, TypeError, ValueError) as exc:
        raise CliFailure(
            "rebuild.source_cache_protocol_failed",
            "source-cache control could not be established in the external runtime",
        ) from exc
    protocol = RebuildSourceCacheProtocol(
        runtime_root=planning_protocol.runtime_root,
        control_path=control_path,
        decision_path=decision_path,
        lifecycle_path=lifecycle_path,
        publication_path=publication_path,
        derivation_manifest_path=planning_protocol.manifest_path,
        control=control,
        runtime_root_identity=runtime_identity,
        protocol_root_identity=protocol_identity,
    )
    try:
        protocol.require_directories_unchanged()
    except RebuildSourceCacheProtocolError as exc:
        raise CliFailure(
            "rebuild.source_cache_protocol_failed",
            "source-cache protocol changed while it was established",
        ) from exc
    return protocol


def validate_rebuild_source_cache_decision(
    protocol: RebuildSourceCacheProtocol,
    *,
    receipt_evidence: Mapping[str, ContentIdentity],
) -> RebuildSourceCacheDecision:
    """Independently validate the driver's canonical decision and receipt binding."""

    configured = protocol.control.configuration
    try:
        protocol.require_directories_unchanged()
        persisted_control = read_rebuild_source_cache_control(
            protocol.control_path,
            directory_identity=protocol.protocol_root_identity,
        )
        if persisted_control != protocol.control:
            raise RebuildSourceCacheProtocolError(
                "source-cache.control-changed",
                "CLI-owned source-cache control changed during the rebuild",
            )
        persisted_manifest = read_rebuild_source_cache_derivation_manifest(
            protocol.derivation_manifest_path,
            directory_identity=protocol.protocol_root_identity,
        )
        if persisted_manifest != protocol.control.derivation_manifest:
            raise RebuildSourceCacheProtocolError(
                "source-cache.derivation-manifest-changed",
                "outer-owned derivation manifest changed during the rebuild",
            )
        decision = read_rebuild_source_cache_decision(
            protocol.decision_path,
            directory_identity=protocol.protocol_root_identity,
        )
        decision.validate_against(protocol.control)
        protocol.require_directories_unchanged()
    except (RebuildSourceCacheProtocolError, TypeError, ValueError) as exc:
        code = (
            "rebuild.source_cache_driver_unaware"
            if configured is not None and configured.mode is not SourceCacheMode.OFF
            else "rebuild.source_cache_decision_invalid"
        )
        raise CliFailure(
            code,
            "configured lifecycle driver did not return a valid exact "
            "source-cache decision",
        ) from exc
    evidence_identity = receipt_evidence.get("source-cache-decision")
    if evidence_identity != decision.identity:
        raise CliFailure(
            "rebuild.source_cache_receipt_mismatch",
            "candidate receipt does not bind the exact source-cache decision",
        )
    return decision


def validate_rebuild_source_cache_lifecycle(
    protocol: RebuildSourceCacheProtocol,
    decision: RebuildSourceCacheDecision,
    receipt: ProjectTestReceipt,
) -> RebuildSourceCacheLifecycleBinding:
    """Validate exact current per-derivation lifecycle membership."""

    try:
        protocol.require_directories_unchanged()
        lifecycle = read_rebuild_source_cache_lifecycle(
            protocol.lifecycle_path,
            directory_identity=protocol.protocol_root_identity,
        )
        lifecycle.validate_against(protocol.control, decision, receipt)
        protocol.require_directories_unchanged()
    except (RebuildSourceCacheProtocolError, TypeError, ValueError) as exc:
        raise CliFailure(
            "rebuild.source_cache_lifecycle_invalid",
            "lifecycle driver did not bind every exact derivation key to current "
            "build, test, acceptance, workspace, provenance, index, and SBOM evidence",
        ) from exc
    return lifecycle


def publish_rebuild_source_cache_offer(
    project: LoadedProject,
    protocol: RebuildSourceCacheProtocol,
    decision: RebuildSourceCacheDecision,
    lifecycle: RebuildSourceCacheLifecycleBinding,
    receipt: ProjectTestReceipt,
    *,
    phases: Sequence[str],
) -> tuple[ContentIdentity, ...]:
    """Publish only after the current receipt and guarded lifecycle were validated."""

    publication_enabled = "publish-source-cache" in phases
    if not publication_enabled:
        try:
            protocol.require_directories_unchanged()
            unexpected_offer = source_cache_protocol_path_present(
                protocol.publication_path,
                directory_identity=protocol.protocol_root_identity,
            )
            protocol.require_directories_unchanged()
        except RebuildSourceCacheProtocolError as exc:
            raise CliFailure(
                "rebuild.source_cache_publication_invalid",
                "source-cache publication namespace changed during validation",
            ) from exc
        if unexpected_offer:
            raise CliFailure(
                "rebuild.source_cache_publication_unauthorized",
                "driver returned a cache publication offer without the lifecycle phase",
            )
        return ()
    configuration = protocol.control.configuration
    if configuration is None or not configuration.mode.can_write:
        raise CliFailure(
            "rebuild.source_cache_publication_unconfigured",
            "publish-source-cache requires a configured cache write mode",
        )
    evidence = {item.kind: item.identity for item in receipt.evidence}
    try:
        protocol.require_directories_unchanged()
        offer = read_rebuild_source_cache_publication(
            protocol.publication_path,
            directory_identity=protocol.protocol_root_identity,
        )
        offer.validate_against(protocol.control, decision, lifecycle)
        _require_bounded_publication_work(offer)
        protocol.require_directories_unchanged()
    except (RebuildSourceCacheProtocolError, TypeError, ValueError) as exc:
        raise CliFailure(
            "rebuild.source_cache_publication_invalid",
            "driver did not return a valid source-cache publication offer",
        ) from exc
    if (
        offer.receipt_subject_identity != receipt.subject_identity
        or offer.acceptance_evidence_identity != evidence.get("acceptance-result")
        or offer.workspace_admission_identity != evidence.get("workspace-admission")
        or offer.publication_result_identity
        != evidence.get("source-cache-publication-result")
    ):
        raise CliFailure(
            "rebuild.source_cache_publication_receipt_mismatch",
            "publication offer does not bind the current independent acceptance, "
            "workspace admission, and expected immutable publication result",
        )
    try:
        resolver = SourceCacheResolver.from_configuration(
            configuration,
            project=project,
            operator_roots={
                item.reference: Path(item.path)
                for item in protocol.control.operator_roots
            },
        )
        published: list[ContentIdentity] = []
        for member in offer.members:
            protocol.require_directories_unchanged()
            cas_root = _runtime_member_path(
                protocol.runtime_root, member.caller_cas_path
            )
            caller_cas = FileSystemCAS(cas_root, create=False)
            identity = resolver.publish(
                member.entry,
                caller_cas=caller_cas,
                intelligence_attachments=member.intelligence_attachments,
            )
            if identity != member.entry.identity:
                raise SourceCacheError(
                    "source-cache.publication-mismatch",
                    "cache target returned another immutable entry identity",
                )
            published.append(identity)
            protocol.require_directories_unchanged()
    except (OSError, SourceCacheError, StorageSafetyError, ValueError) as exc:
        raise CliFailure(
            "rebuild.source_cache_publication_failed",
            "accepted source could not be published to the configured immutable cache",
        ) from exc
    return tuple(published)


def _require_bounded_publication_work(
    offer: RebuildSourceCachePublicationOffer,
) -> None:
    blobs: dict[str, int] = {}
    for member in offer.members:
        entry = member.entry
        references = [source.blob for source in entry.source_files]
        references.extend(
            (
                entry.source_sbom,
                entry.resolved_sbom,
                entry.generated_test_suite,
                entry.build_evidence,
                entry.test_evidence,
                entry.acceptance_evidence,
                entry.provenance_evidence,
            )
        )
        references.extend(
            attachment.artifact for attachment in member.intelligence_attachments
        )
        for reference in references:
            previous = blobs.setdefault(reference.identity, reference.size)
            if previous != reference.size:
                raise ValueError("one blob identity declares conflicting sizes")
    if len(blobs) > MAX_REBUILD_SOURCE_CACHE_PUBLICATION_BLOBS or (
        sum(blobs.values()) > MAX_REBUILD_SOURCE_CACHE_PUBLICATION_BYTES
    ):
        raise ValueError("source-cache publication work exceeds its bounded budget")


def _normalized_external_root(value: str) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute() or candidate.is_symlink():
        raise CliFailure(
            "rebuild.source_cache_root_invalid",
            "source-cache operator roots must be non-symbolic absolute paths",
        )
    try:
        if candidate.exists():
            target = candidate.resolve(strict=True)
            if not target.is_dir():
                raise OSError
            return target
        parent = candidate.parent.resolve(strict=True)
        if parent.is_symlink() or not parent.is_dir():
            raise OSError
        return parent / candidate.name
    except OSError as exc:
        raise CliFailure(
            "rebuild.source_cache_root_invalid",
            "source-cache root must exist or have an existing regular parent",
        ) from exc


def _runtime_member_path(runtime_root: Path, relative: str) -> Path:
    candidate = runtime_root.joinpath(*PurePosixPath(relative).parts)
    current = runtime_root
    try:
        for part in PurePosixPath(relative).parts:
            current /= part
            if current.is_symlink():
                raise OSError
        target = candidate.resolve(strict=True)
        if not target.is_dir() or not target.is_relative_to(runtime_root):
            raise OSError
        return target
    except OSError as exc:
        raise CliFailure(
            "rebuild.source_cache_cas_unsafe",
            "publication caller CAS must be a regular directory inside runtime root",
        ) from exc


__all__ = [
    "MAX_REBUILD_SOURCE_CACHE_PUBLICATION_BLOBS",
    "MAX_REBUILD_SOURCE_CACHE_PUBLICATION_BYTES",
    "RebuildSourceCacheInputs",
    "RebuildSourceCacheProtocol",
    "STANDARD_LOCAL_SOURCE_CACHE_REFERENCE",
    "STANDARD_LOCAL_SOURCE_CACHE_TARGET_ID",
    "parse_rebuild_source_cache_inputs",
    "prepare_rebuild_source_cache_protocol",
    "publish_rebuild_source_cache_offer",
    "validate_rebuild_source_cache_decision",
    "validate_rebuild_source_cache_lifecycle",
]
