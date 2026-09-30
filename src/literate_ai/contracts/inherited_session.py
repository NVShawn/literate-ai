"""Provider-neutral contracts for an authenticated inherited IDE session."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, ClassVar

from ._validation import (
    contract_fields,
    enum_value,
    fail,
    int_value,
    optional_string,
    string_value,
)
from .identity import (
    SCHEMA_PREFIX,
    ContentIdentity,
    canonical_identity,
    contract_identity,
)

INHERITED_SESSION_REQUEST_SCHEMA = f"{SCHEMA_PREFIX}inherited-session-request"
INHERITED_SESSION_RESPONSE_SCHEMA = f"{SCHEMA_PREFIX}inherited-session-response"
INHERITED_SESSION_HANDOFF_EVIDENCE_SCHEMA = (
    f"{SCHEMA_PREFIX}inherited-session-handoff-evidence"
)
INHERITED_SESSION_CONTEXT_BUNDLE_SCHEMA = (
    f"{SCHEMA_PREFIX}inherited-session-context-bundle"
)


def _identity(value: object, path: str) -> ContentIdentity:
    if not isinstance(value, ContentIdentity):
        fail(path, "must be a ContentIdentity")
    return value


def _optional_identity(value: object, path: str) -> ContentIdentity | None:
    if value is None:
        return None
    return _identity(value, path)


class InheritedSessionOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    TIMED_OUT = "timed-out"
    USER_CANCELLED = "user-cancelled"
    RUNNER_CANCELLED = "runner-cancelled"


@dataclass(frozen=True, slots=True)
class InheritedSessionContextBundle:
    """Exact ephemeral generation authority delivered to the session."""

    bounded_prompt: bytes
    workspace_locator: str
    context_manifest_identity: ContentIdentity
    prompt_identity: ContentIdentity
    writable_boundary_identity: ContentIdentity
    component_lock_identity: ContentIdentity
    recipe_identity: ContentIdentity
    model_name: str
    model_binding_identity: ContentIdentity
    output_contract_identity: ContentIdentity
    allowed_output_paths: tuple[str, ...] = ("source/**",)

    SCHEMA: ClassVar[str] = INHERITED_SESSION_CONTEXT_BUNDLE_SCHEMA
    IDENTITY_FIELDS: ClassVar[tuple[str, ...]] = (
        "context_manifest_identity",
        "prompt_identity",
        "writable_boundary_identity",
        "component_lock_identity",
        "recipe_identity",
        "model_binding_identity",
        "output_contract_identity",
    )

    def __post_init__(self) -> None:
        if not isinstance(self.bounded_prompt, bytes) or not self.bounded_prompt:
            fail(
                "InheritedSessionContextBundle.bounded_prompt",
                "must contain exact non-empty bytes",
            )
        string_value(
            self.workspace_locator,
            "InheritedSessionContextBundle.workspace_locator",
            max_length=4096,
        )
        for name in self.IDENTITY_FIELDS:
            _identity(getattr(self, name), f"InheritedSessionContextBundle.{name}")
        string_value(
            self.model_name,
            "InheritedSessionContextBundle.model_name",
            max_length=256,
        )
        expected_prompt = ContentIdentity.parse_uri(
            "sha256:" + hashlib.sha256(self.bounded_prompt).hexdigest()
        )
        if self.prompt_identity != expected_prompt:
            fail(
                "InheritedSessionContextBundle.prompt_identity",
                "does not identify bounded_prompt",
            )
        if not self.allowed_output_paths:
            fail(
                "InheritedSessionContextBundle.allowed_output_paths",
                "must not be empty",
            )
        for index, raw_path in enumerate(self.allowed_output_paths):
            path = PurePosixPath(
                string_value(
                    raw_path,
                    f"InheritedSessionContextBundle.allowed_output_paths[{index}]",
                    max_length=512,
                )
            )
            if (
                path.is_absolute()
                or not path.parts
                or path.parts[0] != "source"
                or raw_path not in {"source", "source/**"}
            ):
                fail(
                    f"InheritedSessionContextBundle.allowed_output_paths[{index}]",
                    "must be source or source/**",
                )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "bounded_prompt": base64.b64encode(self.bounded_prompt).decode("ascii"),
            "workspace_locator": self.workspace_locator,
            "model_name": self.model_name,
            **{name: getattr(self, name).to_dict() for name in self.IDENTITY_FIELDS},
            "allowed_output_paths": list(self.allowed_output_paths),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "InheritedSessionContextBundle"
    ) -> InheritedSessionContextBundle:
        identity_names = frozenset(cls.IDENTITY_FIELDS)
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=identity_names
            | {
                "bounded_prompt",
                "workspace_locator",
                "model_name",
                "allowed_output_paths",
            },
        )
        encoded = string_value(
            data["bounded_prompt"],
            f"{path}.bounded_prompt",
            max_length=64 * 1024 * 1024,
        )
        try:
            bounded_prompt = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            fail(f"{path}.bounded_prompt", "must be canonical base64")
            raise AssertionError from exc
        if base64.b64encode(bounded_prompt).decode("ascii") != encoded:
            fail(f"{path}.bounded_prompt", "must be canonical base64")
        outputs = data["allowed_output_paths"]
        if not isinstance(outputs, list):
            fail(f"{path}.allowed_output_paths", "must be a list")
        return cls(
            bounded_prompt=bounded_prompt,
            workspace_locator=string_value(
                data["workspace_locator"], f"{path}.workspace_locator", max_length=4096
            ),
            model_name=string_value(
                data["model_name"], f"{path}.model_name", max_length=256
            ),
            **{
                name: ContentIdentity.from_dict(data[name], path=f"{path}.{name}")
                for name in identity_names
            },
            allowed_output_paths=tuple(outputs),
        )


@dataclass(frozen=True, slots=True)
class InheritedSessionRequest:
    """Exact public request; prompt bytes and authentication secrets are excluded."""

    provider_identity: ContentIdentity
    session_identity: ContentIdentity
    generation_request_identity: ContentIdentity
    generation_plan_identity: ContentIdentity
    context_manifest_identity: ContentIdentity
    prompt_identity: ContentIdentity
    writable_boundary_identity: ContentIdentity
    component_lock_identity: ContentIdentity
    recipe_identity: ContentIdentity
    model_binding_identity: ContentIdentity
    output_contract_identity: ContentIdentity
    context_bundle_identity: ContentIdentity
    issued_at: str
    expires_at: str
    nonce: str

    SCHEMA: ClassVar[str] = INHERITED_SESSION_REQUEST_SCHEMA
    IDENTITY_FIELDS: ClassVar[tuple[str, ...]] = (
        "provider_identity",
        "session_identity",
        "generation_request_identity",
        "generation_plan_identity",
        "context_manifest_identity",
        "prompt_identity",
        "writable_boundary_identity",
        "component_lock_identity",
        "recipe_identity",
        "model_binding_identity",
        "output_contract_identity",
        "context_bundle_identity",
    )

    def __post_init__(self) -> None:
        for name in self.IDENTITY_FIELDS:
            _identity(getattr(self, name), f"InheritedSessionRequest.{name}")
        for name in ("issued_at", "expires_at", "nonce"):
            string_value(
                getattr(self, name),
                f"InheritedSessionRequest.{name}",
                max_length=128,
            )
        if self.expires_at <= self.issued_at:
            fail(
                "InheritedSessionRequest.expires_at",
                "must be later than issued_at",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            **{name: getattr(self, name).to_dict() for name in self.IDENTITY_FIELDS},
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "nonce": self.nonce,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "InheritedSessionRequest"
    ) -> InheritedSessionRequest:
        names = frozenset(cls.IDENTITY_FIELDS)
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=names | {"issued_at", "expires_at", "nonce"},
        )
        return cls(
            **{
                name: ContentIdentity.from_dict(data[name], path=f"{path}.{name}")
                for name in names
            },
            issued_at=string_value(data["issued_at"], f"{path}.issued_at"),
            expires_at=string_value(data["expires_at"], f"{path}.expires_at"),
            nonce=string_value(data["nonce"], f"{path}.nonce", max_length=128),
        )


@dataclass(frozen=True, slots=True)
class InheritedSessionResponse:
    request_identity: ContentIdentity
    provider_identity: ContentIdentity
    session_identity: ContentIdentity
    sequence: int
    outcome: InheritedSessionOutcome
    source_tree_identity: ContentIdentity | None = None
    source_payload_identity: ContentIdentity | None = None
    transcript_identity: ContentIdentity | None = None
    provider_evidence_identity: ContentIdentity | None = None
    reason_code: str | None = None

    SCHEMA: ClassVar[str] = INHERITED_SESSION_RESPONSE_SCHEMA

    def __post_init__(self) -> None:
        for name in ("request_identity", "provider_identity", "session_identity"):
            _identity(getattr(self, name), f"InheritedSessionResponse.{name}")
        int_value(
            self.sequence,
            "InheritedSessionResponse.sequence",
            minimum=1,
            maximum=1,
        )
        if not isinstance(self.outcome, InheritedSessionOutcome):
            fail("InheritedSessionResponse.outcome", "must be a typed outcome")
        evidence = (
            self.source_tree_identity,
            self.source_payload_identity,
            self.transcript_identity,
            self.provider_evidence_identity,
        )
        for index, item in enumerate(evidence):
            _optional_identity(item, f"InheritedSessionResponse.evidence[{index}]")
        optional_string(self.reason_code, "InheritedSessionResponse.reason_code")
        if self.outcome is InheritedSessionOutcome.SUCCEEDED:
            if any(item is None for item in evidence) or self.reason_code is not None:
                fail(
                    "InheritedSessionResponse",
                    "success requires every digest and no failure reason",
                )
        elif any(item is not None for item in evidence) or self.reason_code is None:
            fail(
                "InheritedSessionResponse",
                "non-success requires only a typed reason code",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "schema": self.SCHEMA,
            "request_identity": self.request_identity.to_dict(),
            "provider_identity": self.provider_identity.to_dict(),
            "session_identity": self.session_identity.to_dict(),
            "sequence": self.sequence,
            "outcome": self.outcome.value,
        }
        for name in (
            "source_tree_identity",
            "source_payload_identity",
            "transcript_identity",
            "provider_evidence_identity",
        ):
            item = getattr(self, name)
            if item is not None:
                value[name] = item.to_dict()
        if self.reason_code is not None:
            value["reason_code"] = self.reason_code
        return value

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "InheritedSessionResponse"
    ) -> InheritedSessionResponse:
        optional = frozenset(
            {
                "source_tree_identity",
                "source_payload_identity",
                "transcript_identity",
                "provider_evidence_identity",
                "reason_code",
            }
        )
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "request_identity",
                    "provider_identity",
                    "session_identity",
                    "sequence",
                    "outcome",
                }
            ),
            optional=optional,
        )
        return cls(
            ContentIdentity.from_dict(
                data["request_identity"], path=f"{path}.request_identity"
            ),
            ContentIdentity.from_dict(
                data["provider_identity"], path=f"{path}.provider_identity"
            ),
            ContentIdentity.from_dict(
                data["session_identity"], path=f"{path}.session_identity"
            ),
            int_value(data["sequence"], f"{path}.sequence", minimum=1, maximum=1),
            enum_value(InheritedSessionOutcome, data["outcome"], f"{path}.outcome"),
            **{
                name: (
                    ContentIdentity.from_dict(data[name], path=f"{path}.{name}")
                    if name in data
                    else None
                )
                for name in optional
                if name != "reason_code"
            },
            reason_code=optional_string(data.get("reason_code"), f"{path}.reason_code"),
        )


@dataclass(frozen=True, slots=True)
class InheritedSessionHandoffEvidence:
    """Public custody evidence; contains identities, never prompts or credentials."""

    request: InheritedSessionRequest
    response: InheritedSessionResponse
    authentication_key_identity: ContentIdentity

    SCHEMA: ClassVar[str] = INHERITED_SESSION_HANDOFF_EVIDENCE_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.request, InheritedSessionRequest):
            fail("InheritedSessionHandoffEvidence.request", "must be a typed request")
        if not isinstance(self.response, InheritedSessionResponse):
            fail("InheritedSessionHandoffEvidence.response", "must be a typed response")
        _identity(
            self.authentication_key_identity,
            "InheritedSessionHandoffEvidence.authentication_key_identity",
        )
        if (
            self.response.request_identity != self.request.identity
            or self.response.provider_identity != self.request.provider_identity
            or self.response.session_identity != self.request.session_identity
        ):
            fail(
                "InheritedSessionHandoffEvidence.response",
                "does not bind the exact request provider and session",
            )

    @property
    def identity(self) -> ContentIdentity:
        return canonical_identity(self.semantic_dict())

    def semantic_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "request": self.request.to_dict(),
            "response": self.response.to_dict(),
            "authentication_key_identity": self.authentication_key_identity.to_dict(),
        }

    def to_dict(self) -> dict[str, object]:
        return {
            **self.semantic_dict(),
            "evidence_identity": self.identity.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "InheritedSessionHandoffEvidence"
    ) -> InheritedSessionHandoffEvidence:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "request",
                    "response",
                    "authentication_key_identity",
                    "evidence_identity",
                }
            ),
        )
        evidence = cls(
            InheritedSessionRequest.from_dict(data["request"], path=f"{path}.request"),
            InheritedSessionResponse.from_dict(
                data["response"], path=f"{path}.response"
            ),
            ContentIdentity.from_dict(
                data["authentication_key_identity"],
                path=f"{path}.authentication_key_identity",
            ),
        )
        recorded = ContentIdentity.from_dict(
            data["evidence_identity"], path=f"{path}.evidence_identity"
        )
        if recorded != evidence.identity:
            fail(f"{path}.evidence_identity", "does not match handoff evidence")
        return evidence


__all__ = [
    "INHERITED_SESSION_CONTEXT_BUNDLE_SCHEMA",
    "INHERITED_SESSION_HANDOFF_EVIDENCE_SCHEMA",
    "INHERITED_SESSION_REQUEST_SCHEMA",
    "INHERITED_SESSION_RESPONSE_SCHEMA",
    "InheritedSessionHandoffEvidence",
    "InheritedSessionContextBundle",
    "InheritedSessionOutcome",
    "InheritedSessionRequest",
    "InheritedSessionResponse",
]
