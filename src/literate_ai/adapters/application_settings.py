"""Reusable provider-neutral settings authority for generated applications.

Generated (derived) applications routinely need their own per-user runtime
configuration: named deployment/host profiles, endpoint settings, and
credential references. `user_paths.py` and `user_assets.py` already give
Literate AI's *own* configuration a centralized, tested contract (native
per-OS roots, `LITAI_*` environment overrides, private-directory custody).
This module generalizes that same contract -- resolution order, versioned
schema, and update-preserving evolution -- for use by generated applications,
while keeping every application's namespace separate from Literate AI's own
`literate-ai/` roots.

Application runtime settings are user custody, not repository authority:

- Path: `$XDG_CONFIG_HOME/<application>/settings.json`, falling back to
  `$HOME/.config/<application>/settings.json` on Linux/macOS, or the user's
  Roaming AppData Known Folder plus `<application>/settings.json` on
  Windows. An application-specific environment variable (by default
  `<APPLICATION>_SETTINGS_FILE`) or an explicit path overrides resolution.
- Precedence: an explicit CLI value wins over an environment value, which
  wins over the settings file, which wins over the specification default.
- Schema: `literate-ai/app-settings@1` carries named profiles. Each profile
  holds non-secret `settings` and a separate `credentials` namespace where
  every entry is either a `ref` (an OS keychain or external secret-provider
  reference -- preferred) or a literal `value`. A file with any literal
  `value` credential must have owner-only permissions.
- Update preservation: `upgrade_application_settings` carries forward every
  existing profile, setting, and credential reference when introducing or
  evolving the schema, so `litai update` (or an application's own updater)
  never silently discards a user's settings file.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any

from .user_paths import (
    UserPathError,
    WindowsKnownFolderResolver,
    _absolute_environment_path,
    _absolute_known_folder,
    _host_platform_family,
    _windows_known_folder,
    resolve_user_home,
)

APP_SETTINGS_SCHEMA_V1 = "literate-ai/app-settings@1"
CURRENT_APP_SETTINGS_SCHEMA = APP_SETTINGS_SCHEMA_V1
DEFAULT_PROFILE_NAME = "default"
CREDENTIAL_REFERENCE_FIELD = "ref"
CREDENTIAL_LITERAL_FIELD = "value"
REDACTED_PLACEHOLDER = "***redacted***"

_SAFE_APPLICATION_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_MAX_SETTINGS_FILE_BYTES = 1024 * 1024


class ApplicationSettingsError(ValueError):
    """An application settings path, document, or value cannot be resolved safely."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _application_directory(application: str) -> str:
    if (
        not isinstance(application, str)
        or not application
        or application != application.strip()
    ):
        raise ApplicationSettingsError(
            "application_settings.application_invalid",
            "application namespace must be a non-empty, unpadded string",
        )
    if not _SAFE_APPLICATION_NAME.fullmatch(application):
        raise ApplicationSettingsError(
            "application_settings.application_invalid",
            "application namespace must be lowercase alphanumerics, '.', '_', or '-'",
        )
    return application


