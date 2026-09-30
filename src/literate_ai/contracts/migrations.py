"""Pure, deterministic, one-version-at-a-time wire-contract migrations."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .identity import ContentIdentity, canonical_identity


class LifecycleSeam(StrEnum):
    """Provider-neutral lifecycle seams that may move independently."""

    SOURCE_IDENTITY = "source-identity"
    INTELLIGENCE = "intelligence"
    COMPOSITION = "composition"
    GENERATION = "generation"
    VALIDATION = "validation"
    SECURITY_BUILD = "security-build"
    PACKAGING_LINKING = "packaging-linking"
    PUBLICATION = "publication"
    SETTINGS = "settings"


_LIFECYCLE_CONTRACTS = (
    "literate_ai.application.migration.LifecycleBridge",
    "literate_ai.contracts.migrations.FrameworkReleaseIdentity",
    "literate_ai.contracts.migrations.LifecycleRequest",
    "literate_ai.contracts.migrations.LifecycleResult",
)


@dataclass(frozen=True, slots=True)
class FrameworkReleaseIdentity:
    """Exact framework release and public migration-contract set."""

    distribution: str
    version: str
    contract_names: tuple[str, ...] = _LIFECYCLE_CONTRACTS

    def __post_init__(self) -> None:
        if not self.distribution.strip() or not self.version.strip():
            raise ValueError("framework distribution and version must not be empty")
        if (
            not self.contract_names
            or self.contract_names != tuple(sorted(self.contract_names))
            or len(self.contract_names) != len(set(self.contract_names))
        ):
            raise ValueError("framework contract names must be unique and sorted")

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "distribution": self.distribution,
            "version": self.version,
            "contract_names": list(self.contract_names),
        }


def framework_release_identity(version: str) -> FrameworkReleaseIdentity:
    """Return the standard identity for one installed literate-ai release."""

    return FrameworkReleaseIdentity("literate-ai", version)


@dataclass(frozen=True, slots=True)
class LifecycleRequest:
    """Immutable typed request binding semantic input and original wire bytes."""

    request_id: str
    seam: LifecycleSeam
    operation: str
    input_wire_digest: str
    input_payload_identity: ContentIdentity
    framework_release_identity: ContentIdentity
    read_only: bool

    def __post_init__(self) -> None:
        if not self.request_id.strip() or not self.operation.strip():
            raise ValueError("lifecycle request and operation identities are required")
        if not isinstance(self.seam, LifecycleSeam):
            raise ValueError("lifecycle request seam is invalid")
        _require_sha256_uri(self.input_wire_digest, "input wire digest")

    def to_dict(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "seam": self.seam.value,
            "operation": self.operation,
            "input_wire_digest": self.input_wire_digest,
            "input_payload_identity": self.input_payload_identity.to_dict(),
            "framework_release_identity": self.framework_release_identity.to_dict(),
            "read_only": self.read_only,
        }


@dataclass(frozen=True, slots=True)
class LifecycleResult:
    """Typed result proving which release handled one exact lifecycle request."""

    request_id: str
    seam: LifecycleSeam
    operation: str
    output_wire_digest: str
    output_payload_identity: ContentIdentity
    framework_release_identity: ContentIdentity
    wrote: bool
    diagnostic_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.request_id.strip() or not self.operation.strip():
            raise ValueError("lifecycle result and operation identities are required")
        if not isinstance(self.seam, LifecycleSeam):
            raise ValueError("lifecycle result seam is invalid")
        _require_sha256_uri(self.output_wire_digest, "output wire digest")
        if self.diagnostic_codes != tuple(sorted(set(self.diagnostic_codes))):
            raise ValueError("lifecycle diagnostics must be unique and sorted")

    def to_dict(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "seam": self.seam.value,
            "operation": self.operation,
            "output_wire_digest": self.output_wire_digest,
            "output_payload_identity": self.output_payload_identity.to_dict(),
            "framework_release_identity": self.framework_release_identity.to_dict(),
            "wrote": self.wrote,
            "diagnostic_codes": list(self.diagnostic_codes),
        }


def wire_digest(value: bytes) -> str:
    """Digest exact, unparsed bytes at a compatibility seam."""

    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _require_sha256_uri(value: str, label: str) -> None:
    try:
        identity = ContentIdentity.parse_uri(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a sha256 content identity") from exc
    if identity.uri != value:
        raise ValueError(f"{label} must use canonical sha256 form")


class MigrationError(RuntimeError):
    """A stable failure to find or execute a safe schema migration."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


Migration = Callable[[dict[str, Any]], dict[str, Any]]
ContractParser = Callable[[Mapping[str, Any]], Any]

_AT_VERSION = re.compile(r"^(.*)@(\d+)$")
_URN_VERSION = re.compile(r"^(urn:[^:]+:schema:)v(\d+):(.*)$")


def _schema_lineage(schema: str) -> tuple[str, int] | None:
    match = _AT_VERSION.fullmatch(schema)
    if match:
        return match.group(1), int(match.group(2))
    match = _URN_VERSION.fullmatch(schema)
    if match:
        return f"{match.group(1)}v?:{match.group(3)}", int(match.group(2))
    return None


