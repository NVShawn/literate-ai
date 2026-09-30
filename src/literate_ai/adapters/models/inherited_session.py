"""Authenticated no-subprocess adapter for the already-running IDE session."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import secrets
import shutil
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Protocol

from literate_ai._filesystem import path_is_link_or_reparse
from literate_ai.adapters.dependencies import validate_cyclonedx_bom
from literate_ai.adapters.intelligence import generated_source_tree_identity
from literate_ai.adapters.models.coding_cli import (
    CodingCliGeneration,
    CodingCliIsolation,
    CodingCliSourceGenerator,
    GenerationRecipe,
    _acceptance_argument_vectors,
    _acceptance_result_shape,
    _canonicalize_generated_framework_metadata,
    _planned_generation_prompt,
    _requested_route_digests,
    _requested_stage_ids,
    _require_recipe_authority_sbom,
)
from literate_ai.contracts import CYCLONEDX_SOURCE_SBOM_PATH, CycloneDxLifecycle
from literate_ai.contracts.identity import (
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.contracts.inherited_session import (
    InheritedSessionContextBundle,
    InheritedSessionHandoffEvidence,
    InheritedSessionOutcome,
    InheritedSessionRequest,
    InheritedSessionResponse,
)
from literate_ai.generated_tests import (
    GENERATED_TEST_SUITE_PATH,
    validate_generated_test_suite,
)

if TYPE_CHECKING:
    from literate_ai.application import GenerationExecutionPlan

INHERITED_SESSION_PROVIDER = "inherited-session"
CODING_PROVIDER_ENVIRONMENT = "LITAI_CODING_PROVIDER"
INHERITED_SESSION_DIRECTORY_REQUEST_SCHEMA = (
    "urn:literate-ai:schema:v1:inherited-session-directory-request"
)
INHERITED_SESSION_DIRECTORY_RESPONSE_SCHEMA = (
    "urn:literate-ai:schema:v1:inherited-session-directory-response"
)
INHERITED_SESSION_SOURCE_PAYLOAD_SCHEMA = (
    "urn:literate-ai:schema:v1:inherited-session-source-payload"
)


class InheritedSessionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class InheritedSessionCustody(StrEnum):
    READY = "runner-owned"
    SESSION = "session-owned"
    TERMINAL = "terminal"


class InheritedSessionCancellation(StrEnum):
    USER = "user"
    RUNNER = "runner"


@dataclass(frozen=True, slots=True)
class InheritedSessionProviderConfig:
    provider_identity: ContentIdentity
    session_identity: ContentIdentity
    authentication_key_id: str
    timeout_seconds: float = 900.0

    def __post_init__(self) -> None:
        if not isinstance(self.provider_identity, ContentIdentity):
            raise TypeError("provider_identity must be a ContentIdentity")
        if not isinstance(self.session_identity, ContentIdentity):
            raise TypeError("session_identity must be a ContentIdentity")
        if not self.authentication_key_id.strip():
            raise ValueError("authentication_key_id must not be empty")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

    @property
    def authentication_key_identity(self) -> ContentIdentity:
        return canonical_identity(
            {
                "schema": "literate-ai/inherited-session-authentication-key@1",
                "key_id": self.authentication_key_id,
            }
        )

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> InheritedSessionProviderConfig:
        configured = os.environ if environment is None else environment
        if configured.get(CODING_PROVIDER_ENVIRONMENT, "").strip() != (
            INHERITED_SESSION_PROVIDER
        ):
            raise InheritedSessionError(
                "inherited_session.not_selected",
                f"{CODING_PROVIDER_ENVIRONMENT} must select inherited-session",
            )
        try:
            provider = ContentIdentity.parse_uri(
                configured["LITAI_INHERITED_SESSION_PROVIDER_IDENTITY"].strip()
            )
            session = ContentIdentity.parse_uri(
                configured["LITAI_INHERITED_SESSION_IDENTITY"].strip()
            )
            key_id = configured["LITAI_INHERITED_SESSION_AUTH_KEY_ID"].strip()
            timeout = float(
                configured.get("LITAI_INHERITED_SESSION_TIMEOUT_SECONDS", "900")
            )
            return cls(provider, session, key_id, timeout)
        except (KeyError, TypeError, ValueError) as exc:
            raise InheritedSessionError(
                "inherited_session.configuration_invalid",
                "inherited-session selection requires canonical provider/session "
                "identities, an authentication key ID, and a positive timeout",
            ) from exc


def selected_coding_provider(
    environment: Mapping[str, str] | None = None,
) -> str:
    """Select the provider kind without probing or starting any process."""

    configured = os.environ if environment is None else environment
    selected = configured.get(CODING_PROVIDER_ENVIRONMENT, "").strip()
    if not selected:
        return "coding-cli"
    if selected not in {"coding-cli", INHERITED_SESSION_PROVIDER}:
        raise InheritedSessionError(
            "coding_provider.unsupported",
            f"{CODING_PROVIDER_ENVIRONMENT} must be coding-cli or inherited-session",
        )
    return selected


@dataclass(frozen=True, slots=True)
class InheritedSessionDelivery:
    response_json: bytes
    source_payload: bytes | None
    authentication_tag: str


class InheritedSessionTransport(Protocol):
    """Downstream-owned authenticated channel to the current IDE session."""

    def exchange(
        self,
        request_json: bytes,
        context_bundle_json: bytes,
        request_authentication_tag: str,
        *,
        timeout_seconds: float,
        cancelled: Callable[[], bool],
    ) -> InheritedSessionDelivery: ...


class DirectoryInheritedSessionTransport:
    """One-request authenticated filesystem rendezvous for an IDE coordinator.

    The directory is an ephemeral channel, not durable evidence.  It must already
    exist, be private to the current user on POSIX, and be empty.  Request and response
    envelopes are create-once regular files, so disconnects, stale state, replacement,
    and replay all fail closed.
    """

    REQUEST_NAME = "request.json"
    RESPONSE_NAME = "response.json"
    DISCONNECT_NAME = "disconnect"
    MAXIMUM_RESPONSE_BYTES = 32 * 1024 * 1024

    def __init__(
        self,
        directory: Path,
        *,
        poll_interval_seconds: float = 0.05,
    ) -> None:
        self.channel_root = Path(directory).resolve(strict=True)
        if (
            not self.channel_root.is_dir()
            or path_is_link_or_reparse(self.channel_root)
            or poll_interval_seconds <= 0
        ):
            raise InheritedSessionError(
                "inherited_session.channel_invalid",
                "exchange channel must be a real directory with a positive "
                "poll interval",
            )
        if os.name != "nt" and self.channel_root.stat().st_mode & 0o077:
            raise InheritedSessionError(
                "inherited_session.channel_permissions",
                "exchange channel must not grant group or other permissions",
            )
        transaction = self.channel_root / f"exchange-{secrets.token_hex(16)}"
        try:
            transaction.mkdir(mode=0o700)
        except OSError as exc:
            raise InheritedSessionError(
                "inherited_session.channel_transaction_failed",
                "a fresh exchange transaction could not be created exclusively",
            ) from exc
        self.directory = transaction.resolve(strict=True)
        self.poll_interval_seconds = poll_interval_seconds

    def exchange(
        self,
        request_json: bytes,
        context_bundle_json: bytes,
        request_authentication_tag: str,
        *,
        timeout_seconds: float,
        cancelled: Callable[[], bool],
    ) -> InheritedSessionDelivery:
        request_path = self.directory / self.REQUEST_NAME
        response_path = self.directory / self.RESPONSE_NAME
        disconnect_path = self.directory / self.DISCONNECT_NAME
        _write_exclusive_private(
            request_path,
            canonical_json_bytes(
                {
                    "schema": INHERITED_SESSION_DIRECTORY_REQUEST_SCHEMA,
                    "request": base64.b64encode(request_json).decode("ascii"),
                    "context_bundle": base64.b64encode(context_bundle_json).decode(
                        "ascii"
                    ),
                    "authentication_tag": request_authentication_tag,
                }
            ),
        )
        deadline = time.monotonic() + timeout_seconds
        while True:
            if cancelled():
                raise InheritedSessionError(
                    "inherited_session.cancelled",
                    "inherited-session exchange was cancelled",
                )
            if disconnect_path.exists():
                _require_private_regular(disconnect_path, maximum_bytes=0)
                raise InheritedSessionError(
                    "inherited_session.disconnected",
                    "IDE coordinator disconnected before returning a response",
                )
            if response_path.exists():
                encoded = _read_private_regular(
                    response_path, maximum_bytes=self.MAXIMUM_RESPONSE_BYTES
                )
                try:
                    envelope = json.loads(encoded)
                    if (
                        not isinstance(envelope, dict)
                        or set(envelope)
                        != {
                            "schema",
                            "response",
                            "source_payload",
                            "authentication_tag",
                        }
                        or envelope["schema"]
                        != INHERITED_SESSION_DIRECTORY_RESPONSE_SCHEMA
                        or not isinstance(envelope["response"], str)
                        or (
                            envelope["source_payload"] is not None
                            and not isinstance(envelope["source_payload"], str)
                        )
                        or not isinstance(envelope["authentication_tag"], str)
                        or canonical_json_bytes(envelope) != encoded
                    ):
                        raise ValueError
                    response_json = base64.b64decode(
                        envelope["response"], validate=True
                    )
                    source_payload = (
                        None
                        if envelope["source_payload"] is None
                        else base64.b64decode(envelope["source_payload"], validate=True)
                    )
                except (ValueError, TypeError, json.JSONDecodeError) as exc:
                    raise InheritedSessionError(
                        "inherited_session.channel_response_invalid",
                        "IDE coordinator response envelope is malformed",
                    ) from exc
                return InheritedSessionDelivery(
                    response_json,
                    source_payload,
                    envelope["authentication_tag"],
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            time.sleep(min(self.poll_interval_seconds, remaining))

    def cleanup(self) -> None:
        """Remove one terminal transaction without touching sibling requests."""

        if self.directory.parent != self.channel_root:
            raise InheritedSessionError(
                "inherited_session.cleanup_boundary_invalid",
                "transaction directory escaped its channel root",
            )
        shutil.rmtree(self.directory, ignore_errors=False)


@dataclass(frozen=True, slots=True)
class InheritedSessionReceivedRequest:
    request: InheritedSessionRequest
    context_bundle: InheritedSessionContextBundle


class DirectoryInheritedSessionCoordinator:
    """IDE-side peer for the canonical one-request directory protocol."""

    def __init__(
        self,
        directory: Path,
        config: InheritedSessionProviderConfig,
        *,
        secret: bytes,
    ) -> None:
        self.directory = Path(directory).resolve(strict=True)
        self.config = config
        self._secret = bytearray(secret)
        if len(self._secret) < 32:
            _clear(self._secret)
            raise InheritedSessionError(
                "inherited_session.authentication_key_invalid",
                "ephemeral handoff authentication key must contain at least 32 bytes",
            )
        self.request: InheritedSessionRequest | None = None
        self.context_bundle: InheritedSessionContextBundle | None = None

    @classmethod
    def discover(cls, channel_root: Path) -> tuple[Path, ...]:
        """Return safe pending transaction directories in deterministic order."""

        root = Path(channel_root).resolve(strict=True)
        if (
            not root.is_dir()
            or path_is_link_or_reparse(root)
            or (os.name != "nt" and root.stat().st_mode & 0o077)
        ):
            raise InheritedSessionError(
                "inherited_session.channel_invalid",
                "exchange channel root is not a private real directory",
            )
        pending = []
        for path in sorted(root.glob("exchange-*")):
            if (
                path.is_dir()
                and not path_is_link_or_reparse(path)
                and (os.name == "nt" or not path.stat().st_mode & 0o077)
                and (path / DirectoryInheritedSessionTransport.REQUEST_NAME).is_file()
                and not (
                    path / DirectoryInheritedSessionTransport.RESPONSE_NAME
                ).exists()
            ):
                pending.append(path)
        return tuple(pending)

    def receive(self) -> InheritedSessionReceivedRequest:
        if self.request is not None:
            raise InheritedSessionError(
                "inherited_session.duplicate_request",
                "coordinator transaction request was already consumed",
            )
        encoded = _read_private_regular(
            self.directory / DirectoryInheritedSessionTransport.REQUEST_NAME,
            maximum_bytes=1024 * 1024,
        )
        try:
            envelope = json.loads(encoded)
            if (
                not isinstance(envelope, dict)
                or set(envelope)
                != {"schema", "request", "context_bundle", "authentication_tag"}
                or envelope["schema"] != INHERITED_SESSION_DIRECTORY_REQUEST_SCHEMA
                or not isinstance(envelope["request"], str)
                or not isinstance(envelope["context_bundle"], str)
                or not isinstance(envelope["authentication_tag"], str)
                or canonical_json_bytes(envelope) != encoded
            ):
                raise ValueError
            request_json = base64.b64decode(envelope["request"], validate=True)
            context_bundle_json = base64.b64decode(
                envelope["context_bundle"], validate=True
            )
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise InheritedSessionError(
                "inherited_session.channel_request_invalid",
                "runner request envelope is malformed",
            ) from exc
        if not hmac.compare_digest(
            envelope["authentication_tag"],
            _authentication_tag(
                self._secret,
                _request_authentication_payload(request_json, context_bundle_json),
                domain=_REQUEST_AUTHENTICATION_DOMAIN,
            ),
        ):
            raise InheritedSessionError(
                "inherited_session.authentication_failed",
                "runner request authentication failed",
            )
        try:
            request = InheritedSessionRequest.from_dict(json.loads(request_json))
            context_bundle = InheritedSessionContextBundle.from_dict(
                json.loads(context_bundle_json)
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise InheritedSessionError(
                "inherited_session.request_invalid",
                "authenticated runner request is not a valid contract",
            ) from exc
        if (
            request.provider_identity != self.config.provider_identity
            or request.session_identity != self.config.session_identity
        ):
            raise InheritedSessionError(
                "inherited_session.request_binding_mismatch",
                "runner request binds another provider or IDE session",
            )
        if (
            request.context_bundle_identity != context_bundle.identity
            or request.context_manifest_identity
            != context_bundle.context_manifest_identity
            or request.prompt_identity != context_bundle.prompt_identity
            or request.writable_boundary_identity
            != context_bundle.writable_boundary_identity
            or request.component_lock_identity != context_bundle.component_lock_identity
            or request.recipe_identity != context_bundle.recipe_identity
            or request.model_binding_identity != context_bundle.model_binding_identity
            or request.output_contract_identity
            != context_bundle.output_contract_identity
        ):
            raise InheritedSessionError(
                "inherited_session.context_binding_mismatch",
                "authenticated context bundle differs from request authority",
            )
        self.request = request
        self.context_bundle = context_bundle
        return InheritedSessionReceivedRequest(request, context_bundle)

    def succeed(
        self,
        files: Mapping[str, str],
        *,
        transcript_identity: ContentIdentity,
        provider_evidence_identity: ContentIdentity,
    ) -> InheritedSessionResponse:
        request = self.request
        context_bundle = self.context_bundle
        if request is None or context_bundle is None:
            raise InheritedSessionError(
                "inherited_session.request_missing",
                "receive and authenticate a request before responding",
            )
        _require_allowed_source_files(files, context_bundle.allowed_output_paths)
        source_payload = canonical_json_bytes(
            {
                "schema": INHERITED_SESSION_SOURCE_PAYLOAD_SCHEMA,
                "files": dict(sorted(files.items())),
            }
        )
        source_tree_identity = ContentIdentity.parse_uri(
            generated_source_tree_identity(
                {path: content.encode("utf-8") for path, content in files.items()}
            )
        )
        response = InheritedSessionResponse(
            request.identity,
            request.provider_identity,
            request.session_identity,
            1,
            InheritedSessionOutcome.SUCCEEDED,
            source_tree_identity,
            ContentIdentity.parse_uri(
                "sha256:" + hashlib.sha256(source_payload).hexdigest()
            ),
            transcript_identity,
            provider_evidence_identity,
        )
        response_json = canonical_json_bytes(response.to_dict())
        _write_exclusive_private(
            self.directory / DirectoryInheritedSessionTransport.RESPONSE_NAME,
            canonical_json_bytes(
                {
                    "schema": INHERITED_SESSION_DIRECTORY_RESPONSE_SCHEMA,
                    "response": base64.b64encode(response_json).decode("ascii"),
                    "source_payload": base64.b64encode(source_payload).decode("ascii"),
                    "authentication_tag": _authentication_tag(
                        self._secret,
                        response_json,
                        domain=_RESPONSE_AUTHENTICATION_DOMAIN,
                    ),
                }
            ),
        )
        _clear(self._secret)
        return response

    def disconnect(self) -> None:
        _write_exclusive_private(
            self.directory / DirectoryInheritedSessionTransport.DISCONNECT_NAME, b""
        )
        _clear(self._secret)


@dataclass(frozen=True, slots=True)
class InheritedSessionHandoffResult:
    evidence: InheritedSessionHandoffEvidence
    source_payload: bytes | None


class InheritedSessionProviderAdapter:
    """Single-owner handoff; this adapter never invokes a nested coding CLI."""

    def __init__(
        self,
        config: InheritedSessionProviderConfig,
        transport: InheritedSessionTransport,
        *,
        secret_provider: Callable[[str], bytes],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not isinstance(config, InheritedSessionProviderConfig):
            raise TypeError("config must be an InheritedSessionProviderConfig")
        if not callable(getattr(transport, "exchange", None)):
            raise TypeError("transport must provide exchange")
        if not callable(secret_provider) or not callable(clock):
            raise TypeError("secret_provider and clock must be callable")
        self.config = config
        self.transport = transport
        self._secret_provider = secret_provider
        self._clock = clock
        self._lock = threading.Lock()
        self._custody = InheritedSessionCustody.READY
        self._cancellation: InheritedSessionCancellation | None = None
        self._request: InheritedSessionRequest | None = None

    @property
    def custody(self) -> InheritedSessionCustody:
        return self._custody

    def cancel(self, owner: InheritedSessionCancellation) -> None:
        if not isinstance(owner, InheritedSessionCancellation):
            raise TypeError("owner must be a typed cancellation owner")
        with self._lock:
            if self._custody is InheritedSessionCustody.TERMINAL:
                raise InheritedSessionError(
                    "inherited_session.already_terminal",
                    "terminal handoff cannot be cancelled or re-owned",
                )
            if self._cancellation is not None and self._cancellation is not owner:
                raise InheritedSessionError(
                    "inherited_session.cancellation_owner_mismatch",
                    "only the first cancellation owner may terminate the handoff",
                )
            self._cancellation = owner

    def execute(
        self,
        *,
        generation_request_identity: ContentIdentity,
        generation_plan_identity: ContentIdentity,
        context_bundle: InheritedSessionContextBundle,
    ) -> InheritedSessionHandoffResult:
        with self._lock:
            if self._custody is not InheritedSessionCustody.READY:
                raise InheritedSessionError(
                    "inherited_session.duplicate_request",
                    "one adapter instance accepts exactly one handoff request",
                )
            now = self._clock().astimezone(UTC)
            if not isinstance(context_bundle, InheritedSessionContextBundle):
                raise InheritedSessionError(
                    "inherited_session.context_bundle_invalid",
                    "handoff requires a typed exact context bundle",
                )
            request = InheritedSessionRequest(
                self.config.provider_identity,
                self.config.session_identity,
                generation_request_identity,
                generation_plan_identity,
                context_bundle.context_manifest_identity,
                context_bundle.prompt_identity,
                context_bundle.writable_boundary_identity,
                context_bundle.component_lock_identity,
                context_bundle.recipe_identity,
                context_bundle.model_binding_identity,
                context_bundle.output_contract_identity,
                context_bundle.identity,
                _timestamp(now),
                _timestamp(now + timedelta(seconds=self.config.timeout_seconds)),
                secrets.token_hex(16),
            )
            self._request = request
            self._custody = InheritedSessionCustody.SESSION
            cancellation = self._cancellation
        if cancellation is not None:
            return self._cancelled(request, cancellation)

        secret = bytearray(self._secret_provider(self.config.authentication_key_id))
        if len(secret) < 32:
            _clear(secret)
            return self._authentication_failure(
                "inherited_session.authentication_key_invalid",
                "ephemeral handoff authentication key must contain at least 32 bytes",
            )
        request_json = canonical_json_bytes(request.to_dict())
        context_bundle_json = canonical_json_bytes(context_bundle.to_dict())
        request_tag = _authentication_tag(
            secret,
            _request_authentication_payload(request_json, context_bundle_json),
            domain=_REQUEST_AUTHENTICATION_DOMAIN,
        )
        try:
            try:
                delivery = self.transport.exchange(
                    request_json,
                    context_bundle_json,
                    request_tag,
                    timeout_seconds=self.config.timeout_seconds,
                    cancelled=lambda: self._cancellation is not None,
                )
            except TimeoutError:
                return self._local_terminal(
                    request,
                    InheritedSessionOutcome.TIMED_OUT,
                    "inherited_session.timeout",
                )
            except InheritedSessionError as exc:
                cancellation = self._cancellation
                if (
                    exc.code == "inherited_session.cancelled"
                    and cancellation is not None
                ):
                    return self._cancelled(request, cancellation)
                self._finish()
                raise
            except Exception:
                self._finish()
                raise
            cancellation = self._cancellation
            if cancellation is not None:
                return self._cancelled(request, cancellation)
            return self.accept_delivery(delivery, secret=secret)
        finally:
            _clear(secret)

    def accept_delivery(
        self,
        delivery: InheritedSessionDelivery,
        *,
        secret: bytes | bytearray,
    ) -> InheritedSessionHandoffResult:
        with self._lock:
            if self._custody is not InheritedSessionCustody.SESSION:
                raise InheritedSessionError(
                    "inherited_session.late_or_duplicate_response",
                    "response arrived after custody was already terminal",
                )
        if not isinstance(delivery, InheritedSessionDelivery):
            return self._authentication_failure(
                "inherited_session.delivery_invalid",
                "transport returned an untyped delivery",
            )
        if not hmac.compare_digest(
            delivery.authentication_tag,
            _authentication_tag(
                secret, delivery.response_json, domain=_RESPONSE_AUTHENTICATION_DOMAIN
            ),
        ):
            return self._authentication_failure(
                "inherited_session.authentication_failed",
                "response authentication failed",
            )
        try:
            response_value = _json_object(delivery.response_json)
            if canonical_json_bytes(response_value) != delivery.response_json:
                raise ValueError("response is not canonical JSON")
            response = InheritedSessionResponse.from_dict(response_value)
        except (TypeError, ValueError) as exc:
            return self._authentication_failure(
                "inherited_session.response_invalid",
                "authenticated response is not a valid inherited-session contract",
                cause=exc,
            )
        request = self._request
        assert request is not None
        if (
            response.request_identity != request.identity
            or response.provider_identity != request.provider_identity
            or response.session_identity != request.session_identity
        ):
            return self._authentication_failure(
                "inherited_session.response_binding_mismatch",
                "response binds another request, provider, or session",
            )
        if self._clock().astimezone(UTC) > _parse_timestamp(request.expires_at):
            return self._local_terminal(
                request,
                InheritedSessionOutcome.TIMED_OUT,
                "inherited_session.stale_response",
            )
        if response.outcome is InheritedSessionOutcome.SUCCEEDED:
            if delivery.source_payload is None:
                return self._authentication_failure(
                    "inherited_session.source_missing",
                    "successful response omitted candidate source payload",
                )
            payload_identity = ContentIdentity.parse_uri(
                "sha256:" + hashlib.sha256(delivery.source_payload).hexdigest()
            )
            if payload_identity != response.source_payload_identity:
                return self._authentication_failure(
                    "inherited_session.source_tampered",
                    "candidate source payload differs from its authenticated digest",
                )
        elif delivery.source_payload is not None:
            return self._authentication_failure(
                "inherited_session.terminal_source_unexpected",
                "non-success response cannot carry source",
            )
        evidence = InheritedSessionHandoffEvidence(
            request,
            response,
            self.config.authentication_key_identity,
        )
        self._finish()
        return InheritedSessionHandoffResult(evidence, delivery.source_payload)

    def _cancelled(
        self,
        request: InheritedSessionRequest,
        owner: InheritedSessionCancellation,
    ) -> InheritedSessionHandoffResult:
        outcome = (
            InheritedSessionOutcome.USER_CANCELLED
            if owner is InheritedSessionCancellation.USER
            else InheritedSessionOutcome.RUNNER_CANCELLED
        )
        return self._local_terminal(
            request,
            outcome,
            f"inherited_session.{outcome.value}",
        )

    def _local_terminal(
        self,
        request: InheritedSessionRequest,
        outcome: InheritedSessionOutcome,
        reason: str,
    ) -> InheritedSessionHandoffResult:
        response = InheritedSessionResponse(
            request.identity,
            request.provider_identity,
            request.session_identity,
            1,
            outcome,
            reason_code=reason,
        )
        evidence = InheritedSessionHandoffEvidence(
            request,
            response,
            self.config.authentication_key_identity,
        )
        self._finish()
        return InheritedSessionHandoffResult(evidence, None)

    def _authentication_failure(
        self,
        code: str,
        message: str,
        *,
        cause: Exception | None = None,
    ) -> InheritedSessionHandoffResult:
        self._finish()
        error = InheritedSessionError(code, message)
        if cause is None:
            raise error
        raise error from cause

    def _finish(self) -> None:
        with self._lock:
            self._custody = InheritedSessionCustody.TERMINAL


@dataclass(frozen=True, slots=True)
class InheritedSessionSelection:
    """Provider selection surface consumed by the existing Standard planner."""

    provider_identity: ContentIdentity
    name: str = INHERITED_SESSION_PROVIDER
    executable: str = ""
    executable_identity: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.provider_identity, ContentIdentity):
            raise TypeError("provider_identity must be a ContentIdentity")
        object.__setattr__(self, "executable_identity", self.provider_identity.uri)

    @property
    def identity(self) -> str:
        return canonical_identity(
            {
                "schema": "literate-ai/inherited-session-selection@1",
                "provider_identity": self.provider_identity.to_dict(),
            }
        ).uri

    @property
    def tool_binding_identity(self) -> str:
        return self.provider_identity.uri

    @property
    def isolation(self) -> CodingCliIsolation:
        return CodingCliIsolation(
            "authenticated-inherited-ide-session-v1",
            False,
            "authenticated-source-payload-only",
            False,
            (
                "IDE provider internals and network egress are not attested",
                "candidate source remains untrusted until verifier-owned admission",
            ),
        )

    def require_unchanged(self) -> None:
        return None

    def to_dict(self) -> dict[str, object]:
        return {
            "coding_provider": self.name,
            "provider_identity": self.provider_identity.uri,
            "isolation": self.isolation.to_dict(),
        }


class InheritedSessionSourceGenerator(CodingCliSourceGenerator):
    """Generate through an authenticated IDE rendezvous without a subprocess."""

    cacheable = False

    def __init__(
        self,
        config: InheritedSessionProviderConfig,
        transport_factory: Callable[[], InheritedSessionTransport],
        *,
        secret_provider: Callable[[str], bytes],
    ) -> None:
        self.config = config
        self.transport_factory = transport_factory
        self.secret_provider = secret_provider
        self.selection = InheritedSessionSelection(config.provider_identity)
        self.source_intelligence_provider = None
        self.source_intelligence_mode = "off"
        self.maximum_generated_files = 1_024
        self.maximum_generated_entries = 4_096
        self.maximum_generated_bytes = 16 * 1024 * 1024
        self.maximum_generated_path_length = 512
        self.maximum_generated_depth = 32
        self._handoffs: dict[str, InheritedSessionHandoffEvidence] = {}

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> InheritedSessionSourceGenerator:
        configured = dict(os.environ if environment is None else environment)
        config = InheritedSessionProviderConfig.from_environment(configured)
        try:
            channel = Path(configured["LITAI_INHERITED_SESSION_CHANNEL"])
            secret_hex = configured["LITAI_INHERITED_SESSION_AUTH_KEY"].strip()
            secret = bytes.fromhex(secret_hex)
        except (KeyError, TypeError, ValueError) as exc:
            from literate_ai.integrations.cursor_inherited_session import (
                require_live_cursor_hook,
            )

            require_live_cursor_hook(environment=configured)
            raise InheritedSessionError(
                "inherited_session.runtime_configuration_invalid",
                "CLI inherited-session use requires a fresh channel directory and "
                "an ephemeral hex authentication key",
            ) from exc
        if len(secret) < 32:
            raise InheritedSessionError(
                "inherited_session.authentication_key_invalid",
                "ephemeral handoff authentication key must contain at least 32 bytes",
            )
        return cls(
            config,
            lambda: DirectoryInheritedSessionTransport(channel),
            secret_provider=lambda key_id: secret,
        )

    def handoff_for(
        self, planned_request_identity: ContentIdentity
    ) -> InheritedSessionHandoffEvidence | None:
        return self._handoffs.get(planned_request_identity.uri)

    def planned_request_identity(
        self,
        recipe: GenerationRecipe,
        *,
        execution_plan: GenerationExecutionPlan,
        stage_request: Mapping[str, object],
        bounded_prompt: bytes | None = None,
    ) -> ContentIdentity:
        return canonical_identity(
            _planned_generation_prompt(
                recipe,
                execution_plan,
                stage_request,
                self.selection,
                bounded_prompt=bounded_prompt,
            )
        )

    def generate(
        self,
        recipe: GenerationRecipe,
        *,
        output_root: Path,
        execution_plan: GenerationExecutionPlan | None = None,
        stage_request: Mapping[str, object] | None = None,
        bounded_prompt: bytes | None = None,
    ) -> CodingCliGeneration:
        if execution_plan is None or stage_request is None or bounded_prompt is None:
            raise InheritedSessionError(
                "inherited_session.exact_invocation_required",
                "inherited-session generation requires a prepared exact node "
                "invocation",
            )
        prior = stage_request.get("prior_stage_outputs")
        plan = prior.get("plan") if isinstance(prior, dict) else None
        if not isinstance(plan, dict):
            raise InheritedSessionError(
                "inherited_session.invocation_binding_missing",
                "prepared invocation omitted inherited-session authority bindings",
            )
        try:
            generation_request_identity = ContentIdentity.parse_uri(
                str(plan["source_generation_request_identity"])
            )
            component_plan_identity = ContentIdentity.parse_uri(
                str(plan["component_generation_plan_identity"])
            )
            context_manifest_identity = ContentIdentity.parse_uri(
                str(plan["context_manifest_identity"])
            )
            workspace_identity = ContentIdentity.parse_uri(
                str(plan["workspace_allocation_identity"])
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise InheritedSessionError(
                "inherited_session.invocation_binding_invalid",
                "prepared invocation contains invalid inherited-session authority",
            ) from exc
        prompt_identity = ContentIdentity.parse_uri(
            "sha256:" + hashlib.sha256(bounded_prompt).hexdigest()
        )
        selected_model = recipe.model_for(self.selection.name)
        if selected_model is None:
            raise InheritedSessionError(
                "inherited_session.model_binding_missing",
                "inherited-session generation requires an explicit bound model",
            )
        model_binding_identity = canonical_identity(
            {
                "schema": "literate-ai/inherited-session-model-binding@1",
                "provider_identity": self.config.provider_identity.to_dict(),
                "model": selected_model,
                "model_scope": (
                    None if recipe.model_scope is None else recipe.model_scope.to_dict()
                ),
            }
        )
        output_contract_identity = canonical_identity(
            {
                "schema": "literate-ai/inherited-session-output-contract@1",
                "allowed_output_paths": ["source/**"],
                "maximum_generated_files": self.maximum_generated_files,
                "maximum_generated_entries": self.maximum_generated_entries,
                "maximum_generated_bytes": self.maximum_generated_bytes,
                "maximum_generated_path_length": self.maximum_generated_path_length,
                "maximum_generated_depth": self.maximum_generated_depth,
            }
        )
        context_bundle = InheritedSessionContextBundle(
            bounded_prompt=bounded_prompt,
            workspace_locator=str(output_root.absolute()),
            context_manifest_identity=context_manifest_identity,
            prompt_identity=prompt_identity,
            writable_boundary_identity=workspace_identity,
            component_lock_identity=recipe.component_lock_identity,
            recipe_identity=ContentIdentity.parse_uri(recipe.identity),
            model_name=selected_model,
            model_binding_identity=model_binding_identity,
            output_contract_identity=output_contract_identity,
        )
        planned_identity = self.planned_request_identity(
            recipe,
            execution_plan=execution_plan,
            stage_request=stage_request,
            bounded_prompt=bounded_prompt,
        )
        transport = self.transport_factory()
        try:
            handoff = InheritedSessionProviderAdapter(
                self.config,
                transport,
                secret_provider=self.secret_provider,
            ).execute(
                generation_request_identity=generation_request_identity,
                generation_plan_identity=component_plan_identity,
                context_bundle=context_bundle,
            )
        finally:
            cleanup = getattr(transport, "cleanup", None)
            if callable(cleanup):
                cleanup()
        if handoff.evidence.response.outcome is not InheritedSessionOutcome.SUCCEEDED:
            reason = handoff.evidence.response.reason_code or "inherited_session.failed"
            raise InheritedSessionError(
                reason, "inherited IDE session did not generate source"
            )
        assert handoff.source_payload is not None
        files = _decode_source_payload(handoff.source_payload)
        root = output_root.resolve()
        if output_root.is_symlink() or (root.exists() and any(root.iterdir())):
            raise InheritedSessionError(
                "inherited_session.output_not_empty",
                "inherited-session output root must be new or empty",
            )
        root.mkdir(parents=True, exist_ok=True)
        _materialize_source_files(root, files)
        collected = self._collect(root, recipe.all_required_entrypoints)
        _canonicalize_generated_framework_metadata(
            root, collected, GENERATED_TEST_SUITE_PATH
        )
        _canonicalize_generated_framework_metadata(
            root, collected, CYCLONEDX_SOURCE_SBOM_PATH
        )
        encoded = {path: content.encode("utf-8") for path, content in collected.items()}
        tree_identity = ContentIdentity.parse_uri(
            generated_source_tree_identity(encoded)
        )
        if handoff.evidence.response.source_tree_identity != tree_identity:
            raise InheritedSessionError(
                "inherited_session.source_tree_mismatch",
                "authenticated response names a different decoded source tree",
            )
        suite_content = collected.get(GENERATED_TEST_SUITE_PATH)
        sbom_content = collected.get(CYCLONEDX_SOURCE_SBOM_PATH)
        if suite_content is None or sbom_content is None:
            raise InheritedSessionError(
                "inherited_session.required_artifact_missing",
                "candidate source omitted its generated tests or source SBOM",
            )
        suite = validate_generated_test_suite(
            suite_content,
            recipe_identity=recipe.identity,
            specification_references=recipe.non_acceptance_document_paths,
            acceptance_arguments=_acceptance_argument_vectors(recipe),
            result_shape=_acceptance_result_shape(recipe),
        )
        if recipe.managed_sbom_graph is None:
            raise InheritedSessionError(
                "inherited_session.sbom_graph_missing",
                "source generation requires the managed Component graph",
            )
        source_sbom = validate_cyclonedx_bom(
            sbom_content.encode("utf-8"),
            lifecycle=CycloneDxLifecycle.SOURCE,
            managed_graph=recipe.managed_sbom_graph,
        )
        _require_recipe_authority_sbom(sbom_content.encode("utf-8"), recipe)
        self._handoffs[planned_identity.uri] = handoff.evidence
        return CodingCliGeneration(
            files=collected,
            coding_cli=self.selection.name,
            executable="",
            model=recipe.model_for(self.selection.name),
            recipe_identity=recipe.identity,
            command=("inherited-session",),
            request_identity=planned_identity.uri,
            execution_plan_identity=execution_plan.identity.uri,
            requested_model_stages=_requested_stage_ids(execution_plan, stage_request),
            requested_route_decision_digests=_requested_route_digests(
                execution_plan, stage_request
            ),
            executable_identity=self.selection.executable_identity,
            command_identity=canonical_identity(("inherited-session",)).uri,
            coding_cli_selection_identity=self.selection.identity,
            isolation_profile=self.selection.isolation.profile,
            hermetic=False,
            environment_keys=(),
            generation_mode=suite.generation_mode,
            generated_test_suite_identity=suite.content_identity,
            source_intelligence=None,
            coding_cli_tool_binding_identity=self.selection.tool_binding_identity,
            source_sbom=source_sbom,
            source_intelligence_status="off",
            provider_evidence_identity=handoff.evidence.identity.uri,
        )


_REQUEST_AUTHENTICATION_DOMAIN = b"\x00"
_RESPONSE_AUTHENTICATION_DOMAIN = b"\x01"


def _authentication_tag(
    secret: bytes | bytearray, payload: bytes, *, domain: bytes
) -> str:
    """Domain-separated HMAC so a request tag can never authenticate as a response.

    Request and response envelopes previously shared one key and construction;
    a reflected request tag would satisfy the response check for the exact same
    secret and payload bytes. The one-byte domain prefix makes that reflection
    fail the HMAC even incidentally, rather than relying only on the envelope
    schema's structural differences to block it.
    """

    return hmac.new(bytes(secret), domain + payload, hashlib.sha256).hexdigest()


def _request_authentication_payload(request_json: bytes, bundle_json: bytes) -> bytes:
    return canonical_json_bytes(
        {
            "request": base64.b64encode(request_json).decode("ascii"),
            "context_bundle": base64.b64encode(bundle_json).decode("ascii"),
        }
    )


def _require_allowed_source_files(
    files: Mapping[str, str], allowed_output_paths: tuple[str, ...]
) -> None:
    if allowed_output_paths != ("source/**",):
        raise InheritedSessionError(
            "inherited_session.output_contract_unsupported",
            "the built-in coordinator supports only source/** output",
        )
    for raw_path in files:
        path = PurePosixPath(raw_path)
        if (
            path.is_absolute()
            or not path.parts
            or path.parts[0] != "source"
            or any(part in {"", ".", ".."} for part in path.parts)
            or "\\" in raw_path
        ):
            raise InheritedSessionError(
                "inherited_session.output_path_not_allowed",
                "coordinator may return only portable source/** files",
            )


def _clear(secret: bytearray) -> None:
    for index in range(len(secret)):
        secret[index] = 0


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _json_object(value: bytes) -> object:
    return json.loads(value)


def _decode_source_payload(payload: bytes) -> dict[str, str]:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InheritedSessionError(
            "inherited_session.source_payload_invalid",
            "source payload must be canonical UTF-8 JSON",
        ) from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "files"}
        or value["schema"] != INHERITED_SESSION_SOURCE_PAYLOAD_SCHEMA
        or not isinstance(value["files"], dict)
        or not value["files"]
        or any(
            not isinstance(path, str) or not isinstance(content, str)
            for path, content in value["files"].items()
        )
        or canonical_json_bytes(value) != payload
    ):
        raise InheritedSessionError(
            "inherited_session.source_payload_invalid",
            "source payload must be a non-empty canonical path-to-text mapping",
        )
    return dict(value["files"])


def _materialize_source_files(root: Path, files: Mapping[str, str]) -> None:
    for raw_path, content in sorted(files.items()):
        path = PurePosixPath(raw_path)
        if (
            path.is_absolute()
            or not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
            or "\\" in raw_path
        ):
            raise InheritedSessionError(
                "inherited_session.source_path_invalid",
                "source payload contains a non-portable or escaping path",
            )
        destination = root.joinpath(*path.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        _write_exclusive_private(destination, content.encode("utf-8"))


def _write_exclusive_private(path: Path, content: bytes) -> None:
    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=path.parent
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        # Publishing a hard link makes the complete inode visible in one operation
        # while retaining O_EXCL semantics: an existing final name is never replaced.
        os.link(temporary, path)
    except OSError as exc:
        raise InheritedSessionError(
            "inherited_session.channel_write_failed",
            "authenticated channel file could not be created exclusively",
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _require_private_regular(path: Path, *, maximum_bytes: int) -> None:
    stat = path.stat(follow_symlinks=False)
    if (
        path_is_link_or_reparse(path)
        or not path.is_file()
        or stat.st_size > maximum_bytes
        or (os.name != "nt" and stat.st_mode & 0o077)
    ):
        raise InheritedSessionError(
            "inherited_session.channel_file_unsafe",
            "authenticated channel file is redirected, oversized, or not private",
        )


def _read_private_regular(path: Path, *, maximum_bytes: int) -> bytes:
    _require_private_regular(path, maximum_bytes=maximum_bytes)
    before = path.stat(follow_symlinks=False)
    content = path.read_bytes()
    after = path.stat(follow_symlinks=False)
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise InheritedSessionError(
            "inherited_session.channel_file_changed",
            "authenticated channel response changed while it was read",
        )
    return content


__all__ = [
    "CODING_PROVIDER_ENVIRONMENT",
    "DirectoryInheritedSessionCoordinator",
    "INHERITED_SESSION_PROVIDER",
    "InheritedSessionCancellation",
    "InheritedSessionCustody",
    "InheritedSessionDelivery",
    "DirectoryInheritedSessionTransport",
    "InheritedSessionError",
    "InheritedSessionHandoffResult",
    "InheritedSessionProviderAdapter",
    "InheritedSessionProviderConfig",
    "InheritedSessionReceivedRequest",
    "InheritedSessionSelection",
    "InheritedSessionSourceGenerator",
    "InheritedSessionTransport",
    "selected_coding_provider",
]
