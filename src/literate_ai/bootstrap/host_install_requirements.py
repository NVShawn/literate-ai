"""Dependency-free contracts for base plus OS host-toolchain CycloneDX SBOMs."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePath, PurePosixPath, PureWindowsPath
from urllib.parse import urlsplit

CYCLONEDX_SCHEMA = "http://cyclonedx.org/schema/bom-1.7.schema.json"
CYCLONEDX_FORMAT = "CycloneDX"
CYCLONEDX_VERSION = "1.7"
PROPERTY_PREFIX = "literate-ai:host-install:"
BASE_PROPERTY_PREFIX = "literate-ai:base-toolchain:"
HOST_INSTALL_SBOM_RELATIVE_PATH = PurePosixPath("flavors")
BASE_TOOLCHAIN_SBOM_RELATIVE_PATH = PurePosixPath(
    "flavors", "os-base", "toolchain.cdx.json"
)

_SAFE_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SAFE_COMMAND = re.compile(r"^[A-Za-z0-9_.+=,@%{}/:$?()\\ -]+$")
_VERSION = re.compile(r"(?<![0-9])([0-9]+(?:\.[0-9]+){0,3})")
_DIGESTS = {
    "sha256": re.compile(r"^[0-9a-f]{64}$"),
    "sha512": re.compile(r"^[0-9a-f]{128}$"),
}
_ARCHITECTURES = {
    "aarch64": "arm64",
    "amd64": "x86_64",
    "arm64": "arm64",
    "x64": "x86_64",
    "x86_64": "x86_64",
}


class HostInstallRequirementError(ValueError):
    """A host tuple or its installation SBOM is malformed."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, order=True, slots=True)
class HostInstallTarget:
    """One explicitly supported native package-management target."""

    operating_system: str
    architecture: str
    accelerator: str = "none"

    def __post_init__(self) -> None:
        for label, value in (
            ("operating system", self.operating_system),
            ("architecture", self.architecture),
            ("accelerator", self.accelerator),
        ):
            if _SAFE_IDENTIFIER.fullmatch(value) is None:
                raise HostInstallRequirementError(
                    "host-install.target-invalid",
                    f"host-install {label} is invalid: {value!r}",
                )

    @property
    def relative_sbom_path(self) -> PurePosixPath:
        if self.operating_system == "macos":
            flavor = "os-macos"
        elif self.operating_system == "windows":
            flavor = "os-windows"
        elif self.operating_system.startswith("linux-"):
            flavor = "os-linux"
        else:
            flavor = "os-" + self.operating_system.removeprefix("linux-")
        return PurePosixPath(
            flavor,
            "host-install",
            self.operating_system,
            self.architecture,
            f"{self.accelerator}.cdx.json",
        )

    def display(self) -> str:
        return (
            f"os={self.operating_system}, arch={self.architecture}, "
            f"gpu={self.accelerator}"
        )


@dataclass(frozen=True, slots=True)
class NativePackageManagerRequirement:
    """Declarative native package-manager invocation contract."""

    identifier: str
    executable: str
    query_executable: str
    query_arguments: tuple[str, ...]
    install_executable: str
    install_mode: str
    install_arguments: tuple[str, ...]
    preinstall_executable: str | None = None
    preinstall_arguments: tuple[str, ...] = ()
    elevation_executable: str | None = None


@dataclass(frozen=True, slots=True)
class HostPackageRequirement:
    """One native package and the behavior that proves it usable."""

    bom_ref: str
    capability: str
    name: str
    manager_package: str
    version_constraint: str
    probe_kind: str
    probe_candidates: tuple[str, ...]
    probe_arguments: tuple[str, ...]
    reason: str
    install_arguments: tuple[str, ...] = ()
    path_entries: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class UniversalCapabilityRequirement:
    """One platform-neutral executable or runtime capability."""

    bom_ref: str
    capability: str
    name: str
    version_constraint: str
    probe_kind: str
    probe_candidates: tuple[str, ...]
    probe_arguments: tuple[str, ...]
    reason: str
    group: str | None = None
    installer_kind: str | None = None
    installer_package: str | None = None


@dataclass(frozen=True, slots=True)
class UniversalToolchainSbom:
    """Validated, possibly composed logical capability authority."""

    sources: tuple[str, ...]
    default_coding_agent: str | None
    capabilities: tuple[UniversalCapabilityRequirement, ...]
    warnings: tuple[ToolchainCompositionWarning, ...] = ()

    def capability(self, name: str) -> UniversalCapabilityRequirement:
        for item in self.capabilities:
            if item.capability == name:
                return item
        raise HostInstallRequirementError(
            "host-install.capability-unknown",
            f"base toolchain has no capability {name!r}",
        )

    @property
    def coding_agents(self) -> tuple[UniversalCapabilityRequirement, ...]:
        return tuple(item for item in self.capabilities if item.group == "coding-agent")


