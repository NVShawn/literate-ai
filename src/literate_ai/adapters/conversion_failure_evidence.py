"""Finite private adoption failure evidence outside transactional project custody."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any
from uuid import uuid4

from literate_ai._cache_lock import CacheLockError, exclusive_cache_lock
from literate_ai._filesystem import (
    UnsafeFilesystemPathError,
    ensure_safe_directory,
    require_safe_directory,
    stat_is_link_or_reparse,
)
from literate_ai.contracts.identity import canonical_json_bytes
from literate_ai.diagnostics import redact_secrets

from .user_paths import UserPathError, resolve_user_paths

MAX_FAILURE_REPORT_BYTES = 512 * 1024
MAX_FAILURE_REPORTS = 128
MAX_FAILURE_STORE_BYTES = 16 * 1024 * 1024


def _text(value: object, limit: int) -> str:
    return redact_secrets(value if isinstance(value, str) else "")[:limit]


def retain_conversion_failure(
    project_root: Path, error: Exception, *, recovery: str
) -> str:
    """Best-effort diagnostic only; failure never replaces the original error.

    Uses cooperative same-host serialization and existing safe-directory custody.
    Never evicts old evidence. Partial files after interrupted writes count against
    the quota; this is not containment against a hostile same-user process.
    """
    try:
        directory = Path(resolve_user_paths().state_root) / "adoption-failures"
        project = project_root.resolve()
        if directory.is_relative_to(project) or project.is_relative_to(directory):
            return "failure evidence unavailable: state overlaps project"
        path = directory / (uuid4().hex + ".json")
        if len(str(path)) > 320:
            return "failure evidence unavailable: private path exceeds limit"
        report = getattr(error, "report", {})
        phases: list[dict[str, Any]] = []
        if isinstance(report, dict) and isinstance(report.get("phases"), list):
            for phase in report["phases"][:8]:
                if not isinstance(phase, dict):
                    continue
                retained: dict[str, Any] = {"phase": _text(phase.get("phase"), 128)}
                for key in ("stdout_excerpt", "stderr_excerpt"):
                    retained[key] = _text(phase.get(key), 4096)
                for key in ("stdout_identity", "stderr_identity"):
                    retained[key] = _text(phase.get(key), 128)
                for key in ("exit_code", "stdout_size", "stderr_size"):
                    value = phase.get(key)
                    if type(value) is int and abs(value) <= 2**63 - 1:
                        retained[key] = value
                phases.append(retained)
        document = {
            "schema": "literate-ai/conversion-failure@1",
            "code": _text(getattr(error, "code", ""), 128),
            "message": _text(getattr(error, "message", ""), 66048),
            "recovery": recovery
            if recovery in {"rolled-back", "incomplete"}
            else "unknown",
            "phases": phases,
            "coverage": "bounded redacted excerpts/digests; not full streams",
            "acceptance": False,
        }
        content = canonical_json_bytes(document) + b"\n"
        if len(content) > MAX_FAILURE_REPORT_BYTES:
            return "failure evidence unavailable: bounded report exceeds limit"
        ensure_safe_directory(directory)
        node = directory.lstat()
        if os.name != "nt" and (
            node.st_uid != os.getuid() or stat.S_IMODE(node.st_mode) & 0o077
        ):
            return "failure evidence unavailable: private directory permissions"
        with exclusive_cache_lock(directory / ".write.lock", timeout_seconds=1):
            require_safe_directory(directory)
            count, size = 0, 0
            for entry in directory.iterdir():
                if entry.name == ".write.lock":
                    continue
                count += 1
                if count >= MAX_FAILURE_REPORTS:
                    return "failure evidence unavailable: retained store is full"
                existing = entry.lstat()
                if (
                    stat_is_link_or_reparse(existing)
                    or not stat.S_ISREG(existing.st_mode)
                    or existing.st_nlink != 1
                ):
                    return "failure evidence unavailable: retained store is unsafe"
                size += existing.st_size
                if size + len(content) > MAX_FAILURE_STORE_BYTES:
                    return "failure evidence unavailable: retained store is full"
            if size + len(content) > MAX_FAILURE_STORE_BYTES:
                return "failure evidence unavailable: retained store is full"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
            descriptor = os.open(path, flags, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
        return f"failure evidence retained: {path}"
    except (
        OSError,
        ValueError,
        CacheLockError,
        UnsafeFilesystemPathError,
        UserPathError,
    ):
        return "failure evidence unavailable: private retention failed"


def conversion_failure_message(
    project_root: Path, error: Exception, *, recovery: str, outcome: str
) -> str:
    notice = retain_conversion_failure(project_root, error, recovery=recovery)
    message = _text(getattr(error, "message", "conversion failed"), 66048)
    return f"{outcome}; {notice}; {message}"
