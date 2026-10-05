"""Install composed Flavor toolchains through tuple-specific OS realizations."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from literate_ai.adapters.user_paths import resolve_host_paths
from literate_ai.bootstrap.host_install_requirements import (
    BASE_TOOLCHAIN_SBOM_RELATIVE_PATH,
    HostInstallRequirementError,
    HostInstallSbom,
    HostInstallTarget,
    HostManagedArtifact,
    HostPackageRequirement,
    UniversalCapabilityRequirement,
    UniversalToolchainSbom,
    compose_toolchain_sboms,
    detect_host_install_target,
    parse_base_toolchain_sbom,
    parse_flavor_toolchain_sbom,
    parse_host_install_sbom,
    parse_os_release,
    supported_targets,
    version_satisfies,
)

INSTALL_DEPENDENCIES_ENVIRONMENT = "LITAI_INSTALL_DEPENDENCIES"
INSTALL_ACCELERATOR_ENVIRONMENT = "LITAI_INSTALL_ACCELERATOR"
CODING_CLI_ENVIRONMENT = "CODING_CLI"

_YES = frozenset({"1", "true", "yes", "y"})
_NO = frozenset({"0", "false", "no", "n", ""})
_ENVIRONMENT_REFERENCE = re.compile(r"%([^%]+)%")
_MAX_ARCHIVE_MEMBERS = 100_000
_MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
_ARTIFACT_MANIFEST = ".literate-ai-artifact.json"


class HostInstallError(RuntimeError):
    """Host dependencies cannot be proven or installed safely."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class HostCapabilityObservation:
    """Observed provenance and runtime status for one composed capability."""

    requirement: UniversalCapabilityRequirement
    source_kind: str
    manager_package: str | None
    manager_installed: bool | None
    probe_satisfied: bool
    observed_version: str | None
    executable: Path | None

    @property
    def ready(self) -> bool:
        return self.probe_satisfied and self.manager_installed is not False

    def to_dict(self) -> dict[str, object]:
        return {
            "capability": self.requirement.capability,
            "source_kind": self.source_kind,
            "package": self.manager_package,
            "required_version": self.requirement.version_constraint,
            "manager_installed": self.manager_installed,
            "probe_satisfied": self.probe_satisfied,
            "observed_version": self.observed_version,
            "executable": str(self.executable) if self.executable else None,
            "reason": self.requirement.reason,
        }


# Compatibility name for the pre-0.9 internal adapter surface.
HostPackageObservation = HostCapabilityObservation


@dataclass(frozen=True, slots=True)
class HostInstallReport:
    """Externally meaningful composed host-toolchain preflight result."""

    base_sbom_path: Path
    sbom_path: Path
    sbom: HostInstallSbom
    observations: tuple[HostCapabilityObservation, ...]
    coding_agent: str
    managed_paths: tuple[Path, ...] = ()
    installed: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return all(item.ready for item in self.observations)

    @property
    def missing(self) -> tuple[HostCapabilityObservation, ...]:
        return tuple(item for item in self.observations if not item.ready)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "literate-ai/host-install-report@2",
            "target": {
                "operating_system": self.sbom.target.operating_system,
                "architecture": self.sbom.target.architecture,
                "accelerator": self.sbom.target.accelerator,
            },
            "base_sbom": str(self.base_sbom_path),
            "realization_sbom": str(self.sbom_path),
            "toolchain_sources": list(self.sbom.base.sources),
            "composition_warnings": [
                item.to_dict() for item in self.sbom.base.warnings
            ],
            "package_manager": self.sbom.package_manager.identifier,
            "coding_agent": self.coding_agent,
            "ready": self.ready,
            "installed": list(self.installed),
            "managed_paths": [str(item) for item in self.managed_paths],
            "capabilities": [item.to_dict() for item in self.observations],
        }


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
CommandLocator = Callable[[str, str | None], str | None]
ArtifactDownloader = Callable[[str, Path], None]


