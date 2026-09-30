"""Cursor Agent Chat hook adapter for authenticated inherited-session requests."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from literate_ai.adapters.models.inherited_session import (
    DirectoryInheritedSessionCoordinator,
    InheritedSessionError,
    InheritedSessionProviderConfig,
)
from literate_ai.contracts import (
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)

CLAIM_NAME = "cursor-claim.json"
CURSOR_CLAIM_SCHEMA = "literate-ai/cursor-inherited-session-claim@1"
CURSOR_PROVIDER_SCHEMA = "literate-ai/cursor-provider-identity@1"
CURSOR_SESSION_SCHEMA = "literate-ai/cursor-session-identity@1"
CURSOR_PROVIDER_EVIDENCE_SCHEMA = "literate-ai/cursor-provider-evidence@1"
MAXIMUM_TRANSCRIPT_BYTES = 32 * 1024 * 1024
MAXIMUM_SOURCE_BYTES = 16 * 1024 * 1024
MAXIMUM_SOURCE_FILES = 1_024


def cursor_provider_identity(hook: Mapping[str, object]) -> ContentIdentity:
    email = hook.get("user_email")
    if not isinstance(email, str) or not email.strip():
        raise InheritedSessionError(
            "cursor_inherited_session.authenticated_user_missing",
            "Cursor hook omitted its authenticated user identity",
        )
    return canonical_identity(
        {
            "schema": CURSOR_PROVIDER_SCHEMA,
            "vendor": "cursor",
            "authenticated_user": email,
        }
    )


def cursor_session_identity(hook: Mapping[str, object]) -> ContentIdentity:
    conversation = hook.get("conversation_id")
    if not isinstance(conversation, str) or not conversation:
        raise InheritedSessionError(
            "cursor_inherited_session.conversation_missing",
            "Cursor hook omitted its conversation identity",
        )
    return canonical_identity(
        {
            "schema": CURSOR_SESSION_SCHEMA,
            "provider_identity": cursor_provider_identity(hook).to_dict(),
            "conversation_id": conversation,
        }
    )


def require_live_cursor_hook(
    hook: Mapping[str, object] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> Mapping[str, object]:
    """Fail closed unless this process is a Cursor stop-hook invocation."""

    payload: object = hook
    configured = dict(os.environ if environment is None else environment)
    if payload is None:
        raw = configured.get("LITAI_CURSOR_STOP_HOOK", "").strip()
        if raw:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise InheritedSessionError(
                    "inherited_session.current_session_unavailable",
                    "Cursor stop-hook payload is not JSON",
                ) from exc
    if not isinstance(payload, Mapping) or not payload.get("conversation_id"):
        raise InheritedSessionError(
            "inherited_session.current_session_unavailable",
            "no Cursor stop-hook environment is present; a live current-session "
            "transaction cannot be injected from this chat",
        )
    cursor_session_identity(payload)
    return payload


def handle_stop(
    hook: Mapping[str, object],
    *,
    environment: Mapping[str, str] | None = None,
) -> dict[str, object]:
    configured = dict(os.environ if environment is None else environment)
    channel = configured.get("LITAI_INHERITED_SESSION_CHANNEL", "").strip()
    secret_hex = configured.get("LITAI_INHERITED_SESSION_AUTH_KEY", "").strip()
    if not channel and not secret_hex:
        return {}
    try:
        secret = bytes.fromhex(secret_hex)
    except ValueError as exc:
        raise InheritedSessionError(
            "cursor_inherited_session.secret_invalid",
            "Cursor hook requires a hexadecimal ephemeral channel key",
        ) from exc
    if len(secret) < 32:
        raise InheritedSessionError(
            "cursor_inherited_session.secret_invalid",
            "Cursor hook requires at least 256 bits of ephemeral key material",
        )
    config = InheritedSessionProviderConfig.from_environment(configured)
    if config.provider_identity != cursor_provider_identity(
        hook
    ) or config.session_identity != cursor_session_identity(hook):
        raise InheritedSessionError(
            "cursor_inherited_session.session_binding_mismatch",
            "request configuration does not identify this Cursor user/conversation",
        )

    pending = DirectoryInheritedSessionCoordinator.discover(Path(channel))
    if not pending:
        return {}
    transaction = pending[0]
    coordinator = DirectoryInheritedSessionCoordinator(
        transaction, config, secret=secret
    )
    received = coordinator.receive()
    request = received.request
    bundle = received.context_bundle
    _verify_cursor_scope(hook, bundle.workspace_locator, bundle.model_name)

    claim_path = transaction / CLAIM_NAME
    if not claim_path.exists():
        claim = {
            "schema": CURSOR_CLAIM_SCHEMA,
            "request_identity": request.identity.to_dict(),
            "conversation_id": hook["conversation_id"],
            "model": _hook_model(hook),
            "workspace_locator": bundle.workspace_locator,
        }
        _write_exclusive_private(claim_path, canonical_json_bytes(claim))
        prompt = bundle.bounded_prompt.decode("utf-8")
        return {
            "followup_message": (
                "Execute this authenticated Literate AI generation request. "
                f"Use exactly `{bundle.workspace_locator}` as the generation workspace "
                "and write only paths beneath `source/` there. Do not use ambient "
                "conversation context as generation authority. The exact bounded "
                "generation prompt follows between the markers.\n\n"
                "--- BEGIN EXACT BOUNDED PROMPT ---\n"
                f"{prompt}\n"
                "--- END EXACT BOUNDED PROMPT ---"
            )
        }

    claim = _read_claim(claim_path)
    if (
        claim.get("request_identity") != request.identity.to_dict()
        or claim.get("conversation_id") != hook.get("conversation_id")
        or claim.get("model") != _hook_model(hook)
        or claim.get("workspace_locator") != bundle.workspace_locator
    ):
        coordinator.disconnect()
        raise InheritedSessionError(
            "cursor_inherited_session.claim_binding_mismatch",
            "Cursor completion differs from the create-once request claim",
        )
    if hook.get("status") != "completed":
        coordinator.disconnect()
        return {}

    files = _collect_source(Path(bundle.workspace_locator))
    transcript_identity = _transcript_identity(hook)
    provider_evidence_identity = canonical_identity(
        {
            "schema": CURSOR_PROVIDER_EVIDENCE_SCHEMA,
            "request_identity": request.identity.to_dict(),
            "provider_identity": request.provider_identity.to_dict(),
            "session_identity": request.session_identity.to_dict(),
            "conversation_id": hook["conversation_id"],
            "generation_id": hook.get("generation_id"),
            "model": _hook_model(hook),
            "model_params": hook.get("model_params", []),
            "workspace_roots": hook.get("workspace_roots", []),
            "transcript_identity": transcript_identity.to_dict(),
        }
    )
    coordinator.succeed(
        files,
        transcript_identity=transcript_identity,
        provider_evidence_identity=provider_evidence_identity,
    )
    return {}


def _hook_model(hook: Mapping[str, object]) -> str:
    model = hook.get("model_id") or hook.get("model")
    if not isinstance(model, str) or not model:
        raise InheritedSessionError(
            "cursor_inherited_session.model_missing",
            "Cursor hook omitted the current model identity",
        )
    return model


def _verify_cursor_scope(
    hook: Mapping[str, object], workspace_locator: str, model_name: str
) -> None:
    if _hook_model(hook) != model_name:
        raise InheritedSessionError(
            "cursor_inherited_session.model_binding_mismatch",
            "request model differs from the current Cursor model",
        )
    roots = hook.get("workspace_roots")
    if not isinstance(roots, list) or not all(isinstance(item, str) for item in roots):
        raise InheritedSessionError(
            "cursor_inherited_session.workspace_roots_missing",
            "Cursor hook omitted its workspace roots",
        )
    workspace = Path(workspace_locator).resolve(strict=False)
    authorized = False
    for raw_root in roots:
        root = Path(raw_root).resolve(strict=True)
        try:
            workspace.relative_to(root)
            authorized = True
        except ValueError:
            continue
    if not authorized:
        raise InheritedSessionError(
            "cursor_inherited_session.workspace_mismatch",
            "request workspace is outside the current Cursor workspace",
        )


def _collect_source(workspace: Path) -> dict[str, str]:
    source = workspace / "source"
    if not source.is_dir() or source.is_symlink():
        raise InheritedSessionError(
            "cursor_inherited_session.source_missing",
            "completed Cursor turn did not create a real source directory",
        )
    files: dict[str, str] = {}
    total = 0
    for path in sorted(source.rglob("*")):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise InheritedSessionError(
                "cursor_inherited_session.source_link_rejected",
                "generated source cannot contain links",
            )
        if not stat.S_ISREG(info.st_mode):
            continue
        relative = path.relative_to(workspace).as_posix()
        content = path.read_bytes()
        total += len(content)
        if len(files) >= MAXIMUM_SOURCE_FILES or total > MAXIMUM_SOURCE_BYTES:
            raise InheritedSessionError(
                "cursor_inherited_session.source_bounds_exceeded",
                "generated source exceeds the Cursor adapter bounds",
            )
        try:
            files[relative] = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InheritedSessionError(
                "cursor_inherited_session.source_not_utf8",
                "generated source must be UTF-8",
            ) from exc
    if not files:
        raise InheritedSessionError(
            "cursor_inherited_session.source_empty",
            "completed Cursor turn returned no source files",
        )
    return files


def _transcript_identity(hook: Mapping[str, object]) -> ContentIdentity:
    raw_path = hook.get("transcript_path")
    if not isinstance(raw_path, str) or not raw_path:
        raise InheritedSessionError(
            "cursor_inherited_session.transcript_missing",
            "Cursor transcripts must be enabled for inherited-session evidence",
        )
    path = Path(raw_path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAXIMUM_TRANSCRIPT_BYTES:
        raise InheritedSessionError(
            "cursor_inherited_session.transcript_unsafe",
            "Cursor transcript is redirected, missing, or oversized",
        )
    return ContentIdentity.parse_uri(
        "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    )


def _write_exclusive_private(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, content)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_claim(path: Path) -> dict[str, Any]:
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_size > 16 * 1024
        or (os.name != "nt" and info.st_mode & 0o077)
    ):
        raise InheritedSessionError(
            "cursor_inherited_session.claim_unsafe",
            "Cursor request claim is redirected, oversized, or not private",
        )
    value = json.loads(path.read_bytes())
    if (
        not isinstance(value, dict)
        or value.get("schema") != CURSOR_CLAIM_SCHEMA
        or canonical_json_bytes(value) != path.read_bytes()
    ):
        raise InheritedSessionError(
            "cursor_inherited_session.claim_invalid",
            "Cursor request claim is malformed",
        )
    return value


def main() -> int:
    try:
        hook = json.load(sys.stdin)
        if not isinstance(hook, dict):
            raise ValueError("hook input must be an object")
        result = handle_stop(hook)
        sys.stdout.buffer.write(canonical_json_bytes(result) + b"\n")
        return 0
    except (InheritedSessionError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
