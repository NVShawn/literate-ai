"""Canonical identities shared by all version-1 contracts.

Canonical JSON v1 deliberately uses a portable JSON subset: null, booleans,
signed 64-bit integers, Unicode strings, arrays, and string-keyed objects.
Floating-point values are rejected. Objects are UTF-8 encoded with keys ordered
by Unicode code point and no insignificant whitespace. An identity is SHA-256 of
those exact bytes and is rendered as a lower-case hexadecimal digest.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar, Protocol

from ._validation import (
    ContractValidationError,
    contract_fields,
    enum_value,
    fail,
    fields,
    string_value,
)
from .versioning import semantic_version

SCHEMA_PREFIX = "urn:literate-ai:schema:v1:"
SCHEMA_V2_PREFIX = "urn:literate-ai:schema:v2:"
CONTENT_IDENTITY_SCHEMA = f"{SCHEMA_PREFIX}content-identity"
CONTENT_REFERENCE_SCHEMA = f"{SCHEMA_PREFIX}content-reference"
VERSIONED_CONTENT_REFERENCE_SCHEMA = f"{SCHEMA_PREFIX}versioned-content-reference"
COMPONENT_REVISION_REFERENCE_SCHEMA = f"{SCHEMA_PREFIX}component-revision-reference"

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_NAMESPACE_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")
_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")


class WireContract(Protocol):
    SCHEMA: ClassVar[str]

    def to_dict(self) -> dict[str, Any]: ...


class HashAlgorithm(StrEnum):
    SHA256 = "sha256"


def _canonical_value(value: Any, path: str = "$") -> Any:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        if not -(2**63) <= value <= 2**63 - 1:
            fail(path, "integer is outside canonical signed 64-bit range")
        return value
    if isinstance(value, float):
        fail(path, "floating-point values are not supported by canonical JSON v1")
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key in value:
            if not isinstance(key, str):
                fail(path, "object keys must be strings")
        for key in sorted(value):
            result[key] = _canonical_value(value[key], f"{path}.{key}")
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            _canonical_value(item, f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    fail(path, f"unsupported canonical value type: {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    """Return deterministic canonical JSON v1 bytes for a portable value."""

    normalized = _canonical_value(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class ContentIdentity:
    algorithm: HashAlgorithm
    digest: str

    SCHEMA: ClassVar[str] = CONTENT_IDENTITY_SCHEMA

    def __post_init__(self) -> None:
        if self.algorithm is not HashAlgorithm.SHA256:
            fail("ContentIdentity.algorithm", "only sha256 is supported in schema v1")
        if not _DIGEST_RE.fullmatch(self.digest):
            fail(
                "ContentIdentity.digest", "must be 64 lower-case hexadecimal characters"
            )

    @property
    def uri(self) -> str:
        return f"{self.algorithm.value}:{self.digest}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "algorithm": self.algorithm.value,
            "digest": self.digest,
        }

    @classmethod
    def from_dict(cls, value: Any, *, path: str = "ContentIdentity") -> ContentIdentity:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"algorithm", "digest"}),
        )
        return cls(
            algorithm=enum_value(HashAlgorithm, data["algorithm"], f"{path}.algorithm"),
            digest=string_value(data["digest"], f"{path}.digest"),
        )

    @classmethod
    def parse_uri(cls, value: str) -> ContentIdentity:
        raw = string_value(value, "ContentIdentity.uri")
        algorithm, separator, digest = raw.partition(":")
        if not separator:
            fail("ContentIdentity.uri", "must use algorithm:digest form")
        return cls(enum_value(HashAlgorithm, algorithm, "ContentIdentity.uri"), digest)


def canonical_identity(value: Any) -> ContentIdentity:
    return ContentIdentity(
        HashAlgorithm.SHA256,
        hashlib.sha256(canonical_json_bytes(value)).hexdigest(),
    )


def contract_identity(value: WireContract) -> ContentIdentity:
    return canonical_identity(value.to_dict())


@dataclass(frozen=True, slots=True)
class ContentReference:
    """A typed reference to immutable content without embedding it."""

    kind: str
    uri: str
    identity: ContentIdentity

    SCHEMA: ClassVar[str] = CONTENT_REFERENCE_SCHEMA

    def __post_init__(self) -> None:
        string_value(self.kind, "ContentReference.kind")
        string_value(self.uri, "ContentReference.uri")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "kind": self.kind,
            "uri": self.uri,
            "identity": self.identity.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ContentReference"
    ) -> ContentReference:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"kind", "uri", "identity"}),
        )
        return cls(
            kind=string_value(data["kind"], f"{path}.kind"),
            uri=string_value(data["uri"], f"{path}.uri"),
            identity=ContentIdentity.from_dict(
                data["identity"], path=f"{path}.identity"
            ),
        )


@dataclass(frozen=True, slots=True)
class ComponentCoordinate:
    namespace: str
    name: str

    def __post_init__(self) -> None:
        if not _NAMESPACE_RE.fullmatch(self.namespace):
            fail(
                "ComponentCoordinate.namespace",
                "must be a lower-case portable namespace",
            )
        if not _NAME_RE.fullmatch(self.name):
            fail("ComponentCoordinate.name", "must be a lower-case portable name")

    @property
    def uri(self) -> str:
        return f"component://{self.namespace}/{self.name}"

    def to_dict(self) -> dict[str, str]:
        return {"namespace": self.namespace, "name": self.name}

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ComponentCoordinate"
    ) -> ComponentCoordinate:
        data = fields(
            value,
            path=path,
            required=frozenset({"namespace", "name"}),
        )
        return cls(
            namespace=string_value(data["namespace"], f"{path}.namespace"),
            name=string_value(data["name"], f"{path}.name"),
        )

    @classmethod
    def parse(cls, value: str) -> ComponentCoordinate:
        raw = string_value(value, "ComponentCoordinate")
        prefix = "component://"
        if not raw.startswith(prefix):
            fail("ComponentCoordinate", f"must start with {prefix!r}")
        namespace, separator, name = raw[len(prefix) :].partition("/")
        if not separator or "/" in name:
            fail("ComponentCoordinate", "must contain exactly one namespace/name pair")
        return cls(namespace, name)


@dataclass(frozen=True, slots=True)
class VersionedContentRef:
    """An exact logical-version/content triple for any first-class object."""

    kind: str
    identifier: str
    version: str
    content_identity: ContentIdentity

    SCHEMA: ClassVar[str] = VERSIONED_CONTENT_REFERENCE_SCHEMA

    def __post_init__(self) -> None:
        string_value(self.kind, "VersionedContentRef.kind")
        string_value(self.identifier, "VersionedContentRef.identifier")
        semantic_version(self.version, "VersionedContentRef.version")
        if not isinstance(self.content_identity, ContentIdentity):
            fail(
                "VersionedContentRef.content_identity",
                "must be a ContentIdentity",
            )

    @property
    def uri(self) -> str:
        return (
            f"versioned://{self.kind}/{self.identifier}@{self.version}"
            f"#{self.content_identity.uri}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "kind": self.kind,
            "identifier": self.identifier,
            "version": self.version,
            "content_identity": self.content_identity.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "VersionedContentRef"
    ) -> VersionedContentRef:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"kind", "identifier", "version", "content_identity"}),
        )
        return cls(
            kind=string_value(data["kind"], f"{path}.kind"),
            identifier=string_value(data["identifier"], f"{path}.identifier"),
            version=semantic_version(data["version"], f"{path}.version"),
            content_identity=ContentIdentity.from_dict(
                data["content_identity"], path=f"{path}.content_identity"
            ),
        )


@dataclass(frozen=True, slots=True)
class ComponentRevisionRef:
    """The exact coordinate, SemVer, and immutable revision of a Component."""

    coordinate: ComponentCoordinate
    version: str
    revision_identity: ContentIdentity

    SCHEMA: ClassVar[str] = COMPONENT_REVISION_REFERENCE_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.coordinate, ComponentCoordinate):
            fail("ComponentRevisionRef.coordinate", "must be a ComponentCoordinate")
        semantic_version(self.version, "ComponentRevisionRef.version")
        if not isinstance(self.revision_identity, ContentIdentity):
            fail(
                "ComponentRevisionRef.revision_identity",
                "must be a ContentIdentity",
            )

    @property
    def uri(self) -> str:
        return f"{self.coordinate.uri}@{self.version}#{self.revision_identity.uri}"

    @property
    def versioned_content(self) -> VersionedContentRef:
        return VersionedContentRef(
            "component",
            self.coordinate.uri,
            self.version,
            self.revision_identity,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "coordinate": self.coordinate.to_dict(),
            "version": self.version,
            "revision_identity": self.revision_identity.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "ComponentRevisionRef"
    ) -> ComponentRevisionRef:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"coordinate", "version", "revision_identity"}),
        )
        return cls(
            coordinate=ComponentCoordinate.from_dict(
                data["coordinate"], path=f"{path}.coordinate"
            ),
            version=semantic_version(data["version"], f"{path}.version"),
            revision_identity=ContentIdentity.from_dict(
                data["revision_identity"], path=f"{path}.revision_identity"
            ),
        )


@dataclass(frozen=True, slots=True)
class FlavorCoordinate:
    namespace: str
    name: str

    def __post_init__(self) -> None:
        if not _NAMESPACE_RE.fullmatch(self.namespace):
            fail(
                "FlavorCoordinate.namespace", "must be a lower-case portable namespace"
            )
        if not _NAME_RE.fullmatch(self.name):
            fail("FlavorCoordinate.name", "must be a lower-case portable name")

    @property
    def uri(self) -> str:
        return f"flavor://{self.namespace}/{self.name}"

    def to_dict(self) -> dict[str, str]:
        return {"namespace": self.namespace, "name": self.name}

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "FlavorCoordinate"
    ) -> FlavorCoordinate:
        data = fields(
            value,
            path=path,
            required=frozenset({"namespace", "name"}),
        )
        return cls(
            namespace=string_value(data["namespace"], f"{path}.namespace"),
            name=string_value(data["name"], f"{path}.name"),
        )


__all__ = [
    "COMPONENT_REVISION_REFERENCE_SCHEMA",
    "CONTENT_IDENTITY_SCHEMA",
    "CONTENT_REFERENCE_SCHEMA",
    "VERSIONED_CONTENT_REFERENCE_SCHEMA",
    "SCHEMA_PREFIX",
    "SCHEMA_V2_PREFIX",
    "ComponentCoordinate",
    "ComponentRevisionRef",
    "ContentIdentity",
    "ContentReference",
    "ContractValidationError",
    "FlavorCoordinate",
    "HashAlgorithm",
    "VersionedContentRef",
    "canonical_identity",
    "canonical_json_bytes",
    "contract_identity",
]