def detect_current_install_target(
    *, environment: Mapping[str, str] | None = None
) -> HostInstallTarget:
    """Detect the current OS, architecture, distribution family, and GPU axis."""

    configured = os.environ if environment is None else environment
    host_paths = resolve_host_paths(environment=configured)
    release: dict[str, str] = {}
    if host_paths.os_release_file is not None:
        release_path = Path(host_paths.os_release_file)
        try:
            release = parse_os_release(release_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            release = {}
        except (OSError, UnicodeError) as exc:
            raise HostInstallError(
                "host-install.os-release-unreadable",
                f"could not read host OS identity from {release_path}: {exc}",
            ) from exc
    return detect_host_install_target(
        system=platform.system(),
        machine=platform.machine(),
        os_release=release,
        accelerator=configured.get(INSTALL_ACCELERATOR_ENVIRONMENT, "none"),
    )


def load_host_install_sbom(
    sbom_root: Path,
    target: HostInstallTarget,
    *,
    flavor_names: Sequence[str] = (),
) -> tuple[Path, HostInstallSbom]:
    """Load os-base, selected mix-ins, and one exact OS realization."""

    try:
        root = sbom_root.resolve(strict=True)
    except OSError as exc:
        raise HostInstallError(
            "host-install.sbom-root-unavailable",
            f"host-install Flavor directory is unavailable: {sbom_root}",
        ) from exc
    supported = _supported_sboms(root)
    path = root.joinpath(*target.relative_sbom_path.parts)
    if path not in supported:
        available = (
            "\n".join(
                f"  - {item.display()}"
                for item in supported_targets(
                    [candidate.relative_to(root) for candidate in supported]
                )
            )
            or "  (none)"
        )
        raise HostInstallError(
            "host-install.target-unsupported",
            f"no host-install realization exists for {target.display()}.\n"
            f"Supported tuples:\n{available}\n"
            f"SBOM directory: {root}\n"
            "Copy the nearest os-<name>/host-install tuple and update its "
            "package-manager and realization declarations to begin a new port.",
        )
    base_path = root.joinpath(*BASE_TOOLCHAIN_SBOM_RELATIVE_PATH.parts[1:])
    try:
        _require_regular_authored_file(root, base_path)
        base = parse_base_toolchain_sbom(_read_json(base_path))
        flavor_toolchains = _load_flavor_toolchains(root)
        unknown = sorted(
            name
            for name in set(flavor_names)
            if not (root / name / "flavor.md").is_file()
        )
        if unknown:
            raise HostInstallError(
                "host-install.flavor-unknown",
                "selected Flavor is unavailable: " + ", ".join(unknown),
            )
        automatic_name = _target_os_flavor(target)
        selected_names = tuple(
            dict.fromkeys(
                (
                    *flavor_names,
                    *((automatic_name,) if automatic_name in flavor_toolchains else ()),
                )
            )
        )
        selected = compose_toolchain_sboms(
            (
                base,
                *(
                    flavor_toolchains[name]
                    for name in selected_names
                    if name in flavor_toolchains
                ),
            )
        )
        catalog = compose_toolchain_sboms((base, *flavor_toolchains.values()))
        sbom = parse_host_install_sbom(
            _read_json(path),
            base=selected,
            catalog=catalog,
            expected_target=target,
        )
    except HostInstallError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HostInstallError(
            "host-install.sbom-unreadable",
            f"could not read host-install SBOM: {exc}",
        ) from exc
    except HostInstallRequirementError as exc:
        raise HostInstallError(exc.code, f"{path}: {exc.message}") from exc
    return path, sbom


def observe_host_install_dependencies(
    *,
    sbom_path: Path,
    sbom: HostInstallSbom,
    environment: Mapping[str, str] | None = None,
    runner: CommandRunner = subprocess.run,
    locator: CommandLocator | None = None,
    managed_tool_root: Path | None = None,
) -> HostInstallReport:
    """Observe native provenance and every selected Flavor capability."""

    configured = dict(os.environ if environment is None else environment)
    locate = locator or _locate_command
    tool_root = managed_tool_root or _managed_tool_root(sbom.target, configured)
    managed_paths = _existing_managed_paths(sbom, tool_root)
    observed_environment = _prepend_paths(configured, managed_paths)
    observed_environment = _prepend_native_paths(observed_environment, sbom.packages)
    coding_agent = _select_coding_agent(
        sbom,
        environment=observed_environment,
        runner=runner,
        locator=locate,
    )
    required = {item.capability for item in sbom.base.capabilities}
    manager = sbom.package_manager
    query_path = locate(manager.query_executable, observed_environment.get("PATH"))
    native = tuple(
        _observe_native_package(
            requirement,
            sbom=sbom,
            query_path=query_path,
            environment=observed_environment,
            runner=runner,
            locator=locate,
        )
        for requirement in sbom.packages
        if requirement.capability in required
        and sbom.base.capability(requirement.capability).group is None
    )
    native_capabilities = {item.requirement.capability for item in native}
    managed = tuple(
        _observe_capability(
            sbom.base.capability(capability),
            source_kind="managed-archive",
            environment=observed_environment,
            runner=runner,
            locator=locate,
        )
        for artifact in sbom.artifacts
        for capability in artifact.capabilities
        if capability in required
        and capability not in native_capabilities
        and sbom.base.capability(capability).group is None
    )
    agent_native = next(
        (item for item in sbom.packages if item.capability == coding_agent), None
    )
    agent = (
        _observe_native_package(
            agent_native,
            sbom=sbom,
            query_path=query_path,
            environment=observed_environment,
            runner=runner,
            locator=locate,
        )
        if agent_native is not None
        else _observe_capability(
            sbom.base.capability(coding_agent),
            source_kind="managed-archive",
            environment=observed_environment,
            runner=runner,
            locator=locate,
        )
    )
    base_path = sbom_path.parents[4] / "os-base" / "toolchain.cdx.json"
    return HostInstallReport(
        base_path,
        sbom_path,
        sbom,
        (*native, *managed, agent),
        coding_agent,
        managed_paths,
    )


def ensure_host_install_dependencies(
    *,
    sbom_root: Path,
    environment: Mapping[str, str] | None = None,
    target: HostInstallTarget | None = None,
    flavor_names: Sequence[str] = (),
    runner: CommandRunner = subprocess.run,
    locator: CommandLocator | None = None,
    downloader: ArtifactDownloader | None = None,
    input_function: Callable[[str], str] = input,
    interactive: bool | None = None,
    root_user: bool | None = None,
    managed_tool_root: Path | None = None,
) -> HostInstallReport:
    """Prove or explicitly install a composed Flavor toolchain contract."""

    configured = dict(os.environ if environment is None else environment)
    selected = target or detect_current_install_target(environment=configured)
    sbom_path, sbom = load_host_install_sbom(
        sbom_root, selected, flavor_names=flavor_names
    )
    tool_root = managed_tool_root or _managed_tool_root(selected, configured)
    report = observe_host_install_dependencies(
        sbom_path=sbom_path,
        sbom=sbom,
        environment=configured,
        runner=runner,
        locator=locator,
        managed_tool_root=tool_root,
    )
    if report.ready:
        return report
    consent = _installation_consent(
        configured,
        manager=sbom.package_manager.identifier,
        missing=report.missing,
        input_function=input_function,
        interactive=sys.stdin.isatty() if interactive is None else interactive,
    )
    if not consent:
        raise HostInstallError(
            "host-install.dependencies-declined",
            _requirements_message(report)
            + "\nNo host tools were changed. Rerun after installing them, or set "
            "LITAI_INSTALL_DEPENDENCIES=yes to authorize the declared native and "
            "per-user managed installations.",
        )

    locate = locator or _locate_command
    installed: list[str] = []
    missing_native_capabilities = {
        item.requirement.capability
        for item in report.missing
        if item.source_kind == "native-package"
    }
    native_requirements = tuple(
        {
            item.manager_package: item
            for item in sbom.packages
            if item.capability in missing_native_capabilities
        }.values()
    )
    if native_requirements:
        _require_package_manager(sbom, configured, locate)
        _install_packages(
            sbom,
            native_requirements,
            environment=configured,
            runner=runner,
            locator=locate,
            root_user=_is_root_user() if root_user is None else root_user,
        )
        installed.extend(
            f"native:{item.manager_package}" for item in native_requirements
        )

    missing_capabilities = {item.requirement.capability for item in report.missing}
    for artifact in sbom.artifacts:
        required = set(artifact.capabilities) & missing_capabilities
        if not required:
            continue
        if (
            any(sbom.base.capability(item).group == "coding-agent" for item in required)
            and report.coding_agent not in artifact.capabilities
        ):
            continue
        _install_managed_artifact(
            artifact,
            tool_root=tool_root,
            downloader=downloader or _download_artifact,
        )
        installed.append(f"artifact:{artifact.capabilities[0]}@{artifact.version}")

    verified = observe_host_install_dependencies(
        sbom_path=sbom_path,
        sbom=sbom,
        environment=configured,
        runner=runner,
        locator=locator,
        managed_tool_root=tool_root,
    )
    if not verified.ready:
        raise HostInstallError(
            "host-install.install-incomplete",
            _requirements_message(verified)
            + "\nInstallation commands completed, but the composed Flavor contract "
            "still cannot be proven. Restart the terminal if PATH changed, then "
            "rerun the operation.",
        )
    return HostInstallReport(
        verified.base_sbom_path,
        verified.sbom_path,
        verified.sbom,
        verified.observations,
        verified.coding_agent,
        verified.managed_paths,
        tuple(installed),
    )


def apply_host_install_path(report: HostInstallReport) -> None:
    """Expose verified native and per-user managed tools to child processes."""

    entries = [str(item) for item in report.managed_paths]
    entries.extend(
        str(item.executable.parent)
        for item in report.observations
        if item.executable is not None
    )
    os.environ["PATH"] = os.pathsep.join(
        (*dict.fromkeys(entries), os.environ.get("PATH", ""))
    )


def _load_flavor_toolchains(root: Path) -> dict[str, UniversalToolchainSbom]:
    result: dict[str, UniversalToolchainSbom] = {}
    for path in sorted(root.glob("*/host-toolchain.cdx.json")):
        _require_regular_authored_file(root, path)
        parsed = parse_flavor_toolchain_sbom(_read_json(path))
        name = path.parent.name
        if name in result:
            raise HostInstallError(
                "host-install.flavor-toolchain-duplicate",
                f"duplicate host toolchain authority for Flavor {name!r}",
            )
        result[name] = parsed
    return result


def _target_os_flavor(target: HostInstallTarget) -> str:
    if target.operating_system == "macos":
        return "os-macos"
    if target.operating_system == "windows":
        return "os-windows"
    if target.operating_system.startswith("linux-"):
        return "os-linux"
    return "os-" + target.operating_system


def _supported_sboms(root: Path) -> tuple[Path, ...]:
    result: list[Path] = []
    for path in root.glob("os-*/host-install/*/*/*.cdx.json"):
        if not path.is_file() or _has_symlink_between(root, path):
            continue
        try:
            path.resolve(strict=True).relative_to(root)
        except (OSError, ValueError):
            continue
        result.append(path)
    return tuple(sorted(result))


def _read_json(path: Path) -> object:
    return json.loads(
        path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object
    )


def _require_regular_authored_file(root: Path, path: Path) -> None:
    if not path.is_file() or _has_symlink_between(root, path):
        raise HostInstallError(
            "host-install.sbom-path-invalid",
            f"host-install SBOM must be a regular authored file below {root}: {path}",
        )
    try:
        path.resolve(strict=True).relative_to(root)
    except (OSError, ValueError) as exc:
        raise HostInstallError(
            "host-install.sbom-path-invalid",
            f"host-install SBOM escapes its Flavor root: {path}",
        ) from exc


def _has_symlink_between(root: Path, path: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _select_coding_agent(
    sbom: HostInstallSbom,
    *,
    environment: Mapping[str, str],
    runner: CommandRunner,
    locator: CommandLocator,
) -> str:
    requested = environment.get(CODING_CLI_ENVIRONMENT, "").strip()
    supported = tuple(item.capability for item in sbom.base.coding_agents)
    if requested:
        if requested not in supported:
            raise HostInstallError(
                "host-install.coding-agent-unsupported",
                f"{CODING_CLI_ENVIRONMENT} selects {requested!r}; supported coding "
                f"agents are: {', '.join(supported)}",
            )
        return requested
    for requirement in sbom.base.coding_agents:
        observation = _observe_capability(
            requirement,
            source_kind="coding-agent",
            environment=environment,
            runner=runner,
            locator=locator,
        )
        if observation.ready:
            return requirement.capability
    default = sbom.base.default_coding_agent
    if default is None:
        raise HostInstallError(
            "host-install.coding-agent-default-missing",
            "os-base does not declare a default coding agent",
        )
    return default


def _observe_native_package(
    requirement: HostPackageRequirement,
    *,
    sbom: HostInstallSbom,
    query_path: str | None,
    environment: Mapping[str, str],
    runner: CommandRunner,
    locator: CommandLocator,
) -> HostCapabilityObservation:
    package_present = False
    if query_path is not None:
        query_arguments = _expand_single(
            sbom.package_manager.query_arguments,
            token="{package}",
            replacement=requirement.manager_package,
        )
        query = runner(
            (query_path, *query_arguments),
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
            env=dict(environment),
        )
        package_present = (
            query.returncode == 0
            and requirement.manager_package.casefold()
            in (query.stdout + query.stderr).casefold()
        )
    base = sbom.base.capability(requirement.capability)
    probe = _probe_capability(
        base, environment=environment, runner=runner, locator=locator
    )
    return HostCapabilityObservation(
        base,
        "native-package",
        requirement.manager_package,
        package_present,
        probe[0],
        probe[1],
        probe[2],
    )


def _observe_capability(
    requirement: UniversalCapabilityRequirement,
    *,
    source_kind: str,
    environment: Mapping[str, str],
    runner: CommandRunner,
    locator: CommandLocator,
) -> HostCapabilityObservation:
    satisfied, version, executable = _probe_capability(
        requirement, environment=environment, runner=runner, locator=locator
    )
    return HostCapabilityObservation(
        requirement, source_kind, None, None, satisfied, version, executable
    )


def _probe_capability(
    requirement: UniversalCapabilityRequirement,
    *,
    environment: Mapping[str, str],
    runner: CommandRunner,
    locator: CommandLocator,
) -> tuple[bool, str | None, Path | None]:
    if requirement.probe_kind == "current-python":
        observed = platform.python_version()
        return (
            version_satisfies(observed, requirement.version_constraint),
            observed,
            Path(sys.executable).resolve(),
        )
    if requirement.probe_kind == "python-module":
        completed = runner(
            (sys.executable, *requirement.probe_arguments),
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
            env=dict(environment),
        )
        observed = platform.python_version()
        return (
            completed.returncode == 0
            and version_satisfies(observed, requirement.version_constraint),
            observed,
            Path(sys.executable).resolve(),
        )
    if requirement.probe_kind == "file-group":
        paths = tuple(
            Path(_expand_environment_candidate(candidate, environment))
            for candidate in requirement.probe_candidates
        )
        if not all(path.is_absolute() and path.is_file() for path in paths):
            return False, None, None
        return True, "complete", paths[0].resolve().parent
    executable = _locate_candidate(
        requirement.probe_candidates, environment=environment, locator=locator
    )
    if executable is None:
        return False, None, None
    completed = runner(
        (str(executable), *requirement.probe_arguments),
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
        env=dict(environment),
    )
    output = (completed.stdout + completed.stderr).strip()
    return (
        completed.returncode == 0
        and version_satisfies(output, requirement.version_constraint),
        output[:256] or None,
        executable,
    )


def _installation_consent(
    environment: Mapping[str, str],
    *,
    manager: str,
    missing: Sequence[HostCapabilityObservation],
    input_function: Callable[[str], str],
    interactive: bool,
) -> bool:
    configured = environment.get(INSTALL_DEPENDENCIES_ENVIRONMENT)
    if configured is not None:
        normalized = configured.strip().casefold()
        if normalized in _YES:
            return True
        if normalized in _NO:
            return False
        raise HostInstallError(
            "host-install.consent-invalid",
            f"{INSTALL_DEPENDENCIES_ENVIRONMENT} must be yes or no, not {configured!r}",
        )
    if not interactive:
        return False
    capabilities = ", ".join(item.requirement.capability for item in missing)
    answer = input_function(
        f"Install required tools ({capabilities}) using {manager} and per-user "
        "managed archives? [y/N] "
    )
    return answer.strip().casefold() in _YES


def _require_package_manager(
    sbom: HostInstallSbom,
    environment: Mapping[str, str],
    locator: CommandLocator,
) -> None:
    manager = sbom.package_manager
    if (
        locator(manager.executable, environment.get("PATH")) is None
        or locator(manager.query_executable, environment.get("PATH")) is None
    ):
        raise HostInstallError(
            "host-install.manager-missing",
            f"native package manager {manager.identifier!r} requires "
            f"{manager.executable!r} and {manager.query_executable!r} on PATH. "
            "Bootstrap that operating-system package manager, then rerun.",
        )


def _install_packages(
    sbom: HostInstallSbom,
    requirements: tuple[HostPackageRequirement, ...],
    *,
    environment: Mapping[str, str],
    runner: CommandRunner,
    locator: CommandLocator,
    root_user: bool,
) -> None:
    manager = sbom.package_manager
    prefix: tuple[str, ...] = ()
    if manager.elevation_executable is not None and not root_user:
        elevation = locator(manager.elevation_executable, environment.get("PATH"))
        if elevation is None:
            raise HostInstallError(
                "host-install.elevation-missing",
                f"package manager {manager.identifier!r} requires "
                f"{manager.elevation_executable!r} for installation",
            )
        prefix = (elevation,)
    if manager.preinstall_executable is not None:
        executable = locator(manager.preinstall_executable, environment.get("PATH"))
        if executable is None:
            raise HostInstallError(
                "host-install.preinstall-command-missing",
                f"preinstall command {manager.preinstall_executable!r} is absent",
            )
        _run_install_command(
            (*prefix, executable, *manager.preinstall_arguments),
            manager=manager.identifier,
            environment=environment,
            runner=runner,
        )
    install = locator(manager.install_executable, environment.get("PATH"))
    if install is None:
        raise HostInstallError(
            "host-install.install-command-missing",
            f"install command {manager.install_executable!r} is absent",
        )
    overrides = tuple(item for item in requirements if item.install_arguments)
    packages = tuple(
        item.manager_package for item in requirements if not item.install_arguments
    )
    argument_sets = (
        (
            _expand_many(
                manager.install_arguments,
                token="{packages}",
                replacements=packages,
            ),
        )
        if manager.install_mode == "batch" and packages
        else tuple(
            _expand_single(
                manager.install_arguments, token="{package}", replacement=package
            )
            for package in packages
        )
    )
    override_sets = tuple(
        _expand_single(
            item.install_arguments,
            token="{package}",
            replacement=item.manager_package,
        )
        for item in overrides
    )
    for arguments in (*argument_sets, *override_sets):
        _run_install_command(
            (*prefix, install, *arguments),
            manager=manager.identifier,
            environment=environment,
            runner=runner,
        )


def _install_managed_artifact(
    artifact: HostManagedArtifact,
    *,
    tool_root: Path,
    downloader: ArtifactDownloader,
) -> Path:
    destination = _artifact_destination(tool_root, artifact)
    if _artifact_manifest_matches(destination, artifact):
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".literate-ai-download-", dir=destination.parent
    ) as download_directory:
        archive = Path(download_directory) / "artifact"
        downloader(artifact.url, archive)
        observed = _file_digest(archive, artifact.digest_algorithm)
        if observed != artifact.digest:
            raise HostInstallError(
                "host-install.artifact-digest-mismatch",
                f"managed artifact {artifact.name!r} {artifact.digest_algorithm} "
                f"was {observed}, expected {artifact.digest}",
            )
        staging = Path(
            tempfile.mkdtemp(prefix=".literate-ai-extract-", dir=destination.parent)
        )
        try:
            _extract_archive(archive, artifact, staging)
            _normalize_artifact_executable(staging, artifact)
            manifest = {
                "schema": "literate-ai/managed-tool-artifact@1",
                "url": artifact.url,
                "digest_algorithm": artifact.digest_algorithm,
                "digest": artifact.digest,
                "version": artifact.version,
                "capabilities": list(artifact.capabilities),
                "required_runtime_paths": list(artifact.required_runtime_paths),
            }
            (staging / _ARTIFACT_MANIFEST).write_text(
                json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            try:
                staging.replace(destination)
            except FileExistsError:
                if not _artifact_manifest_matches(destination, artifact):
                    raise HostInstallError(
                        "host-install.artifact-destination-conflict",
                        f"managed artifact destination already exists: {destination}",
                    ) from None
            return destination
        finally:
            if staging.exists():
                shutil.rmtree(staging)


def _normalize_artifact_executable(
    destination: Path, artifact: HostManagedArtifact
) -> None:
    source = destination.joinpath(*PurePosixPath(artifact.executable_path).parts)
    target = _artifact_executable_target(destination, artifact)
    if not source.is_file():
        raise HostInstallError(
            "host-install.artifact-executable-missing",
            f"managed artifact lacks declared executable {artifact.executable_path!r}",
        )
    if source != target:
        if target.exists():
            raise HostInstallError(
                "host-install.artifact-executable-conflict",
                "managed artifact executable name conflicts: "
                f"{artifact.executable_name}",
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        source.replace(target)
    if os.name != "nt":
        target.chmod(target.stat().st_mode | 0o700)


def _download_artifact(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "literate-ai/0.9"})
    try:
        with urllib.request.urlopen(request, timeout=300) as response:  # noqa: S310
            with destination.open("wb") as output:
                shutil.copyfileobj(response, output, length=1024 * 1024)
    except (OSError, urllib.error.URLError) as exc:
        raise HostInstallError(
            "host-install.artifact-download-failed",
            f"could not download managed artifact {url}: {exc}",
        ) from exc


def _extract_archive(
    archive: Path, artifact: HostManagedArtifact, destination: Path
) -> None:
    if artifact.archive_format == "file":
        target = destination.joinpath(*PurePosixPath(artifact.executable_path).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(archive, target)
    elif artifact.archive_format == "zip":
        _extract_zip(archive, artifact.strip_prefix, destination)
    else:
        mode = "r:gz" if artifact.archive_format == "tar.gz" else "r:xz"
        _extract_tar(archive, artifact.strip_prefix, destination, mode=mode)


def _extract_zip(archive: Path, strip_prefix: str, destination: Path) -> None:
    count = 0
    total = 0
    try:
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                count += 1
                total += member.file_size
                _check_archive_limits(count, total)
                relative = _stripped_archive_path(member.filename, strip_prefix)
                if relative is None:
                    continue
                file_type = (member.external_attr >> 16) & 0o170000
                if file_type == 0o120000:
                    raise HostInstallError(
                        "host-install.archive-member-invalid",
                        "managed zip archives may not contain symbolic links",
                    )
                target = destination.joinpath(*relative.parts)
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                mode = (member.external_attr >> 16) & 0o777
                if mode:
                    target.chmod(mode)
    except HostInstallError:
        raise
    except (OSError, zipfile.BadZipFile) as exc:
        raise HostInstallError(
            "host-install.archive-invalid", f"managed zip archive is invalid: {exc}"
        ) from exc


def _extract_tar(
    archive: Path, strip_prefix: str, destination: Path, *, mode: str
) -> None:
    count = 0
    total = 0
    deferred: list[tuple[Path, Path]] = []
    try:
        with tarfile.open(archive, mode) as bundle:
            for member in bundle:
                count += 1
                total += member.size
                _check_archive_limits(count, total)
                relative = _stripped_archive_path(member.name, strip_prefix)
                if relative is None:
                    continue
                target = destination.joinpath(*relative.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if member.issym() or member.islnk():
                    link = _tar_link_target(member, strip_prefix)
                    deferred.append((target, destination.joinpath(*link.parts)))
                    continue
                if not member.isfile():
                    raise HostInstallError(
                        "host-install.archive-member-invalid",
                        "managed tar archive contains unsupported member "
                        f"{member.name!r}",
                    )
                source = bundle.extractfile(member)
                if source is None:
                    raise HostInstallError(
                        "host-install.archive-member-invalid",
                        f"managed tar member cannot be read: {member.name!r}",
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                with source, target.open("wb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                target.chmod(member.mode & 0o777)
        _materialize_archive_links(deferred, destination)
    except HostInstallError:
        raise
    except (OSError, tarfile.TarError) as exc:
        raise HostInstallError(
            "host-install.archive-invalid", f"managed tar archive is invalid: {exc}"
        ) from exc


def _tar_link_target(member: tarfile.TarInfo, strip_prefix: str) -> PurePosixPath:
    raw = (
        str(PurePosixPath(member.name).parent / member.linkname)
        if member.issym()
        else member.linkname
    )
    relative = _stripped_archive_path(raw, strip_prefix)
    if relative is None:
        raise HostInstallError(
            "host-install.archive-link-invalid",
            f"archive link {member.name!r} escapes the declared strip prefix",
        )
    return relative


def _materialize_archive_links(
    pending: Sequence[tuple[Path, Path]], destination: Path
) -> None:
    unresolved = list(pending)
    while unresolved:
        remaining: list[tuple[Path, Path]] = []
        progressed = False
        for target, source in unresolved:
            try:
                source.relative_to(destination)
            except ValueError as exc:
                raise HostInstallError(
                    "host-install.archive-link-invalid",
                    "archive link escapes the extraction root",
                ) from exc
            if source.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                progressed = True
            else:
                remaining.append((target, source))
        if not progressed:
            raise HostInstallError(
                "host-install.archive-link-invalid",
                "archive contains an unresolved or cyclic link",
            )
        unresolved = remaining


def _stripped_archive_path(name: str, strip_prefix: str) -> PurePosixPath | None:
    path = PurePosixPath(name.replace("\\", "/"))
    prefix = PurePosixPath(strip_prefix)
    if path.is_absolute() or any(part == ".." for part in path.parts):
        raise HostInstallError(
            "host-install.archive-traversal",
            f"managed archive member escapes extraction root: {name!r}",
        )
    try:
        relative = path if strip_prefix == "." else path.relative_to(prefix)
    except ValueError as exc:
        raise HostInstallError(
            "host-install.archive-prefix-mismatch",
            "managed archive member is outside strip-prefix "
            f"{strip_prefix!r}: {name!r}",
        ) from exc
    if str(relative) in {"", "."}:
        return None
    return relative


def _check_archive_limits(count: int, total: int) -> None:
    if count > _MAX_ARCHIVE_MEMBERS or total > _MAX_ARCHIVE_BYTES:
        raise HostInstallError(
            "host-install.archive-limit-exceeded",
            "managed archive exceeds the member-count or expanded-size limit",
        )


def _artifact_destination(tool_root: Path, artifact: HostManagedArtifact) -> Path:
    return (
        tool_root
        / artifact.capabilities[0]
        / f"{artifact.version}-{artifact.digest[:12]}"
    )


def _artifact_manifest_matches(
    destination: Path, artifact: HostManagedArtifact
) -> bool:
    try:
        manifest = json.loads(
            (destination / _ARTIFACT_MANIFEST).read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return (
        isinstance(manifest, dict)
        and manifest.get("url") == artifact.url
        and manifest.get("digest_algorithm") == artifact.digest_algorithm
        and manifest.get("digest") == artifact.digest
        and manifest.get("version") == artifact.version
        and manifest.get("capabilities") == list(artifact.capabilities)
        and manifest.get("required_runtime_paths", [])
        == list(artifact.required_runtime_paths)
        and _artifact_executable_target(destination, artifact).is_file()
        and all(
            destination.joinpath(*PurePosixPath(item).parts).is_file()
            for item in artifact.required_runtime_paths
        )
    )


def _artifact_executable_target(
    destination: Path, artifact: HostManagedArtifact
) -> Path:
    path_entry = PurePosixPath(artifact.path_entries[0])
    return destination.joinpath(*path_entry.parts) / artifact.executable_name


def _existing_managed_paths(sbom: HostInstallSbom, tool_root: Path) -> tuple[Path, ...]:
    entries: list[Path] = []
    for artifact in sbom.artifacts:
        destination = _artifact_destination(tool_root, artifact)
        if not _artifact_manifest_matches(destination, artifact):
            continue
        entries.extend(
            destination.joinpath(*PurePosixPath(item).parts)
            for item in artifact.path_entries
        )
    return tuple(dict.fromkeys(entries))


def _managed_tool_root(
    target: HostInstallTarget, environment: Mapping[str, str]
) -> Path:
    family = (
        "windows"
        if target.operating_system == "windows"
        else "macos"
        if target.operating_system == "macos"
        else "linux"
    )
    return Path(
        resolve_host_paths(
            platform_family=family, environment=environment
        ).managed_tool_root
    )


def _run_install_command(
    arguments: tuple[str, ...],
    *,
    manager: str,
    environment: Mapping[str, str],
    runner: CommandRunner,
) -> None:
    completed = runner(arguments, check=False, timeout=1800, env=dict(environment))
    if completed.returncode != 0:
        raise HostInstallError(
            "host-install.package-manager-failed",
            f"native package manager {manager!r} failed with status "
            f"{completed.returncode}",
        )


def _requirements_message(report: HostInstallReport) -> str:
    lines = [
        f"Host requirements for {report.sbom.target.display()} from composed Flavors "
        f"{', '.join(report.sbom.base.sources)} ({report.sbom_path}):"
    ]
    for observation in report.missing:
        requirement = observation.requirement
        realization = (
            f" via {observation.manager_package}"
            if observation.manager_package
            else f" via {requirement.installer_kind or observation.source_kind}"
        )
        lines.append(
            f"  - {requirement.capability} {requirement.version_constraint}"
            f"{realization}: {requirement.reason}"
        )
    return "\n".join(lines)


def _expand_single(
    arguments: Sequence[str], *, token: str, replacement: str
) -> tuple[str, ...]:
    result: list[str] = []
    count = 0
    for argument in arguments:
        if argument == token:
            result.append(replacement)
            count += 1
        else:
            result.append(argument)
    if count != 1:
        raise HostInstallError(
            "host-install.template-invalid",
            f"command template must contain exactly one {token} token",
        )
    return tuple(result)


def _expand_many(
    arguments: Sequence[str], *, token: str, replacements: Sequence[str]
) -> tuple[str, ...]:
    result: list[str] = []
    count = 0
    for argument in arguments:
        if argument == token:
            result.extend(replacements)
            count += 1
        else:
            result.append(argument)
    if count != 1:
        raise HostInstallError(
            "host-install.template-invalid",
            f"command template must contain exactly one {token} token",
        )
    return tuple(result)


def _locate_candidate(
    candidates: Sequence[str],
    *,
    environment: Mapping[str, str],
    locator: CommandLocator,
) -> Path | None:
    for raw_candidate in candidates:
        candidate = _expand_environment_candidate(raw_candidate, environment)
        located = locator(candidate, environment.get("PATH"))
        if located is not None:
            return Path(located).resolve()
        path = Path(candidate)
        if path.is_absolute() and path.is_file():
            return path.resolve()
    return None


def _expand_environment_candidate(
    candidate: str, environment: Mapping[str, str]
) -> str:
    casefolded = {key.casefold(): value for key, value in environment.items()}

    def replace(match: re.Match[str]) -> str:
        return casefolded.get(match.group(1).casefold(), match.group(0))

    return _ENVIRONMENT_REFERENCE.sub(replace, candidate)


def _prepend_paths(
    environment: Mapping[str, str], entries: Sequence[Path]
) -> dict[str, str]:
    updated = dict(environment)
    try:
        values = tuple(dict.fromkeys(str(item) for item in entries if item.is_dir()))
    except OSError as exc:
        raise HostInstallError(
            "host-install.path-unreadable",
            "could not inspect a declared native tool search directory; "
            "run installation from an account with access to the host tool paths: "
            f"{exc}",
        ) from exc
    if values:
        updated["PATH"] = os.pathsep.join((*values, updated.get("PATH", "")))
    return updated


def _prepend_native_paths(
    environment: Mapping[str, str],
    requirements: Sequence[HostPackageRequirement],
) -> dict[str, str]:
    """Expose paths installed by native packages to the current process snapshot."""

    entries = tuple(
        Path(_expand_environment_candidate(item, environment))
        for requirement in requirements
        for item in requirement.path_entries
    )
    return _prepend_paths(environment, entries)


def _file_digest(path: Path, algorithm: str) -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _locate_command(command: str, path: str | None) -> str | None:
    return shutil.which(command, path=path)


def _is_root_user() -> bool:
    geteuid = getattr(os, "geteuid", None)
    return bool(geteuid is not None and geteuid() == 0)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise HostInstallError(
                "host-install.sbom-duplicate-key",
                f"host-install SBOM repeats JSON key {key!r}",
            )
        result[key] = value
    return result


__all__ = [
    "CODING_CLI_ENVIRONMENT",
    "HostCapabilityObservation",
    "HostInstallError",
    "HostInstallReport",
    "HostPackageObservation",
    "apply_host_install_path",
    "detect_current_install_target",
    "ensure_host_install_dependencies",
    "load_host_install_sbom",
    "observe_host_install_dependencies",
]