@dataclass(frozen=True, slots=True)
class ToolchainCompositionWarning:
    """One harmless duplicate capability claimed by multiple Flavor mix-ins."""

    capability: str
    sources: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "code": "host-install.duplicate-capability",
            "capability": self.capability,
            "sources": list(self.sources),
        }


@dataclass(frozen=True, slots=True)
class HostManagedArtifact:
    """One integrity-pinned, tuple-specific archive realization."""

    bom_ref: str
    name: str
    version: str
    capabilities: tuple[str, ...]
    url: str
    digest_algorithm: str
    digest: str
    archive_format: str
    strip_prefix: str
    path_entries: tuple[str, ...]
    executable_path: str
    executable_name: str
    required_runtime_paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _NativePackage:
    """Validated tuple-specific native-package realization."""

    bom_ref: str
    name: str
    version: str
    manager_package: str
    capabilities: tuple[str, ...]
    probe_candidates: tuple[str, ...] = ()
    probe_arguments: tuple[str, ...] = ()
    install_arguments: tuple[str, ...] = ()
    path_entries: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class HostInstallSbom:
    """Validated projection of the bootstrap-relevant SBOM information."""

    target: HostInstallTarget
    base: UniversalToolchainSbom
    package_manager: NativePackageManagerRequirement
    packages: tuple[HostPackageRequirement, ...]
    artifacts: tuple[HostManagedArtifact, ...]


def normalize_architecture(machine: str) -> str:
    """Normalize only architectures for which a tuple may be declared."""

    normalized = machine.strip().casefold().replace("-", "_")
    return _ARCHITECTURES.get(normalized, normalized or "unknown")


def detect_host_install_target(
    *,
    system: str,
    machine: str,
    os_release: Mapping[str, str] | None = None,
    accelerator: str = "none",
) -> HostInstallTarget:
    """Project host observations to a stable install-SBOM tuple."""

    normalized_system = system.strip().casefold()
    if normalized_system == "darwin":
        operating_system = "macos"
    elif normalized_system == "windows":
        operating_system = "windows"
    elif normalized_system == "linux":
        release = {
            key.casefold(): value.strip().casefold()
            for key, value in (os_release or {}).items()
        }
        lineage = " ".join((release.get("id", ""), release.get("id_like", "")))
        if any(item in lineage.split() for item in ("debian", "ubuntu")):
            operating_system = "linux-debian"
        else:
            operating_system = "linux-" + (release.get("id") or "unknown")
    else:
        operating_system = normalized_system or "unknown"
    return HostInstallTarget(
        operating_system,
        normalize_architecture(machine),
        accelerator.strip().casefold() or "none",
    )