def _canonical_clone(value: Mapping[str, Any]) -> dict[str, Any]:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise MigrationError(
            "contracts.migration_input_not_json",
            "migration input must be canonical JSON data",
        ) from exc
    if not isinstance(decoded, dict):
        raise MigrationError(
            "contracts.migration_input_not_object",
            "migration input must be a JSON object",
        )
    return decoded


class SchemaMigrationRegistry:
    """Register explicit schema edges and migrate without mutating caller data."""

    def __init__(self) -> None:
        self._steps: dict[str, tuple[str, Migration]] = {}

    def register(
        self, source_schema: str, target_schema: str, migration: Migration
    ) -> None:
        if not source_schema or not target_schema or source_schema == target_schema:
            raise ValueError("migration schemas must be distinct and non-empty")
        if source_schema in self._steps:
            raise ValueError(f"migration already registered for {source_schema!r}")
        self._steps[source_schema] = (target_schema, migration)

    def migrate(
        self,
        document: Mapping[str, Any],
        *,
        target_schema: str,
    ) -> dict[str, Any]:
        original = _canonical_clone(document)
        working = copy.deepcopy(original)
        current = working.get("schema")
        if not isinstance(current, str) or not current:
            raise MigrationError(
                "contracts.migration_schema_missing",
                "migration input has no schema identity",
            )
        visited: set[str] = set()
        while current != target_schema:
            if current in visited:
                raise MigrationError(
                    "contracts.migration_cycle",
                    "schema migration graph contains a cycle",
                )
            visited.add(current)
            step = self._steps.get(current)
            if step is None:
                source_lineage = _schema_lineage(current)
                target_lineage = _schema_lineage(target_schema)
                if (
                    source_lineage is not None
                    and target_lineage is not None
                    and source_lineage[0] == target_lineage[0]
                    and source_lineage[1] > target_lineage[1]
                ):
                    raise MigrationError(
                        "contracts.migration_future_schema",
                        f"cannot read future schema {current!r} as {target_schema!r}",
                    )
                raise MigrationError(
                    "contracts.migration_path_missing",
                    f"no migration from {current!r} to {target_schema!r}",
                )
            next_schema, transform = step
            first_input = copy.deepcopy(working)
            second_input = copy.deepcopy(working)
            try:
                first = _canonical_clone(transform(first_input))
                second = _canonical_clone(transform(second_input))
            except MigrationError:
                raise
            except Exception as exc:
                raise MigrationError(
                    "contracts.migration_failed",
                    f"migration from {current!r} failed",
                ) from exc
            if first != second:
                raise MigrationError(
                    "contracts.migration_nondeterministic",
                    f"migration from {current!r} is not deterministic",
                )
            if first.get("schema") != next_schema:
                raise MigrationError(
                    "contracts.migration_wrong_target",
                    f"migration from {current!r} did not produce {next_schema!r}",
                )
            working = first
            current = next_schema
        if _canonical_clone(document) != original:
            raise MigrationError(
                "contracts.migration_mutated_input",
                "migration mutated its caller-owned input",
            )
        return working


class SchemaCompatibilityReader:
    """Parse current documents or explicitly registered legacy migrations.

    The reader never guesses a version, chooses a latest object, or accepts a
    future schema.  A migration that lacks enough identity data should raise
    ``MigrationError('contracts.migration_legacy_ambiguous', ...)``.
    """

    def __init__(
        self,
        *,
        current_schema: str,
        parser: ContractParser,
        migrations: SchemaMigrationRegistry | None = None,
        legacy_schemas: frozenset[str] = frozenset(),
    ) -> None:
        if not current_schema:
            raise ValueError("current schema must not be empty")
        self.current_schema = current_schema
        self.parser = parser
        self.migrations = migrations or SchemaMigrationRegistry()
        self.legacy_schemas = legacy_schemas

    def read(self, document: Mapping[str, Any]) -> Any:
        cloned = _canonical_clone(document)
        source_schema = cloned.get("schema")
        if not isinstance(source_schema, str) or not source_schema:
            raise MigrationError(
                "contracts.migration_schema_missing",
                "compatibility input has no schema identity",
            )
        if source_schema == self.current_schema:
            return self.parser(cloned)
        source_lineage = _schema_lineage(source_schema)
        current_lineage = _schema_lineage(self.current_schema)
        if (
            source_lineage is not None
            and current_lineage is not None
            and source_lineage[0] == current_lineage[0]
            and source_lineage[1] > current_lineage[1]
        ):
            raise MigrationError(
                "contracts.migration_future_schema",
                f"cannot read future schema {source_schema!r}",
            )
        if source_schema not in self.legacy_schemas:
            raise MigrationError(
                "contracts.migration_schema_unsupported",
                f"schema {source_schema!r} is not an admitted legacy schema",
            )
        migrated = self.migrations.migrate(
            cloned,
            target_schema=self.current_schema,
        )
        return self.parser(migrated)


def legacy_ambiguity(message: str) -> MigrationError:
    """Create the stable fail-closed error used by context-requiring migrations."""

    return MigrationError("contracts.migration_legacy_ambiguous", message)


__all__ = [
    "FrameworkReleaseIdentity",
    "LifecycleRequest",
    "LifecycleResult",
    "LifecycleSeam",
    "Migration",
    "MigrationError",
    "SchemaMigrationRegistry",
    "SchemaCompatibilityReader",
    "framework_release_identity",
    "legacy_ambiguity",
    "wire_digest",
]
