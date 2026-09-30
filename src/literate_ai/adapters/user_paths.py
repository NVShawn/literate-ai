"""Central host paths for durable operator configuration and mutable state."""

from __future__ import annotations

import hashlib
import os
import re
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath

APPLICATION_DIRECTORY = "literate-ai"
HOST_INSTALL_MANIFEST_SCHEMA_V1 = "literate-ai/host-install-manifest@1"
HOST_INSTALL_MANIFEST_SCHEMA = "literate-ai/host-install-manifest@2"
CONFIG_DIRECTORY_ENVIRONMENT = "LITAI_CONFIG_DIR"
STATE_DIRECTORY_ENVIRONMENT = "LITAI_STATE_DIR"
DATA_DIRECTORY_ENVIRONMENT = "LITAI_DATA_DIR"
CACHE_DIRECTORY_ENVIRONMENT = "LITAI_CACHE_DIR"
TOOL_DIRECTORY_ENVIRONMENT = "LITAI_TOOL_DIR"
INSTALL_DIRECTORY_ENVIRONMENT = "LITAI_INSTALL_DIR"
TEMP_DIRECTORY_ENVIRONMENT = "LITAI_TEMP_DIR"
STAGING_DIRECTORY_ENVIRONMENT = "LITAI_STAGING_DIR"

_SAFE_PROJECT_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)