def parse_os_release(content: str) -> dict[str, str]:
    """Parse the bounded key/value subset needed from /etc/os-release."""

    result: dict[str, str] = {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key in {"ID", "ID_LIKE"}:
            result[key] = value
    return result


def supported_targets(paths: Sequence[PurePath]) -> tuple[HostInstallTarget, ...]:
    """Derive supported tuples from bounded relative SBOM filenames."""

    targets: set[HostInstallTarget] = set()
    for path in paths:
        parts = PurePosixPath(path.as_posix()).parts
        if (
            len(parts) != 5
            or not parts[0].startswith("os-")
            or parts[1] != "host-install"
            or not parts[4].endswith(".cdx.json")
        ):
            continue
        targets.add(HostInstallTarget(parts[2], parts[3], parts[4][:-9]))
    return tuple(sorted(targets))


def parse_base_toolchain_sbom(document: object) -> UniversalToolchainSbom:
    """Validate the logical capability SBOM owned by the os-base Flavor."""

    root = _cyclonedx_root(document, "base-toolchain SBOM")
    metadata = _mapping(root.get("metadata"), "base-toolchain SBOM metadata")
    properties = _properties(
        metadata.get("properties"), "metadata", prefix=BASE_PROPERTY_PREFIX
    )
    if (
        _required_property(properties, "role", prefix=BASE_PROPERTY_PREFIX)
        != "universal"
    ):
        raise HostInstallRequirementError(
            "host-install.base-role-invalid",
            "os-base toolchain SBOM must declare the universal role",
        )
    default = _identifier_property(
        properties, "default-coding-agent", prefix=BASE_PROPERTY_PREFIX
    )
    components = root.get("components")
    if not isinstance(components, list) or not components:
        raise HostInstallRequirementError(
            "host-install.capabilities-missing",
            "os-base toolchain SBOM must declare capabilities",
        )
    capabilities = tuple(_base_capability(item) for item in components)
    names = tuple(item.capability for item in capabilities)
    if len(names) != len(set(names)):
        raise HostInstallRequirementError(
            "host-install.capability-duplicate",
            "os-base toolchain SBOM repeats a capability identifier",
        )
    coding_agents = tuple(item for item in capabilities if item.group == "coding-agent")
    if len(coding_agents) < 2 or default not in {
        item.capability for item in coding_agents
    }:
        raise HostInstallRequirementError(
            "host-install.coding-agent-group-invalid",
            "os-base must declare multiple coding agents and a member default",
        )
    if any(
        item.group not in {None, "coding-agent"}
        or (item.group is None and item.installer_kind is not None)
        or (item.group == "coding-agent" and item.installer_kind is None)
        for item in capabilities
    ):
        raise HostInstallRequirementError(
            "host-install.capability-group-invalid",
            "only coding-agent alternatives may declare managed installers",
        )
    _validate_dependency_graph(
        root, metadata, tuple(item.bom_ref for item in capabilities)
    )
    source = _toolchain_source(metadata)
    return UniversalToolchainSbom((source,), default, capabilities)


def parse_flavor_toolchain_sbom(document: object) -> UniversalToolchainSbom:
    """Validate one non-base Flavor's logical host-toolchain contribution."""

    root = _cyclonedx_root(document, "Flavor toolchain SBOM")
    metadata = _mapping(root.get("metadata"), "Flavor toolchain SBOM metadata")
    properties = _properties(
        metadata.get("properties"), "metadata", prefix=BASE_PROPERTY_PREFIX
    )
    if _required_property(properties, "role", prefix=BASE_PROPERTY_PREFIX) != "mixin":
        raise HostInstallRequirementError(
            "host-install.flavor-role-invalid",
            "Flavor toolchain SBOM must declare the mixin role",
        )
    components = root.get("components")
    if not isinstance(components, list) or not components:
        raise HostInstallRequirementError(
            "host-install.capabilities-missing",
            "Flavor toolchain SBOM must declare capabilities",
        )
    capabilities = tuple(_base_capability(item) for item in components)
    names = tuple(item.capability for item in capabilities)
    if len(names) != len(set(names)):
        raise HostInstallRequirementError(
            "host-install.capability-duplicate",
            "Flavor toolchain SBOM repeats a capability identifier",
        )
    if any(
        item.group is not None or item.installer_kind is not None
        for item in capabilities
    ):
        raise HostInstallRequirementError(
            "host-install.flavor-capability-invalid",
            "only os-base may declare alternative or managed provider capabilities",
        )
    _validate_dependency_graph(
        root, metadata, tuple(item.bom_ref for item in capabilities)
    )
    return UniversalToolchainSbom((_toolchain_source(metadata),), None, capabilities)


def compose_toolchain_sboms(
    toolchains: Sequence[UniversalToolchainSbom],
) -> UniversalToolchainSbom:
    """Compose Flavor mix-ins, deduplicating identical capabilities with warnings."""

    if not toolchains:
        raise HostInstallRequirementError(
            "host-install.toolchain-empty", "at least one toolchain SBOM is required"
        )
    selected: dict[str, UniversalCapabilityRequirement] = {}
    owners: dict[str, list[str]] = {}
    warnings: list[ToolchainCompositionWarning] = []
    default: str | None = None
    sources: list[str] = []
    for toolchain in toolchains:
        if toolchain.default_coding_agent is not None:
            if default is not None and default != toolchain.default_coding_agent:
                raise HostInstallRequirementError(
                    "host-install.coding-agent-default-conflict",
                    "selected toolchains disagree on the default coding agent",
                )
            default = toolchain.default_coding_agent
        for source in toolchain.sources:
            if source not in sources:
                sources.append(source)
        for requirement in toolchain.capabilities:
            prior = selected.get(requirement.capability)
            if prior is None:
                selected[requirement.capability] = requirement
                owners[requirement.capability] = list(toolchain.sources)
                continue
            if _capability_contract(prior) != _capability_contract(requirement):
                raise HostInstallRequirementError(
                    "host-install.capability-conflict",
                    f"selected Flavors disagree on capability "
                    f"{requirement.capability!r}",
                )
            for source in toolchain.sources:
                if source not in owners[requirement.capability]:
                    owners[requirement.capability].append(source)
            warnings.append(
                ToolchainCompositionWarning(
                    requirement.capability,
                    tuple(owners[requirement.capability]),
                )
            )
    deduplicated_warnings = {item.capability: item for item in warnings}
    return UniversalToolchainSbom(
        tuple(sources),
        default,
        tuple(selected.values()),
        tuple(deduplicated_warnings.values()),
    )


def parse_host_install_sbom(
    document: object,
    *,
    base: UniversalToolchainSbom,
    catalog: UniversalToolchainSbom | None = None,
    expected_target: HostInstallTarget | None = None,
) -> HostInstallSbom:
    """Compose one OS realization SBOM with the os-base capability authority."""

    root = _cyclonedx_root(document, "host-install SBOM")
    metadata = _mapping(root.get("metadata"), "host-install SBOM metadata")
    properties = _properties(metadata.get("properties"), "metadata")
    target = HostInstallTarget(
        _required_property(properties, "operating-system"),
        _required_property(properties, "architecture"),
        _required_property(properties, "accelerator"),
    )
    if expected_target is not None and target != expected_target:
        raise HostInstallRequirementError(
            "host-install.sbom-target-mismatch",
            f"SBOM declares {target.display()}, expected {expected_target.display()}",
        )
    manager = NativePackageManagerRequirement(
        identifier=_identifier_property(properties, "package-manager"),
        executable=_command_property(properties, "manager-executable"),
        query_executable=_command_property(properties, "query-executable"),
        query_arguments=_arguments_property(properties, "query-arguments"),
        install_executable=_command_property(properties, "install-executable"),
        install_mode=_identifier_property(properties, "install-mode"),
        install_arguments=_arguments_property(properties, "install-arguments"),
        preinstall_executable=_optional_command_property(
            properties, "preinstall-executable"
        ),
        preinstall_arguments=_arguments_property(
            properties, "preinstall-arguments", required=False
        ),
        elevation_executable=_optional_command_property(
            properties, "elevation-executable"
        ),
    )
    if "{package}" not in manager.query_arguments:
        raise HostInstallRequirementError(
            "host-install.query-template-invalid",
            "package-manager query arguments must contain one {package} token",
        )
    install_token = "{packages}" if manager.install_mode == "batch" else "{package}"
    if manager.install_mode not in {"batch", "each"}:
        raise HostInstallRequirementError(
            "host-install.install-mode-invalid",
            "package-manager install mode must be batch or each",
        )
    if install_token not in manager.install_arguments:
        raise HostInstallRequirementError(
            "host-install.install-template-invalid",
            f"package-manager install arguments must contain one {install_token} token",
        )
    components = root.get("components")
    if not isinstance(components, list) or not components:
        raise HostInstallRequirementError(
            "host-install.packages-missing",
            "host-install SBOM must declare at least one native package component",
        )
    realizations = tuple(_realization(item) for item in components)
    native = tuple(item for item in realizations if isinstance(item, _NativePackage))
    artifacts = tuple(
        item for item in realizations if isinstance(item, HostManagedArtifact)
    )
    names = [item.manager_package for item in native]
    if len(names) != len(set(names)):
        raise HostInstallRequirementError(
            "host-install.package-duplicate",
            "host-install SBOM repeats a native package identifier",
        )
    realized = [capability for item in realizations for capability in item.capabilities]
    if len(realized) != len(set(realized)):
        raise HostInstallRequirementError(
            "host-install.capability-realization-duplicate",
            "host-install SBOM realizes one base capability more than once",
        )
    capability_catalog = catalog or base
    base_names = {item.capability for item in capability_catalog.capabilities}
    unknown = sorted(set(realized) - base_names)
    required = {item.capability for item in base.capabilities}
    missing = sorted(required - set(realized))
    if unknown or missing:
        detail = []
        if unknown:
            detail.append("unknown: " + ", ".join(unknown))
        if missing:
            detail.append("missing: " + ", ".join(missing))
        raise HostInstallRequirementError(
            "host-install.capability-closure-invalid",
            "OS realization does not close os-base capabilities ("
            + "; ".join(detail)
            + ")",
        )
    packages = tuple(
        HostPackageRequirement(
            item.bom_ref,
            capability,
            base_requirement.name,
            item.manager_package,
            base_requirement.version_constraint,
            base_requirement.probe_kind,
            item.probe_candidates or base_requirement.probe_candidates,
            (
                item.probe_arguments
                if item.probe_candidates
                else base_requirement.probe_arguments
            ),
            base_requirement.reason,
            item.install_arguments,
            item.path_entries,
        )
        for item in native
        for capability in item.capabilities
        for base_requirement in (capability_catalog.capability(capability),)
    )
    _validate_dependency_graph(
        root, metadata, tuple(item.bom_ref for item in realizations)
    )
    return HostInstallSbom(target, base, manager, packages, artifacts)


def version_satisfies(observed: str, constraint: str) -> bool:
    """Evaluate the deliberately small bootstrap version-range vocabulary."""

    match = _VERSION.search(observed)
    if match is None:
        return False
    value = _version_tuple(match.group(1))
    for raw_clause in constraint.split(","):
        clause = raw_clause.strip()
        operator = next(
            (item for item in (">=", "<=", "==", ">", "<") if clause.startswith(item)),
            None,
        )
        if operator is None:
            return False
        expected = _version_tuple(clause[len(operator) :].strip())
        width = max(len(value), len(expected))
        left = value + (0,) * (width - len(value))
        right = expected + (0,) * (width - len(expected))
        if not {
            ">=": left >= right,
            "<=": left <= right,
            "==": left == right,
            ">": left > right,
            "<": left < right,
        }[operator]:
            return False
    return True


def _cyclonedx_root(document: object, label: str) -> Mapping[str, object]:
    root = _mapping(document, label)
    if (
        root.get("$schema") != CYCLONEDX_SCHEMA
        or root.get("bomFormat") != CYCLONEDX_FORMAT
        or root.get("specVersion") != CYCLONEDX_VERSION
    ):
        raise HostInstallRequirementError(
            "host-install.sbom-schema-invalid",
            f"{label} must be an explicit CycloneDX {CYCLONEDX_VERSION} document",
        )
    return root


def _base_capability(value: object) -> UniversalCapabilityRequirement:
    component = _mapping(value, "base-toolchain capability component")
    bom_ref, name, version = _component_identity(component, "base capability")
    properties = _properties(
        component.get("properties"),
        f"component {name!r}",
        prefix=BASE_PROPERTY_PREFIX,
    )
    capability = _identifier_property(
        properties, "capability", prefix=BASE_PROPERTY_PREFIX
    )
    probe_kind = _identifier_property(
        properties, "probe-kind", prefix=BASE_PROPERTY_PREFIX
    )
    if probe_kind not in {
        "command",
        "current-python",
        "file-group",
        "python-module",
    }:
        raise HostInstallRequirementError(
            "host-install.probe-kind-invalid",
            f"base capability {capability!r} has unsupported probe kind {probe_kind!r}",
        )
    candidates = _string_array_property(
        properties,
        "probe-candidates",
        required=probe_kind in {"command", "file-group"},
        prefix=BASE_PROPERTY_PREFIX,
    )
    arguments = _arguments_property(
        properties,
        "probe-arguments",
        required=probe_kind in {"command", "python-module"},
        prefix=BASE_PROPERTY_PREFIX,
    )
    group = _optional_identifier_property(
        properties, "group", prefix=BASE_PROPERTY_PREFIX
    )
    installer_kind = _optional_identifier_property(
        properties, "installer-kind", prefix=BASE_PROPERTY_PREFIX
    )
    if installer_kind not in {None, "npm", "managed-archive", "host-realization"}:
        raise HostInstallRequirementError(
            "host-install.installer-kind-invalid",
            f"base capability {capability!r} has unsupported installer kind",
        )
    installer_package = _optional_property(
        properties, "installer-package", prefix=BASE_PROPERTY_PREFIX
    )
    if (installer_kind == "npm") != (installer_package is not None):
        raise HostInstallRequirementError(
            "host-install.installer-package-invalid",
            f"base capability {capability!r} must pair npm with an exact package pin",
        )
    if installer_package is not None and not _exact_npm_package(installer_package):
        raise HostInstallRequirementError(
            "host-install.installer-package-invalid",
            f"base capability {capability!r} has a non-exact npm package pin",
        )
    return UniversalCapabilityRequirement(
        bom_ref,
        capability,
        name,
        version,
        probe_kind,
        candidates,
        arguments,
        _required_property(properties, "reason", prefix=BASE_PROPERTY_PREFIX),
        group,
        installer_kind,
        installer_package,
    )


def _toolchain_source(metadata: Mapping[str, object]) -> str:
    component = _mapping(metadata.get("component"), "metadata component")
    name = component.get("name")
    if not isinstance(name, str) or not name:
        raise HostInstallRequirementError(
            "host-install.toolchain-source-invalid",
            "toolchain metadata component needs a nonempty name",
        )
    return name


def _capability_contract(
    requirement: UniversalCapabilityRequirement,
) -> tuple[object, ...]:
    return (
        requirement.capability,
        requirement.version_constraint,
        requirement.probe_kind,
        requirement.probe_candidates,
        requirement.probe_arguments,
        requirement.group,
        requirement.installer_kind,
        requirement.installer_package,
    )


def _realization(value: object) -> _NativePackage | HostManagedArtifact:
    component = _mapping(value, "host-install realization component")
    bom_ref, name, version = _component_identity(component, "host realization")
    properties = _properties(component.get("properties"), f"component {name!r}")
    kind = _identifier_property(properties, "realization-kind")
    capabilities = _identifier_array_property(properties, "base-capabilities")
    if kind == "native-package":
        native = _NativePackage(
            bom_ref,
            name,
            version,
            _required_property(properties, "manager-package"),
            capabilities,
            _string_array_property(properties, "probe-candidates", required=False),
            _arguments_property(properties, "probe-arguments", required=False),
            _arguments_property(properties, "install-arguments", required=False),
            _native_path_entries_property(properties),
        )
        if native.install_arguments and "{package}" not in native.install_arguments:
            raise HostInstallRequirementError(
                "host-install.package-install-template-invalid",
                f"native realization {name!r} install override needs {{package}}",
            )
        return native
    if kind != "managed-archive":
        raise HostInstallRequirementError(
            "host-install.realization-kind-invalid",
            f"host realization {name!r} has unsupported kind {kind!r}",
        )
    url = _required_property(properties, "artifact-url")
    parsed_url = urlsplit(url)
    if (
        parsed_url.scheme != "https"
        or not parsed_url.netloc
        or parsed_url.username is not None
        or parsed_url.password is not None
        or parsed_url.fragment
    ):
        raise HostInstallRequirementError(
            "host-install.artifact-url-invalid",
            f"managed artifact {name!r} must use an uncredentialed HTTPS URL",
        )
    digest_algorithm = _identifier_property(properties, "artifact-digest-algorithm")
    digest = _required_property(properties, "artifact-digest")
    if (
        digest_algorithm not in _DIGESTS
        or _DIGESTS[digest_algorithm].fullmatch(digest) is None
    ):
        raise HostInstallRequirementError(
            "host-install.artifact-digest-invalid",
            f"managed artifact {name!r} needs a supported lowercase digest",
        )
    archive_format = _identifier_property(properties, "archive-format")
    if archive_format not in {"file", "tar.gz", "tar.xz", "zip"}:
        raise HostInstallRequirementError(
            "host-install.archive-format-invalid",
            f"managed artifact {name!r} has unsupported archive format",
        )
    strip_prefix = _safe_relative_property(properties, "strip-prefix")
    path_entries = _safe_relative_paths_property(properties, "path-entries")
    executable_path = _safe_relative_property(properties, "executable-path")
    executable_name = _required_property(properties, "executable-name")
    if (
        not executable_name
        or "/" in executable_name
        or "\\" in executable_name
        or executable_name in {".", ".."}
    ):
        raise HostInstallRequirementError(
            "host-install.artifact-executable-invalid",
            f"managed artifact {name!r} has an unsafe executable name",
        )
    return HostManagedArtifact(
        bom_ref,
        name,
        version,
        capabilities,
        url,
        digest_algorithm,
        digest,
        archive_format,
        strip_prefix,
        path_entries,
        executable_path,
        executable_name,
        _safe_relative_paths_property(
            properties, "required-runtime-paths", required=False
        ),
    )


def _component_identity(
    component: Mapping[str, object], label: str
) -> tuple[str, str, str]:
    values = (component.get("bom-ref"), component.get("name"), component.get("version"))
    if not all(isinstance(item, str) and item for item in values):
        raise HostInstallRequirementError(
            "host-install.component-invalid",
            f"{label} needs nonempty bom-ref, name, and version",
        )
    return str(values[0]), str(values[1]), str(values[2])


def _validate_dependency_graph(
    root: Mapping[str, object],
    metadata: Mapping[str, object],
    component_refs: tuple[str, ...],
) -> None:
    root_component = _mapping(metadata.get("component"), "metadata component")
    root_ref = root_component.get("bom-ref")
    if not isinstance(root_ref, str) or not root_ref:
        raise HostInstallRequirementError(
            "host-install.root-ref-missing", "host-install root component needs bom-ref"
        )
    expected = {root_ref, *component_refs}
    dependencies = root.get("dependencies")
    if not isinstance(dependencies, list):
        raise HostInstallRequirementError(
            "host-install.dependencies-missing",
            "host-install SBOM needs an explicit dependency graph",
        )
    observed: dict[str, tuple[str, ...]] = {}
    for item in dependencies:
        entry = _mapping(item, "dependency entry")
        ref = entry.get("ref")
        depends_on = entry.get("dependsOn")
        if (
            not isinstance(ref, str)
            or not isinstance(depends_on, list)
            or not all(isinstance(value, str) for value in depends_on)
        ):
            raise HostInstallRequirementError(
                "host-install.dependency-invalid",
                "host-install dependency is malformed",
            )
        if ref in observed:
            raise HostInstallRequirementError(
                "host-install.dependency-duplicate", "duplicate dependency graph entry"
            )
        observed[ref] = tuple(depends_on)
    if set(observed) != expected or set(observed[root_ref]) != expected - {root_ref}:
        raise HostInstallRequirementError(
            "host-install.dependency-closure-invalid",
            "host-install dependency graph must cover the root and every package",
        )
    if any(observed[item] for item in component_refs):
        raise HostInstallRequirementError(
            "host-install.dependency-leaf-invalid",
            "toolchain component entries must be dependency leaves",
        )


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise HostInstallRequirementError(
            "host-install.sbom-invalid", f"{label} must be a JSON object"
        )
    return value


def _properties(
    value: object, label: str, *, prefix: str = PROPERTY_PREFIX
) -> dict[str, str]:
    if not isinstance(value, list):
        raise HostInstallRequirementError(
            "host-install.properties-invalid", f"{label} properties must be an array"
        )
    result: dict[str, str] = {}
    for item in value:
        entry = _mapping(item, f"{label} property")
        name = entry.get("name")
        content = entry.get("value")
        if (
            not isinstance(name, str)
            or not name.startswith(prefix)
            or not isinstance(content, str)
            or not content
            or name in result
        ):
            raise HostInstallRequirementError(
                "host-install.property-invalid", f"{label} has an invalid property"
            )
        result[name] = content
    return result


def _required_property(
    properties: Mapping[str, str], suffix: str, *, prefix: str = PROPERTY_PREFIX
) -> str:
    name = prefix + suffix
    try:
        return properties[name]
    except KeyError as exc:
        raise HostInstallRequirementError(
            "host-install.property-missing",
            f"host-install property {name!r} is missing",
        ) from exc


def _optional_property(
    properties: Mapping[str, str], suffix: str, *, prefix: str = PROPERTY_PREFIX
) -> str | None:
    return properties.get(prefix + suffix)


def _identifier_property(
    properties: Mapping[str, str], suffix: str, *, prefix: str = PROPERTY_PREFIX
) -> str:
    value = _required_property(properties, suffix, prefix=prefix)
    if _SAFE_IDENTIFIER.fullmatch(value) is None:
        raise HostInstallRequirementError(
            "host-install.identifier-invalid", f"invalid {suffix}: {value!r}"
        )
    return value


def _optional_identifier_property(
    properties: Mapping[str, str], suffix: str, *, prefix: str = PROPERTY_PREFIX
) -> str | None:
    if prefix + suffix not in properties:
        return None
    return _identifier_property(properties, suffix, prefix=prefix)


def _command_property(
    properties: Mapping[str, str], suffix: str, *, prefix: str = PROPERTY_PREFIX
) -> str:
    value = _required_property(properties, suffix, prefix=prefix)
    if _SAFE_COMMAND.fullmatch(value) is None or any(
        character.isspace() for character in value
    ):
        raise HostInstallRequirementError(
            "host-install.command-invalid", f"invalid {suffix}: {value!r}"
        )
    return value


def _optional_command_property(
    properties: Mapping[str, str], suffix: str, *, prefix: str = PROPERTY_PREFIX
) -> str | None:
    name = prefix + suffix
    if name not in properties:
        return None
    return _command_property(properties, suffix, prefix=prefix)


def _arguments_property(
    properties: Mapping[str, str],
    suffix: str,
    *,
    required: bool = True,
    prefix: str = PROPERTY_PREFIX,
) -> tuple[str, ...]:
    return _string_array_property(properties, suffix, required=required, prefix=prefix)


def _string_array_property(
    properties: Mapping[str, str],
    suffix: str,
    *,
    required: bool = True,
    prefix: str = PROPERTY_PREFIX,
) -> tuple[str, ...]:
    name = prefix + suffix
    if name not in properties and not required:
        return ()
    raw = _required_property(properties, suffix, prefix=prefix)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HostInstallRequirementError(
            "host-install.arguments-invalid", f"{suffix} must be a JSON string array"
        ) from exc
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item and _SAFE_COMMAND.fullmatch(item)
        for item in value
    ):
        raise HostInstallRequirementError(
            "host-install.arguments-invalid", f"{suffix} must be a JSON string array"
        )
    return tuple(value)