def _default_settings_environment_variable(application: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", application).strip("_").upper()
    return f"{normalized}_SETTINGS_FILE"


@dataclass(frozen=True, slots=True)
class CredentialEntry:
    """One named credential: either a reference (preferred) or a literal value."""

    ref: str | None = None
    value: str | None = None

    def is_reference(self) -> bool:
        return self.ref is not None

    def to_dict(self) -> dict[str, str]:
        if self.ref is not None:
            return {CREDENTIAL_REFERENCE_FIELD: self.ref}
        assert self.value is not None
        return {CREDENTIAL_LITERAL_FIELD: self.value}


@dataclass(frozen=True, slots=True)
class ProfileSettings:
    """One named deployment/host profile: non-secret settings plus credentials."""

    name: str
    settings: Mapping[str, Any] = field(default_factory=dict)
    credentials: Mapping[str, CredentialEntry] = field(default_factory=dict)

    def has_literal_credentials(self) -> bool:
        return any(entry.ref is None for entry in self.credentials.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "settings": dict(self.settings),
            "credentials": {
                key: entry.to_dict() for key, entry in self.credentials.items()
            },
        }


@dataclass(frozen=True, slots=True)
class ApplicationSettingsDocument:
    """A complete, versioned per-user application settings document."""

    application: str
    schema: str = CURRENT_APP_SETTINGS_SCHEMA
    active_profile: str = DEFAULT_PROFILE_NAME
    profiles: Mapping[str, ProfileSettings] = field(default_factory=dict)

    def profile(self, name: str | None = None) -> ProfileSettings:
        selected = name or self.active_profile
        try:
            return self.profiles[selected]
        except KeyError as exc:
            raise ApplicationSettingsError(
                "application_settings.profile_unknown",
                f"profile {selected!r} is not defined",
            ) from exc

    def has_literal_credentials(self) -> bool:
        return any(
            profile.has_literal_credentials() for profile in self.profiles.values()
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "application": self.application,
            "active_profile": self.active_profile,
            "profiles": {
                name: profile.to_dict() for name, profile in self.profiles.items()
            },
        }


def default_application_settings(application: str) -> ApplicationSettingsDocument:
    """The empty, spec-default document for a freshly onboarded application."""

    name = _application_directory(application)
    return ApplicationSettingsDocument(
        application=name,
        schema=CURRENT_APP_SETTINGS_SCHEMA,
        active_profile=DEFAULT_PROFILE_NAME,
        profiles={DEFAULT_PROFILE_NAME: ProfileSettings(DEFAULT_PROFILE_NAME)},
    )


def resolve_application_config_root(
    application: str,
    *,
    platform_family: str | None = None,
    environment: Mapping[str, str] | None = None,
    home: PurePath | None = None,
    windows_known_folder: WindowsKnownFolderResolver | None = None,
) -> PurePath:
    """Resolve the platform-native config root for one application namespace.

    This deliberately does not nest beneath Literate AI's own `literate-ai/`
    roots: each generated application gets its own sibling namespace, the
    same way `user_paths.py` resolves Literate AI's roots for itself.
    """

    name = _application_directory(application)
    configured = dict(os.environ if environment is None else environment)
    family = platform_family or _host_platform_family()
    if family not in {"linux", "macos", "windows"}:
        raise ApplicationSettingsError(
            "application_settings.platform_unsupported",
            f"unsupported application-settings platform family: {family!r}",
        )
    path_type = PureWindowsPath if family == "windows" else PurePosixPath
    if family == "windows":
        known_folder = windows_known_folder or _windows_known_folder
        roaming = _absolute_known_folder(known_folder("roaming"), "Roaming AppData")
        return roaming / name
    try:
        resolved_home = resolve_user_home(
            platform_family=family,
            environment=configured,
            home=home,
            windows_known_folder=windows_known_folder,
        )
    except UserPathError as exc:
        raise ApplicationSettingsError(exc.code, exc.message) from exc
    xdg_config = _absolute_environment_path(
        configured, "XDG_CONFIG_HOME", path_type=path_type
    )
    return (xdg_config or resolved_home / ".config") / name


def resolve_app_settings_path(
    application: str,
    *,
    explicit: str | Path | None = None,
    environment: Mapping[str, str] | None = None,
    environment_variable: str | None = None,
    platform_family: str | None = None,
    home: PurePath | None = None,
    windows_known_folder: WindowsKnownFolderResolver | None = None,
) -> Path:
    """Resolve the settings-file path for one application, CLI-explicit first."""

    name = _application_directory(application)
    configured = dict(os.environ if environment is None else environment)
    family = platform_family or _host_platform_family()
    path_type = PureWindowsPath if family == "windows" else PurePosixPath
    if explicit is not None:
        candidate = Path(explicit)
        if not path_type(str(explicit)).is_absolute():
            raise ApplicationSettingsError(
                "application_settings.override_relative",
                "an explicit settings path must be absolute",
            )
        return candidate
    variable = environment_variable or _default_settings_environment_variable(name)
    raw = str(configured.get(variable, "")).strip()
    if raw:
        candidate = Path(raw)
        if not path_type(raw).is_absolute():
            raise ApplicationSettingsError(
                "application_settings.override_relative",
                f"{variable} must be an absolute path",
            )
        return candidate
    root = resolve_application_config_root(
        name,
        platform_family=platform_family,
        environment=configured,
        home=home,
        windows_known_folder=windows_known_folder,
    )
    return Path(root) / "settings.json"


def resolve_setting(
    key: str,
    *,
    cli_value: Any | None = None,
    environment: Mapping[str, str] | None = None,
    environment_variable: str | None = None,
    document: ApplicationSettingsDocument | None = None,
    profile: str | None = None,
    default: Any | None = None,
) -> Any:
    """Deterministic precedence: CLI, then environment, then file, then default."""

    if cli_value is not None:
        return cli_value
    if environment_variable is not None:
        configured = os.environ if environment is None else environment
        raw = configured.get(environment_variable)
        if raw is not None and str(raw).strip() != "":
            return raw
    if document is not None:
        selected = document.profile(profile)
        if key in selected.settings:
            return selected.settings[key]
    return default


def _read_document(path: Path) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise ApplicationSettingsError(
            "application_settings.unreadable", f"settings file is unreadable: {exc}"
        ) from exc
    if len(raw) > _MAX_SETTINGS_FILE_BYTES:
        raise ApplicationSettingsError(
            "application_settings.oversized",
            "settings file exceeds the retained size limit",
        )
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApplicationSettingsError(
            "application_settings.malformed", f"settings file is not valid JSON: {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise ApplicationSettingsError(
            "application_settings.malformed", "settings file must contain a JSON object"
        )
    return parsed, raw


def _parse_credential(name: str, raw: Any) -> CredentialEntry:
    if not isinstance(raw, dict):
        raise ApplicationSettingsError(
            "application_settings.credential_invalid",
            f"credential {name!r} must be a JSON object",
        )
    ref = raw.get(CREDENTIAL_REFERENCE_FIELD)
    value = raw.get(CREDENTIAL_LITERAL_FIELD)
    has_ref = isinstance(ref, str) and ref != ""
    has_value = isinstance(value, str)
    if has_ref and has_value:
        raise ApplicationSettingsError(
            "application_settings.credential_ambiguous",
            f"credential {name!r} must not declare both 'ref' and 'value'",
        )
    if has_ref:
        return CredentialEntry(ref=ref)
    if has_value:
        return CredentialEntry(value=value)
    raise ApplicationSettingsError(
        "application_settings.credential_invalid",
        f"credential {name!r} must declare exactly one of 'ref' or 'value'",
    )


def _parse_profile(name: str, raw: Any) -> ProfileSettings:
    if not isinstance(raw, dict):
        raise ApplicationSettingsError(
            "application_settings.malformed", f"profile {name!r} must be a JSON object"
        )
    settings = raw.get("settings", {})
    if not isinstance(settings, dict):
        raise ApplicationSettingsError(
            "application_settings.malformed",
            f"profile {name!r} settings must be a JSON object",
        )
    credentials_raw = raw.get("credentials", {})
    if not isinstance(credentials_raw, dict):
        raise ApplicationSettingsError(
            "application_settings.malformed",
            f"profile {name!r} credentials must be a JSON object",
        )
    credentials = {
        key: _parse_credential(key, value) for key, value in credentials_raw.items()
    }
    return ProfileSettings(name=name, settings=dict(settings), credentials=credentials)


def parse_application_settings(
    raw: Mapping[str, Any], *, application: str
) -> ApplicationSettingsDocument:
    """Parse an already-decoded JSON object; fails closed on unsupported schema."""

    name = _application_directory(application)
    schema = raw.get("schema")
    if schema is None:
        raw = upgrade_application_settings(raw, application=name)
        schema = raw.get("schema")
    if schema != CURRENT_APP_SETTINGS_SCHEMA:
        raise ApplicationSettingsError(
            "application_settings.schema_unsupported",
            f"unsupported application-settings schema: {schema!r}",
        )
    declared_application = raw.get("application", name)
    if declared_application != name:
        raise ApplicationSettingsError(
            "application_settings.application_mismatch",
            f"settings file belongs to application {declared_application!r}, "
            f"not {name!r}",
        )
    profiles_raw = raw.get("profiles", {})
    if not isinstance(profiles_raw, dict) or not profiles_raw:
        raise ApplicationSettingsError(
            "application_settings.malformed",
            "settings file must declare at least one profile",
        )
    profiles = {
        profile_name: _parse_profile(profile_name, profile_raw)
        for profile_name, profile_raw in profiles_raw.items()
    }
    active_profile = raw.get("active_profile", DEFAULT_PROFILE_NAME)
    if not isinstance(active_profile, str) or active_profile not in profiles:
        raise ApplicationSettingsError(
            "application_settings.profile_unknown",
            f"active_profile {active_profile!r} is not among the declared profiles",
        )
    return ApplicationSettingsDocument(
        application=name,
        schema=schema,
        active_profile=active_profile,
        profiles=profiles,
    )


def upgrade_application_settings(
    raw: Mapping[str, Any], *, application: str
) -> dict[str, Any]:
    """Carry forward every existing key while introducing/evolving the schema.

    An absent `schema` field is treated as a pre-contract, unversioned file
    (for example hand-authored or written by an older application version):
    its entire top-level content becomes the default profile's `settings`,
    losing nothing. This is what lets `litai update` -- or an application's
    own updater -- introduce this convention without overwriting a user's
    existing settings.
    """

    name = _application_directory(application)
    if "schema" in raw:
        # Already versioned; nothing to upgrade under the current single
        # schema revision. Returned unchanged so callers can still fail
        # closed on a genuinely unsupported future schema.
        return dict(raw)
    preserved = {
        key: value
        for key, value in raw.items()
        if key not in {"schema", "application", "active_profile", "profiles"}
    }
    return {
        "schema": CURRENT_APP_SETTINGS_SCHEMA,
        "application": name,
        "active_profile": DEFAULT_PROFILE_NAME,
        "profiles": {DEFAULT_PROFILE_NAME: {"settings": preserved, "credentials": {}}},
    }


def load_application_settings(
    path: Path, *, application: str
) -> ApplicationSettingsDocument:
    """Load settings from `path`, or the spec default when the file is absent."""

    name = _application_directory(application)
    try:
        raw, _ = _read_document(Path(path))
    except FileNotFoundError:
        return default_application_settings(name)
    if not _is_permission_safe(Path(path)):
        # Only literal credentials require owner-only permissions; check
        # after a best-effort parse so a reference-only file is never
        # penalized for host-inherited permissive umask.
        provisional = raw
        if "schema" not in provisional:
            provisional = upgrade_application_settings(provisional, application=name)
        if _dict_has_literal_credentials(provisional):
            raise ApplicationSettingsError(
                "application_settings.insecure_permissions",
                "settings file has literal credentials but is not owner-only",
            )
    return parse_application_settings(raw, application=name)


def _dict_has_literal_credentials(raw: Mapping[str, Any]) -> bool:
    profiles = raw.get("profiles", {})
    if not isinstance(profiles, dict):
        return False
    for profile in profiles.values():
        if not isinstance(profile, dict):
            continue
        credentials = profile.get("credentials", {})
        if not isinstance(credentials, dict):
            continue
        for entry in credentials.values():
            if isinstance(entry, dict) and CREDENTIAL_LITERAL_FIELD in entry:
                return True
    return False


def _is_permission_safe(path: Path) -> bool:
    if os.name == "nt":
        return True
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return True
    return mode & (stat.S_IRWXG | stat.S_IRWXO) == 0


def write_application_settings(
    path: Path, document: ApplicationSettingsDocument
) -> None:
    """Atomically write `document`, always with owner-only file permissions."""

    destination = Path(path)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(destination.parent, 0o700)
    payload = json.dumps(document.to_dict(), indent=2, sort_keys=True) + "\n"
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    if os.name != "nt":
        os.chmod(temporary, 0o600)
    os.replace(temporary, destination)


def redact_application_settings(
    document: ApplicationSettingsDocument,
) -> dict[str, Any]:
    """A diagnostic-safe projection: literal credential values are never included.

    Credential *references* (e.g. `keychain:service/account`) are kept: a
    reference is a pointer to secret custody elsewhere, not the secret
    itself, and keeping it visible is what makes diagnostics useful. Only
    literal `value` entries are replaced with a fixed placeholder.
    """

    redacted: dict[str, Any] = {
        "schema": document.schema,
        "application": document.application,
        "active_profile": document.active_profile,
        "profiles": {},
    }
    for name, profile in document.profiles.items():
        redacted["profiles"][name] = {
            "settings": dict(profile.settings),
            "credentials": {
                key: (
                    {CREDENTIAL_REFERENCE_FIELD: entry.ref}
                    if entry.ref is not None
                    else {CREDENTIAL_LITERAL_FIELD: REDACTED_PLACEHOLDER}
                )
                for key, entry in profile.credentials.items()
            },
        }
    return redacted


def example_application_settings(application: str) -> str:
    """A repository-safe example document: no personal hosts or credentials."""

    name = _application_directory(application)
    document = ApplicationSettingsDocument(
        application=name,
        schema=CURRENT_APP_SETTINGS_SCHEMA,
        active_profile=DEFAULT_PROFILE_NAME,
        profiles={
            DEFAULT_PROFILE_NAME: ProfileSettings(
                DEFAULT_PROFILE_NAME,
                settings={"endpoint": "https://example.invalid/api"},
                credentials={
                    "api_key": CredentialEntry(ref=f"keychain:{name}/api_key")
                },
            )
        },
    )
    return json.dumps(document.to_dict(), indent=2, sort_keys=True) + "\n"


__all__ = [
    "APP_SETTINGS_SCHEMA_V1",
    "ApplicationSettingsDocument",
    "ApplicationSettingsError",
    "CREDENTIAL_LITERAL_FIELD",
    "CREDENTIAL_REFERENCE_FIELD",
    "CURRENT_APP_SETTINGS_SCHEMA",
    "CredentialEntry",
    "DEFAULT_PROFILE_NAME",
    "ProfileSettings",
    "REDACTED_PLACEHOLDER",
    "default_application_settings",
    "example_application_settings",
    "load_application_settings",
    "parse_application_settings",
    "redact_application_settings",
    "resolve_app_settings_path",
    "resolve_application_config_root",
    "resolve_setting",
    "upgrade_application_settings",
    "write_application_settings",
]