class UserPathError(ValueError):
    """A user configuration/state path cannot be resolved safely."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class UserPaths:
    """Resolved per-user configuration and state custody."""

    config_root: PurePath
    state_root: PurePath

    @property
    def worker_config(self) -> PurePath:
        return self.config_root / "workers.json"

    @property
    def mcp_catalog(self) -> PurePath:
        return self.config_root / "mcps.json"

    @property
    def worker_observations(self) -> PurePath:
        return self.state_root / "worker-observations.json"

    @property
    def channel_events(self) -> PurePath:
        return self.state_root / "events"

    def project_test_config(self, project_id: str) -> PurePath:
        return (
            self.config_root / "projects" / _project_directory(project_id) / "test.json"
        )


@dataclass(frozen=True, slots=True)
class HostSystemPaths:
    """Platform-owned system files and conventional runtime roots."""

    os_release_file: PurePath | None
    apparmor_user_namespace_restriction_file: PurePath | None
    apparmor_bwrap_profile_file: PurePath | None
    linux_ldconfig_candidates: tuple[PurePath, ...]
    sandbox_runtime_roots: tuple[PurePath, ...]
    bubblewrap_proc_root: PurePath | None
    bubblewrap_device_root: PurePath | None
    bubblewrap_temporary_root: PurePath | None


@dataclass(frozen=True, slots=True)
class HostPaths(UserPaths):
    """All per-user host-native roots owned by the platform policy surface."""

    data_root: PurePath
    cache_root: PurePath
    managed_tool_root: PurePath
    install_root: PurePath
    temporary_root: PurePath
    short_staging_root: PurePath
    os_release_file: PurePath | None
    apparmor_user_namespace_restriction_file: PurePath | None
    apparmor_bwrap_profile_file: PurePath | None
    linux_ldconfig_candidates: tuple[PurePath, ...]
    sandbox_runtime_roots: tuple[PurePath, ...]
    bubblewrap_proc_root: PurePath | None
    bubblewrap_device_root: PurePath | None
    bubblewrap_temporary_root: PurePath | None

    @property
    def install_layout(self) -> HostInstallLayout:
        return HostInstallLayout.for_prefix(
            self.install_root,
            platform_family=(
                "windows"
                if isinstance(self.install_root, PureWindowsPath)
                else _host_platform_family()
            ),
        )


@dataclass(frozen=True, slots=True)
class HostInstallLayout:
    """The smallest platform-specific surface for Literate AI host installation."""

    prefix: PurePath
    application_root: PurePath
    environment: PurePath
    launcher: PurePath
    manifest: PurePath

    @classmethod
    def for_prefix(
        cls, prefix: PurePath, *, platform_family: str | None = None
    ) -> HostInstallLayout:
        family = platform_family or _host_platform_family()
        if family not in {"linux", "macos", "windows"}:
            raise UserPathError(
                "user_paths.platform_unsupported",
                f"unsupported install-layout platform family: {family!r}",
            )
        if not prefix.is_absolute():
            raise UserPathError(
                "user_paths.install_prefix_relative",
                "Literate AI install prefix must be absolute",
            )
        application_root = prefix / "share" / APPLICATION_DIRECTORY
        environment = application_root / "venv"
        launcher = prefix / "bin" / ("litai.cmd" if family == "windows" else "litai")
        return cls(
            prefix,
            application_root,
            environment,
            launcher,
            application_root / "install.json",
        )


WindowsKnownFolderResolver = Callable[[str], PurePath]


def resolve_host_system_paths(*, platform_family: str | None = None) -> HostSystemPaths:
    """Resolve system locations without consulting user configuration or home."""

    family = platform_family or _host_platform_family()
    if family == "windows":
        return HostSystemPaths(None, None, None, (), (), None, None, None)
    if family == "macos":
        return HostSystemPaths(
            None,
            None,
            None,
            (),
            (
                PurePosixPath("/System"),
                PurePosixPath("/usr"),
                PurePosixPath("/bin"),
                PurePosixPath("/dev"),
            ),
            None,
            None,
            None,
        )
    if family == "linux":
        return HostSystemPaths(
            PurePosixPath("/etc/os-release"),
            PurePosixPath("/proc/sys/kernel/apparmor_restrict_unprivileged_userns"),
            PurePosixPath("/etc/apparmor.d/bwrap-userns-restrict"),
            (PurePosixPath("/sbin/ldconfig"), PurePosixPath("/usr/sbin/ldconfig")),
            (
                PurePosixPath("/usr"),
                PurePosixPath("/bin"),
                PurePosixPath("/lib"),
                PurePosixPath("/lib64"),
            ),
            PurePosixPath("/proc"),
            PurePosixPath("/dev"),
            PurePosixPath("/tmp"),
        )
    raise UserPathError(
        "user_paths.platform_unsupported",
        f"unsupported user-path platform family: {family!r}",
    )


def resolve_user_paths(
    *,
    platform_family: str | None = None,
    environment: Mapping[str, str] | None = None,
    home: PurePath | None = None,
    windows_known_folder: WindowsKnownFolderResolver | None = None,
) -> UserPaths:
    """Resolve user paths without consulting the current working directory."""

    paths = resolve_host_paths(
        platform_family=platform_family,
        environment=environment,
        home=home,
        windows_known_folder=windows_known_folder,
    )
    return UserPaths(paths.config_root, paths.state_root)


def resolve_user_home(
    *,
    platform_family: str | None = None,
    environment: Mapping[str, str] | None = None,
    home: PurePath | None = None,
    windows_known_folder: WindowsKnownFolderResolver | None = None,
) -> PurePath:
    """Resolve the platform-native user profile without consulting the CWD."""

    configured = dict(os.environ if environment is None else environment)
    family = platform_family or _host_platform_family()
    if family not in {"linux", "macos", "windows"}:
        raise UserPathError(
            "user_paths.platform_unsupported",
            f"unsupported user-path platform family: {family!r}",
        )
    if home is not None:
        return _absolute_home(home)
    if family == "windows":
        explicit_profile = _absolute_environment_path(
            configured, "USERPROFILE", path_type=PureWindowsPath
        )
        if explicit_profile is not None:
            return explicit_profile
        known_folder = windows_known_folder or _windows_known_folder
        return _absolute_known_folder(known_folder("profile"), "Profile")
    explicit_home = _absolute_environment_path(
        configured, "HOME", path_type=PurePosixPath
    )
    return _absolute_home(explicit_home)


def resolve_host_paths(
    *,
    platform_family: str | None = None,
    environment: Mapping[str, str] | None = None,
    home: PurePath | None = None,
    windows_known_folder: WindowsKnownFolderResolver | None = None,
    temporary_root: PurePath | None = None,
) -> HostPaths:
    """Resolve the complete host policy without consulting the working directory."""

    configured = dict(os.environ if environment is None else environment)
    family = platform_family or _host_platform_family()
    if family not in {"linux", "macos", "windows"}:
        raise UserPathError(
            "user_paths.platform_unsupported",
            f"unsupported user-path platform family: {family!r}",
        )
    system_paths = resolve_host_system_paths(platform_family=family)
    path_type = PureWindowsPath if family == "windows" else PurePosixPath
    explicit_config = _absolute_environment_path(
        configured, CONFIG_DIRECTORY_ENVIRONMENT, path_type=path_type
    )
    explicit_state = _absolute_environment_path(
        configured, STATE_DIRECTORY_ENVIRONMENT, path_type=path_type
    )
    explicit_data = _absolute_environment_path(
        configured, DATA_DIRECTORY_ENVIRONMENT, path_type=path_type
    )
    explicit_cache = _absolute_environment_path(
        configured, CACHE_DIRECTORY_ENVIRONMENT, path_type=path_type
    )
    explicit_tools = _absolute_environment_path(
        configured, TOOL_DIRECTORY_ENVIRONMENT, path_type=path_type
    )
    explicit_install = _absolute_environment_path(
        configured, INSTALL_DIRECTORY_ENVIRONMENT, path_type=path_type
    )
    explicit_temp = _absolute_environment_path(
        configured, TEMP_DIRECTORY_ENVIRONMENT, path_type=path_type
    )
    explicit_staging = _absolute_environment_path(
        configured, STAGING_DIRECTORY_ENVIRONMENT, path_type=path_type
    )
    if family == "windows":
        known_folder = windows_known_folder or _windows_known_folder
        local = _absolute_known_folder(known_folder("local"), "Local AppData")
        config_base = (
            explicit_config
            if explicit_config is not None
            else _absolute_known_folder(known_folder("roaming"), "Roaming AppData")
            / APPLICATION_DIRECTORY
        )
        state_base = (
            explicit_state
            if explicit_state is not None
            else local / APPLICATION_DIRECTORY / "state"
        )
        data_base = explicit_data or local / APPLICATION_DIRECTORY / "data"
        cache_base = explicit_cache or local / APPLICATION_DIRECTORY / "cache"
        tool_base = explicit_tools or local / APPLICATION_DIRECTORY / "tools"
        install_base = explicit_install or local / APPLICATION_DIRECTORY
        temp_base = explicit_temp or temporary_root
        if temp_base is None:
            temp_base = _absolute_environment_path(
                configured, "TEMP", path_type=path_type
            )
        if temp_base is None:
            temp_base = local / "Temp"
        staging_base = explicit_staging or (
            PureWindowsPath(PureWindowsPath(local).anchor) / "litai-stage"
        )
        return HostPaths(
            config_base,
            state_base,
            data_base,
            cache_base,
            tool_base,
            install_base,
            temp_base,
            staging_base,
            system_paths.os_release_file,
            system_paths.apparmor_user_namespace_restriction_file,
            system_paths.apparmor_bwrap_profile_file,
            system_paths.linux_ldconfig_candidates,
            system_paths.sandbox_runtime_roots,
            system_paths.bubblewrap_proc_root,
            system_paths.bubblewrap_device_root,
            system_paths.bubblewrap_temporary_root,
        )

    resolved_home = resolve_user_home(
        platform_family=family,
        environment=configured,
        home=home,
    )
    xdg_config = _absolute_environment_path(
        configured, "XDG_CONFIG_HOME", path_type=path_type
    )
    xdg_state = _absolute_environment_path(
        configured, "XDG_STATE_HOME", path_type=path_type
    )
    xdg_data = _absolute_environment_path(
        configured, "XDG_DATA_HOME", path_type=path_type
    )
    xdg_cache = _absolute_environment_path(
        configured, "XDG_CACHE_HOME", path_type=path_type
    )
    config_root = (
        explicit_config
        if explicit_config is not None
        else (xdg_config or resolved_home / ".config") / APPLICATION_DIRECTORY
    )
    state_root = (
        explicit_state
        if explicit_state is not None
        else (xdg_state or resolved_home / ".local" / "state") / APPLICATION_DIRECTORY
    )
    data_root = (
        explicit_data
        if explicit_data is not None
        else (xdg_data or resolved_home / ".local" / "share") / APPLICATION_DIRECTORY
    )
    cache_root = (
        explicit_cache
        if explicit_cache is not None
        else (xdg_cache or resolved_home / ".cache") / APPLICATION_DIRECTORY
    )
    managed_tools = explicit_tools or data_root / "tools"
    install_root = explicit_install or resolved_home / ".local"
    resolved_temporary = explicit_temp or temporary_root
    if resolved_temporary is None:
        resolved_temporary = Path(tempfile.gettempdir()) / APPLICATION_DIRECTORY
    if not resolved_temporary.is_absolute():
        raise UserPathError(
            "user_paths.temporary_relative", "temporary root must be absolute"
        )
    staging_root = explicit_staging or resolved_temporary / "stage"
    return HostPaths(
        config_root,
        state_root,
        data_root,
        cache_root,
        managed_tools,
        install_root,
        resolved_temporary,
        staging_root,
        system_paths.os_release_file,
        system_paths.apparmor_user_namespace_restriction_file,
        system_paths.apparmor_bwrap_profile_file,
        system_paths.linux_ldconfig_candidates,
        system_paths.sandbox_runtime_roots,
        system_paths.bubblewrap_proc_root,
        system_paths.bubblewrap_device_root,
        system_paths.bubblewrap_temporary_root,
    )


def prepare_user_directory(root: Path, destination: Path) -> None:
    """Create one real private directory chain within a resolved custody root."""

    configured_root = Path(root)
    configured_destination = Path(destination)
    if not configured_root.is_absolute() or not configured_destination.is_absolute():
        raise UserPathError(
            "user_paths.custody_relative", "user custody paths must be absolute"
        )
    try:
        relative = configured_destination.relative_to(configured_root)
    except ValueError as exc:
        raise UserPathError(
            "user_paths.custody_escape",
            "user directory destination is outside its custody root",
        ) from exc
    candidates = [configured_root]
    current = configured_root
    for part in relative.parts:
        current /= part
        candidates.append(current)
    for candidate in candidates:
        try:
            if not candidate.exists() and not candidate.is_symlink():
                try:
                    candidate.mkdir(
                        mode=0o700,
                        parents=candidate == configured_root,
                    )
                except FileExistsError:
                    # Another process may prepare the same private root concurrently.
                    pass
            if candidate.is_symlink() or not candidate.is_dir():
                raise UserPathError(
                    "user_paths.custody_unsafe",
                    "user custody path must contain only real directories",
                )
            if os.name != "nt":
                os.chmod(candidate, 0o700)
        except UserPathError:
            raise
        except OSError as exc:
            raise UserPathError(
                "user_paths.custody_unavailable",
                "user custody directory cannot be prepared",
            ) from exc


def _absolute_environment_path(
    environment: Mapping[str, str],
    name: str,
    *,
    path_type: Callable[[str], PurePath] = Path,
) -> PurePath | None:
    raw = str(environment.get(name, "")).strip()
    if not raw:
        return None
    path = path_type(raw)
    if not path.is_absolute():
        raise UserPathError(
            "user_paths.override_relative",
            f"{name} must be an absolute path",
        )
    return path


def _absolute_home(configured: PurePath | None) -> PurePath:
    if configured is None:
        try:
            configured = Path.home()
        except (OSError, RuntimeError) as exc:
            raise UserPathError(
                "user_paths.home_unavailable",
                "the user home directory is unavailable",
            ) from exc
    if not configured.is_absolute():
        raise UserPathError(
            "user_paths.home_relative", "the user home directory must be absolute"
        )
    return configured


def _absolute_known_folder(path: PurePath, label: str) -> PurePath:
    if not path.is_absolute():
        raise UserPathError(
            "user_paths.known_folder_invalid",
            f"the Windows {label} Known Folder is not absolute",
        )
    return path


def _project_directory(project_id: str) -> str:
    if (
        not isinstance(project_id, str)
        or not project_id
        or project_id != project_id.strip()
    ):
        raise UserPathError(
            "user_paths.project_id_invalid", "project ID is invalid for user config"
        )
    folded = project_id.casefold()
    if _SAFE_PROJECT_ID.fullmatch(project_id) and folded not in _WINDOWS_RESERVED:
        return project_id
    return "sha256-" + hashlib.sha256(project_id.encode("utf-8")).hexdigest()


def _host_platform_family() -> str:
    if os.name == "nt":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    raise UserPathError(
        "user_paths.platform_unsupported",
        f"unsupported host platform: {sys.platform!r}",
    )


def _windows_known_folder(kind: str) -> Path:
    if os.name != "nt":
        raise UserPathError(
            "user_paths.known_folder_unavailable",
            "Windows Known Folders are unavailable on this host",
        )
    import ctypes
    import uuid
    from ctypes import wintypes

    identifiers = {
        "roaming": "3EB685DB-65F9-4CF6-A03A-E3EF65729F3D",
        "local": "F1B32785-6FBA-4FCF-9D55-7B8E7F157091",
        "profile": "5E6C858F-0E22-4760-9AFE-EA3317B67173",
    }
    try:
        raw_identifier = identifiers[kind]
    except KeyError as exc:
        raise UserPathError(
            "user_paths.known_folder_invalid", "unknown Windows Known Folder"
        ) from exc

    class _Guid(ctypes.Structure):
        _fields_ = (
            ("data1", wintypes.DWORD),
            ("data2", wintypes.WORD),
            ("data3", wintypes.WORD),
            ("data4", ctypes.c_ubyte * 8),
        )

    value = uuid.UUID(raw_identifier)
    guid = _Guid(
        value.time_low,
        value.time_mid,
        value.time_hi_version,
        (ctypes.c_ubyte * 8).from_buffer_copy(value.bytes[8:]),
    )
    output = ctypes.c_wchar_p()
    shell32 = ctypes.windll.shell32
    ole32 = ctypes.windll.ole32
    result = shell32.SHGetKnownFolderPath(
        ctypes.byref(guid), 0, wintypes.HANDLE(), ctypes.byref(output)
    )
    if result != 0 or not output.value:
        raise UserPathError(
            "user_paths.known_folder_unavailable",
            f"Windows Known Folder resolution failed with HRESULT {result}",
        )
    try:
        return Path(output.value)
    finally:
        ole32.CoTaskMemFree(output)


__all__ = [
    "APPLICATION_DIRECTORY",
    "CONFIG_DIRECTORY_ENVIRONMENT",
    "HOST_INSTALL_MANIFEST_SCHEMA",
    "HOST_INSTALL_MANIFEST_SCHEMA_V1",
    "HostPaths",
    "HostInstallLayout",
    "HostSystemPaths",
    "STATE_DIRECTORY_ENVIRONMENT",
    "UserPathError",
    "UserPaths",
    "prepare_user_directory",
    "resolve_host_paths",
    "resolve_host_system_paths",
    "resolve_user_home",
    "resolve_user_paths",
]