def _identifier_array_property(
    properties: Mapping[str, str], suffix: str
) -> tuple[str, ...]:
    values = _string_array_property(properties, suffix)
    if not values or any(_SAFE_IDENTIFIER.fullmatch(item) is None for item in values):
        raise HostInstallRequirementError(
            "host-install.identifiers-invalid",
            f"{suffix} must be a nonempty JSON array of identifiers",
        )
    if len(values) != len(set(values)):
        raise HostInstallRequirementError(
            "host-install.identifiers-duplicate",
            f"{suffix} must not repeat identifiers",
        )
    return values


def _safe_relative_property(properties: Mapping[str, str], suffix: str) -> str:
    return _safe_relative_path(_required_property(properties, suffix), label=suffix)


def _safe_relative_paths_property(
    properties: Mapping[str, str], suffix: str, *, required: bool = True
) -> tuple[str, ...]:
    values = tuple(
        _safe_relative_path(item, label=suffix)
        for item in _string_array_property(properties, suffix, required=required)
    )
    if len(values) != len(set(values)):
        raise HostInstallRequirementError(
            "host-install.artifact-paths-duplicate",
            f"managed artifact {suffix} must not repeat paths",
        )
    return values


def _safe_relative_path(value: str, *, label: str) -> str:
    if value == ".":
        return value
    candidate = PurePosixPath(value.replace("\\", "/"))
    if (
        not value
        or candidate.is_absolute()
        or any(part in {"", ".."} for part in candidate.parts)
        or ":" in candidate.parts[0]
    ):
        raise HostInstallRequirementError(
            "host-install.artifact-path-invalid",
            f"managed artifact {label} must be a safe relative path",
        )
    return candidate.as_posix()


