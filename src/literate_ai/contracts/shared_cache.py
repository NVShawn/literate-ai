"""Private provider-neutral configuration for verified shared caches."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar
from urllib.parse import urlsplit

from ._validation import (
    bool_value,
    contract_fields,
    enum_value,
    fail,
    int_value,
    list_value,
    optional_string,
    string_value,
)
from .identity import ContentIdentity, canonical_identity, contract_identity

SHARED_CACHE_CONFIGURATION_SCHEMA = (
    "urn:literate-ai:schema:v2:shared-cache-configuration"
)
SHARED_CACHE_NAMESPACE_POLICY_SCHEMA = (
    "urn:literate-ai:schema:v2:shared-cache-namespace-policy"
)

_PORTABLE_ID = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")
_CREDENTIAL_REFERENCE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._:/-]{0,254}[A-Za-z0-9])?$"
)


class SharedCacheScope(StrEnum):
    USER = "user"
    TEAM = "team"
    ORGANIZATION = "organization"
    COMPANY = "company"


class SharedCacheAccessMode(StrEnum):
    READ_ONLY = "read-only"
    READ_WRITE = "read-write"

    @property
    def can_write(self) -> bool:
        return self is SharedCacheAccessMode.READ_WRITE


class SharedCacheNamespace(StrEnum):
    BAZEL = "bazel"
    COMPILER = "compiler"
    TEST = "test"
    PACKAGE = "package"
    CONTAINER = "container"


def _portable_id(value: object, path: str) -> str:
    result = string_value(value, path, max_length=64)
    if _PORTABLE_ID.fullmatch(result) is None:
        fail(path, "must be a portable lower-case identifier")
    return result


@dataclass(frozen=True, slots=True)
class SharedCacheNamespacePolicy:
    namespace: SharedCacheNamespace
    mode: SharedCacheAccessMode

    SCHEMA: ClassVar[str] = SHARED_CACHE_NAMESPACE_POLICY_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.namespace, SharedCacheNamespace):
            fail("SharedCacheNamespacePolicy.namespace", "must be typed")
        if not isinstance(self.mode, SharedCacheAccessMode):
            fail("SharedCacheNamespacePolicy.mode", "must be typed")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "namespace": self.namespace.value,
            "mode": self.mode.value,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "SharedCacheNamespacePolicy"
    ) -> SharedCacheNamespacePolicy:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"namespace", "mode"}),
        )
        return cls(
            enum_value(SharedCacheNamespace, data["namespace"], f"{path}.namespace"),
            enum_value(SharedCacheAccessMode, data["mode"], f"{path}.mode"),
        )


@dataclass(frozen=True, slots=True)
class SharedCacheConfiguration:
    scope: SharedCacheScope
    namespace: str
    local_root_reference: str
    policies: tuple[SharedCacheNamespacePolicy, ...]
    maximum_bytes: int
    retention_seconds: int
    endpoint: str | None = None
    tls_required: bool = True
    credential_reference: str | None = None

    SCHEMA: ClassVar[str] = SHARED_CACHE_CONFIGURATION_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.scope, SharedCacheScope):
            fail("SharedCacheConfiguration.scope", "must be typed")
        _portable_id(self.namespace, "SharedCacheConfiguration.namespace")
        _portable_id(
            self.local_root_reference,
            "SharedCacheConfiguration.local_root_reference",
        )
        if not self.policies or any(
            not isinstance(item, SharedCacheNamespacePolicy) for item in self.policies
        ):
            fail(
                "SharedCacheConfiguration.policies",
                "must contain typed namespace policies",
            )
        names = tuple(item.namespace.value for item in self.policies)
        if names != tuple(sorted(set(names))):
            fail(
                "SharedCacheConfiguration.policies",
                "must be uniquely sorted by namespace",
            )
        int_value(
            self.maximum_bytes,
            "SharedCacheConfiguration.maximum_bytes",
            minimum=1,
        )
        int_value(
            self.retention_seconds,
            "SharedCacheConfiguration.retention_seconds",
            minimum=1,
        )
        bool_value(self.tls_required, "SharedCacheConfiguration.tls_required")
        if self.endpoint is None:
            if self.credential_reference is not None:
                fail(
                    "SharedCacheConfiguration.credential_reference",
                    "requires a network endpoint",
                )
            return
        endpoint = string_value(
            self.endpoint, "SharedCacheConfiguration.endpoint", max_length=4096
        )
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            fail(
                "SharedCacheConfiguration.endpoint",
                "must be one credential-free HTTP(S) URL without query or fragment",
            )
        if self.tls_required and parsed.scheme != "https":
            fail(
                "SharedCacheConfiguration.endpoint",
                "must use HTTPS when TLS is required",
            )
        if (
            self.credential_reference is not None
            and _CREDENTIAL_REFERENCE.fullmatch(self.credential_reference) is None
        ):
            fail(
                "SharedCacheConfiguration.credential_reference",
                "must be an opaque portable reference, not credential material",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    @property
    def storage_identity(self) -> ContentIdentity:
        """Bind shared content to its scope without binding consumer permissions.

        Transport, credentials, local placement, retention, and access mode remain
        part of the full configuration identity used for dispatch. They cannot
        change the identity of bytes shared between writers and read-only readers.
        """
        return canonical_identity(
            {
                "schema": "literate-ai/shared-cache-storage@1",
                "scope": self.scope.value,
                "namespace": self.namespace,
            }
        )

    def policy(self, namespace: SharedCacheNamespace) -> SharedCacheNamespacePolicy:
        if not isinstance(namespace, SharedCacheNamespace):
            fail("SharedCacheConfiguration.policy", "namespace must be typed")
        match = next(
            (item for item in self.policies if item.namespace is namespace), None
        )
        if match is None:
            fail(
                "SharedCacheConfiguration.policy",
                f"namespace {namespace.value!r} is not configured",
            )
        return match

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "scope": self.scope.value,
            "namespace": self.namespace,
            "local_root_reference": self.local_root_reference,
            "policies": [item.to_dict() for item in self.policies],
            "maximum_bytes": self.maximum_bytes,
            "retention_seconds": self.retention_seconds,
            "endpoint": self.endpoint,
            "tls_required": self.tls_required,
            "credential_reference": self.credential_reference,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "SharedCacheConfiguration"
    ) -> SharedCacheConfiguration:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "scope",
                    "namespace",
                    "local_root_reference",
                    "policies",
                    "maximum_bytes",
                    "retention_seconds",
                    "endpoint",
                    "tls_required",
                    "credential_reference",
                }
            ),
        )
        return cls(
            enum_value(SharedCacheScope, data["scope"], f"{path}.scope"),
            _portable_id(data["namespace"], f"{path}.namespace"),
            _portable_id(data["local_root_reference"], f"{path}.local_root_reference"),
            tuple(
                SharedCacheNamespacePolicy.from_dict(
                    item, path=f"{path}.policies[{index}]"
                )
                for index, item in enumerate(
                    list_value(data["policies"], f"{path}.policies")
                )
            ),
            int_value(data["maximum_bytes"], f"{path}.maximum_bytes", minimum=1),
            int_value(
                data["retention_seconds"],
                f"{path}.retention_seconds",
                minimum=1,
            ),
            optional_string(data["endpoint"], f"{path}.endpoint"),
            bool_value(data["tls_required"], f"{path}.tls_required"),
            optional_string(
                data["credential_reference"], f"{path}.credential_reference"
            ),
        )


__all__ = [
    "SHARED_CACHE_CONFIGURATION_SCHEMA",
    "SHARED_CACHE_NAMESPACE_POLICY_SCHEMA",
    "SharedCacheAccessMode",
    "SharedCacheConfiguration",
    "SharedCacheNamespace",
    "SharedCacheNamespacePolicy",
    "SharedCacheScope",
]
