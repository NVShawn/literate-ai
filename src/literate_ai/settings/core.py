"""Typed, layered settings with opaque secret references."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from literate_ai.storage import StorageSafetyError, canonical_json_bytes

_SETTING_KEY = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_SECRET_PROVIDER = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_SECRET_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_./:-]{0,255}$")


class SettingsError(RuntimeError):
    """A settings document or resolution operation is invalid."""


class UnknownSettingError(SettingsError):
    """A document contains a setting absent from the registry."""


class SettingsMigrationError(SettingsError):
    """No pure migration path exists for a settings document."""


class SettingScope(StrEnum):
    FRAMEWORK_DEFAULT = "framework-default"
    REPOSITORY = "repository"
    WORKSPACE = "workspace"
    USER = "user"
    MACHINE = "machine"
    RUN = "run"


SCOPE_ORDER = tuple(SettingScope)


@dataclass(frozen=True, slots=True)
class SecretReference:
    """A reference to secret material, never the secret value itself."""

    provider: str
    name: str

    def __post_init__(self) -> None:
        if not _SECRET_PROVIDER.fullmatch(self.provider):
            raise ValueError("secret provider contains unsafe characters")
        if not _SECRET_NAME.fullmatch(self.name):
            raise ValueError("secret reference name contains unsafe characters")

    def to_dict(self) -> dict[str, Any]:
        return {"$secret": {"provider": self.provider, "name": self.name}}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SecretReference:
        envelope = value.get("$secret")
        if not isinstance(envelope, Mapping):
            raise ValueError("secret setting must contain a $secret reference")
        return cls(provider=str(envelope["provider"]), name=str(envelope["name"]))

    def available(self, environment: Mapping[str, str] | None = None) -> bool:
        """Report availability without reading or returning the secret value."""

        if self.provider != "env":
            return False
        source = os.environ if environment is None else environment
        return self.name in source and bool(source[self.name])


AllowedType = type[str] | type[int] | type[float] | type[bool] | type[list] | type[dict]


@dataclass(frozen=True, slots=True)
class SettingDefinition:
    key: str
    value_type: AllowedType | type[SecretReference]
    default: Any
    allowed_scopes: frozenset[SettingScope] = frozenset(SCOPE_ORDER)
    description: str = ""

    def __post_init__(self) -> None:
        _validate_key(self.key)
        if not self.allowed_scopes:
            raise ValueError("setting must be allowed in at least one scope")
        _validate_value(self, self.default)

    @property
    def secret(self) -> bool:
        return self.value_type is SecretReference


@dataclass(frozen=True, slots=True)
class SettingsDocument:
    schema_version: int
    scope: SettingScope
    values: dict[str, Any]

    def __post_init__(self) -> None:
        if self.schema_version < 1:
            raise ValueError("settings schema version must be positive")
        for key in self.values:
            _validate_key(key)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "scope": self.scope.value,
            "values": {
                key: _serialize_value(value)
                for key, value in sorted(self.values.items())
            },
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SettingsDocument:
        raw_values = value.get("values")
        if not isinstance(raw_values, Mapping):
            raise SettingsError("settings values must be an object")
        return cls(
            schema_version=int(value["schema_version"]),
            scope=SettingScope(str(value["scope"])),
            values={str(key): item for key, item in raw_values.items()},
        )


@dataclass(frozen=True, slots=True)
class SettingContribution:
    scope: SettingScope
    value: Any
    value_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope.value,
            "value": _serialize_value(self.value),
            "value_digest": self.value_digest,
        }


@dataclass(frozen=True, slots=True)
class SettingResolution:
    key: str
    value: Any
    effective_scope: SettingScope
    history: tuple[SettingContribution, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value": _serialize_value(self.value),
            "effective_scope": self.effective_scope.value,
            "history": [item.to_dict() for item in self.history],
        }


@dataclass(frozen=True, slots=True)
class SettingsSnapshot:
    schema_version: int
    settings: dict[str, SettingResolution]

    def value(self, key: str) -> Any:
        try:
            return self.settings[key].value
        except KeyError as exc:
            raise UnknownSettingError(f"unknown setting {key!r}") from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "settings": {
                key: item.to_dict() for key, item in sorted(self.settings.items())
            },
        }


Migration = Callable[[dict[str, Any]], dict[str, Any]]


class SettingsMigrator:
    """Registry of pure one-version-at-a-time document migrations."""

    def __init__(self, current_version: int = 1) -> None:
        if current_version < 1:
            raise ValueError("current settings version must be positive")
        self.current_version = current_version
        self._migrations: dict[int, Migration] = {}

    def register(self, from_version: int, migration: Migration) -> None:
        if from_version < 1 or from_version >= self.current_version:
            raise ValueError("migration source must precede the current version")
        if from_version in self._migrations:
            raise ValueError("migration already registered for this version")
        self._migrations[from_version] = migration

    def migrate(self, document: SettingsDocument) -> SettingsDocument:
        if document.schema_version > self.current_version:
            raise SettingsMigrationError("settings document is from a newer schema")
        raw = document.to_dict()
        version = document.schema_version
        while version < self.current_version:
            migration = self._migrations.get(version)
            if migration is None:
                raise SettingsMigrationError(
                    f"no settings migration from version {version}"
                )
            # A canonical round-trip prevents migration functions from retaining
            # aliases into the caller's document.
            migrated = migration(json.loads(canonical_json_bytes(raw)))
            if not isinstance(migrated, dict):
                raise SettingsMigrationError("settings migration returned no document")
            next_version = int(migrated.get("schema_version", 0))
            if next_version != version + 1:
                raise SettingsMigrationError(
                    "settings migrations must advance exactly one version"
                )
            raw = migrated
            version = next_version
        return SettingsDocument.from_dict(raw)


class SettingsRegistry:
    """Validate and resolve settings in a deterministic six-scope order."""

    def __init__(
        self,
        definitions: Iterable[SettingDefinition],
        *,
        migrator: SettingsMigrator | None = None,
    ) -> None:
        self.migrator = migrator or SettingsMigrator()
        self.definitions: dict[str, SettingDefinition] = {}
        for definition in definitions:
            if definition.key in self.definitions:
                raise ValueError(f"duplicate setting definition {definition.key!r}")
            self.definitions[definition.key] = definition

    def validate_document(self, document: SettingsDocument) -> SettingsDocument:
        migrated = self.migrator.migrate(document)
        if migrated.scope == SettingScope.FRAMEWORK_DEFAULT:
            raise SettingsError(
                "framework defaults are declared by the registry, not documents"
            )
        values: dict[str, Any] = {}
        for key, raw_value in migrated.values.items():
            definition = self.definitions.get(key)
            if definition is None:
                raise UnknownSettingError(f"unknown setting {key!r}")
            if migrated.scope not in definition.allowed_scopes:
                raise SettingsError(
                    f"setting {key!r} is not allowed in {migrated.scope.value} scope"
                )
            value = _deserialize_value(definition, raw_value)
            _validate_value(definition, value)
            values[key] = value
        return SettingsDocument(
            schema_version=self.migrator.current_version,
            scope=migrated.scope,
            values=values,
        )

    def resolve(self, documents: Iterable[SettingsDocument]) -> SettingsSnapshot:
        by_scope: dict[SettingScope, SettingsDocument] = {}
        for document in documents:
            validated = self.validate_document(document)
            if validated.scope in by_scope:
                raise SettingsError(
                    f"more than one document supplied for {validated.scope.value}"
                )
            by_scope[validated.scope] = validated

        settings: dict[str, SettingResolution] = {}
        for key, definition in sorted(self.definitions.items()):
            contributions = [
                SettingContribution(
                    scope=SettingScope.FRAMEWORK_DEFAULT,
                    value=definition.default,
                    value_digest=_value_digest(definition.default),
                )
            ]
            for scope in SCOPE_ORDER[1:]:
                document = by_scope.get(scope)
                if document is None or key not in document.values:
                    continue
                value = document.values[key]
                contributions.append(
                    SettingContribution(
                        scope=scope,
                        value=value,
                        value_digest=_value_digest(value),
                    )
                )
            effective = contributions[-1]
            settings[key] = SettingResolution(
                key=key,
                value=effective.value,
                effective_scope=effective.scope,
                history=tuple(contributions),
            )
        return SettingsSnapshot(
            schema_version=self.migrator.current_version,
            settings=settings,
        )


class SettingsFileStore:
    """Atomic persistence for one scoped, validated settings document."""

    def __init__(self, root: str | Path, registry: SettingsRegistry) -> None:
        configured = Path(root).expanduser()
        if configured.is_symlink():
            raise StorageSafetyError("settings root must not be a symbolic link")
        configured.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root = configured.resolve(strict=True)
        self.registry = registry

    def save(self, document: SettingsDocument) -> Path:
        validated = self.registry.validate_document(document)
        path = self._path(validated.scope)
        if path.is_symlink():
            raise StorageSafetyError("settings document must not be a symbolic link")
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=self.root, text=True
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(validated.to_dict(), output, indent=2, sort_keys=True)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temp_path, 0o600)
            temp_path.replace(path)
        finally:
            temp_path.unlink(missing_ok=True)
        return path

    def load(self, scope: SettingScope) -> SettingsDocument | None:
        path = self._path(scope)
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file():
            raise StorageSafetyError("settings document must be a regular file")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            document = SettingsDocument.from_dict(raw)
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            KeyError,
            ValueError,
        ) as exc:
            raise SettingsError(f"invalid {scope.value} settings document") from exc
        if document.scope != scope:
            raise SettingsError("settings document scope does not match its file")
        return self.registry.validate_document(document)

    def load_all(self) -> tuple[SettingsDocument, ...]:
        return tuple(
            document
            for scope in SCOPE_ORDER[1:]
            if (document := self.load(scope)) is not None
        )

    def _path(self, scope: SettingScope) -> Path:
        if scope == SettingScope.FRAMEWORK_DEFAULT:
            raise SettingsError("framework defaults are schema, not persisted settings")
        return self.root / f"{scope.value}.json"


def _validate_key(value: str) -> None:
    if not _SETTING_KEY.fullmatch(value):
        raise ValueError(f"invalid setting key {value!r}")


def _validate_value(definition: SettingDefinition, value: Any) -> None:
    expected = definition.value_type
    if expected is SecretReference:
        if not isinstance(value, SecretReference):
            raise SettingsError(
                f"secret setting {definition.key!r} requires a SecretReference"
            )
        return
    # bool is an int subclass; enforce exact booleans/integers to avoid drift.
    if expected is int and (not isinstance(value, int) or isinstance(value, bool)):
        raise SettingsError(f"setting {definition.key!r} must be an integer")
    if expected is float and (
        not isinstance(value, (float, int)) or isinstance(value, bool)
    ):
        raise SettingsError(f"setting {definition.key!r} must be a number")
    if expected not in {int, float} and not isinstance(value, expected):
        raise SettingsError(f"setting {definition.key!r} must be {expected.__name__}")
    try:
        canonical_json_bytes(value)
    except (TypeError, ValueError) as exc:
        raise SettingsError(
            f"setting {definition.key!r} must be finite JSON-compatible data"
        ) from exc


def _serialize_value(value: Any) -> Any:
    return value.to_dict() if isinstance(value, SecretReference) else value


def _deserialize_value(definition: SettingDefinition, value: Any) -> Any:
    if definition.value_type is SecretReference:
        if not isinstance(value, Mapping):
            raise SettingsError(
                f"secret setting {definition.key!r} contains a raw value"
            )
        try:
            return SecretReference.from_dict(value)
        except (KeyError, TypeError, ValueError) as exc:
            raise SettingsError(
                f"secret setting {definition.key!r} has an invalid reference"
            ) from exc
    return value


def _value_digest(value: Any) -> str:
    serialized = _serialize_value(value)
    return "sha256:" + hashlib.sha256(canonical_json_bytes(serialized)).hexdigest()