def _native_path_entries_property(
    properties: Mapping[str, str],
) -> tuple[str, ...]:
    """Parse absolute, OS-owned search paths without embedding them in Python."""

    values = _string_array_property(properties, "path-entries", required=False)
    for value in values:
        normalized = value.replace("\\", "/")
        parts = PurePosixPath(normalized).parts
        environment_rooted = (
            re.match(r"^%[A-Za-z_][A-Za-z0-9_()]*%/", normalized) is not None
        )
        if any(part in {"", ".", ".."} for part in parts) or not (
            PurePosixPath(normalized).is_absolute()
            or PureWindowsPath(value).is_absolute()
            or environment_rooted
        ):
            raise HostInstallRequirementError(
                "host-install.native-path-invalid",
                "native package path entries must be absolute paths or "
                "environment-rooted absolute path templates",
            )
    return values


def _exact_npm_package(value: str) -> bool:
    if not value or any(character.isspace() for character in value):
        return False
    separator = value.rfind("@")
    if separator <= 0 or separator == len(value) - 1:
        return False
    package_version = value[separator + 1 :]
    return (
        re.fullmatch(
            r"[0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9._-]+)?",
            package_version,
        )
        is not None
    )


def _version_tuple(value: str) -> tuple[int, ...]:
    if re.fullmatch(r"[0-9]+(?:\.[0-9]+){0,3}", value) is None:
        raise HostInstallRequirementError(
            "host-install.version-invalid",
            f"invalid version constraint value: {value!r}",
        )
    return tuple(int(item) for item in value.split("."))


__all__ = [
    "BASE_TOOLCHAIN_SBOM_RELATIVE_PATH",
    "HostInstallRequirementError",
    "HostInstallSbom",
    "HostInstallTarget",
    "HostManagedArtifact",
    "HostPackageRequirement",
    "NativePackageManagerRequirement",
    "ToolchainCompositionWarning",
    "UniversalCapabilityRequirement",
    "UniversalToolchainSbom",
    "HOST_INSTALL_SBOM_RELATIVE_PATH",
    "detect_host_install_target",
    "compose_toolchain_sboms",
    "normalize_architecture",
    "parse_base_toolchain_sbom",
    "parse_flavor_toolchain_sbom",
    "parse_host_install_sbom",
    "parse_os_release",
    "supported_targets",
    "version_satisfies",
]
