"""Structured performance telemetry for every ``litai`` invocation.

Every top-level command, and every worker dispatched during a release-gate
fan-out, records one JSON-Lines span in operator state, keyed by the invoking
project's object root (``OBJ_DIR``, default ``_build``). This log is diagnostic, not
authority: a read-only or missing build root must never fail the run it is
observing, and the log itself carries no claim about correctness -- only
about how long each step took and what it used.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from literate_ai._cache_lock import _identity_matches
from literate_ai._filesystem import (
    ensure_safe_directory,
    path_is_link_or_reparse,
    require_safe_directory,
    stat_is_link_or_reparse,
)
from literate_ai.adapters.user_paths import UserPathError, resolve_user_paths

PERFORMANCE_SPAN_SCHEMA = "urn:literate-ai:schema:v1:performance-span"
_PERF_SUBDIRECTORY = ("." + "litai", "perf")


def perf_log_directory(build_root: Path) -> Path:
    """The legacy disposable location, retained for reading existing spans."""

    directory = Path(build_root)
    for part in _PERF_SUBDIRECTORY:
        directory = directory / part
    return directory


def performance_log_directories(build_root: Path) -> tuple[Path, ...]:
    """Read legacy logs and the current operator-state namespace for this root."""

    legacy = perf_log_directory(build_root)
    try:
        return (legacy, _state_log_directory(build_root))
    except (OSError, UserPathError, RuntimeError):
        return (legacy,)


def _state_log_directory(build_root: Path) -> Path:
    root = Path(build_root).resolve()
    key = hashlib.sha256(os.path.normcase(str(root)).encode("utf-8")).hexdigest()
    configured_state = Path(resolve_user_paths().state_root)
    if path_is_link_or_reparse(configured_state):
        raise OSError("performance state root cannot be redirected")
    # Bind system ancestor aliases (such as macOS /var) before custody checks.
    # Keep the configured leaf uncollapsed so a leaf link remains a refusal.
    state = configured_state.parent.resolve() / configured_state.name
    directory = state / "performance" / key
    # An explicitly redirected state root must not reintroduce object-root writes.
    if directory.resolve().is_relative_to(root.resolve()):
        raise OSError("performance state overlaps the object root")
    return directory


def _default_build_root() -> Path:
    configured = os.environ.get("OBJ_DIR", "").strip()
    return Path(configured) if configured else Path("_build")


@dataclass(frozen=True, slots=True)
class PerformanceSpan:
    """One timed step: what it was, how long it took, and what it used."""

    run_id: str
    stage: str
    target_kind: str
    target_id: str
    coding_cli: str | None
    model: str | None
    started_at: str
    ended_at: str
    duration_ms: int
    ok: bool
    error_code: str | None
    extra: dict[str, object]
    schema: str = PERFORMANCE_SPAN_SCHEMA

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "run_id": self.run_id,
            "stage": self.stage,
            "target_kind": self.target_kind,
            "target_id": self.target_id,
            "coding_cli": self.coding_cli,
            "model": self.model,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_ms": self.duration_ms,
            "ok": self.ok,
            "error_code": self.error_code,
            "extra": self.extra,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> PerformanceSpan:
        return cls(
            schema=str(value.get("schema", PERFORMANCE_SPAN_SCHEMA)),
            run_id=str(value["run_id"]),
            stage=str(value["stage"]),
            target_kind=str(value["target_kind"]),
            target_id=str(value["target_id"]),
            coding_cli=(
                None if value.get("coding_cli") is None else str(value["coding_cli"])
            ),
            model=None if value.get("model") is None else str(value["model"]),
            started_at=str(value["started_at"]),
            ended_at=str(value["ended_at"]),
            duration_ms=int(value["duration_ms"]),
            ok=bool(value["ok"]),
            error_code=(
                None if value.get("error_code") is None else str(value["error_code"])
            ),
            extra=dict(value.get("extra") or {}),
        )


class PerformanceRecorder:
    """Append one JSON-Lines span record per completed timed step."""

    def __init__(
        self,
        *,
        build_root: Path | None = None,
        run_id: str | None = None,
        project_root: Path | None = None,
    ) -> None:
        self.build_root = Path(build_root or _default_build_root()).absolute()
        self.run_id = run_id or uuid.uuid4().hex
        self.project_root = (
            None if project_root is None else Path(project_root).absolute()
        )
        try:
            if (
                re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", self.run_id) is None
                or self.run_id.endswith(".")
                or self.run_id.split(".", 1)[0].lower()
                in {
                    "con",
                    "prn",
                    "aux",
                    "nul",
                    *(f"com{i}" for i in range(1, 10)),
                    *(f"lpt{i}" for i in range(1, 10)),
                }
            ):
                raise OSError("performance run ID is not a portable filename")
            self._directory = _state_log_directory(self.build_root)
        except (OSError, UserPathError, RuntimeError):
            self._directory = None

    @property
    def log_path(self) -> Path:
        if self._directory is None:
            raise OSError("performance state is unavailable")
        return self._directory / f"{self.run_id}.jsonl"

    def _write(self, span: PerformanceSpan) -> None:
        try:
            log_path = self.log_path
            directory = log_path.parent
            if self.project_root is not None and directory.resolve().is_relative_to(
                Path(self.project_root).resolve()
            ):
                return
            ensure_safe_directory(directory)
            if log_path.exists() or log_path.is_symlink():
                prior = log_path.lstat()
                if stat_is_link_or_reparse(prior) or not stat.S_ISREG(prior.st_mode):
                    return
            flags = (
                os.O_WRONLY
                | os.O_APPEND
                | os.O_CREAT
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0)
            )
            descriptor = os.open(log_path, flags, 0o600)
            with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
                observed = os.fstat(handle.fileno())
                current = log_path.lstat()
                require_safe_directory(directory)
                if (
                    stat_is_link_or_reparse(current)
                    or not stat.S_ISREG(observed.st_mode)
                    or observed.st_nlink != 1
                    or not _identity_matches(
                        log_path, current, handle.fileno(), observed
                    )
                ):
                    return
                handle.write(json.dumps(span.to_dict(), sort_keys=True) + "\n")
        except (OSError, UserPathError, RuntimeError):
            pass

    @contextmanager
    def span(
        self,
        stage: str,
        *,
        target_kind: str,
        target_id: str,
        coding_cli: str | None = None,
        model: str | None = None,
        extra: dict[str, object] | None = None,
    ) -> Iterator[dict[str, object]]:
        """Time one step; the yielded dict may be updated before it ends.

        A caller that only learns which coding CLI or model was actually used
        partway through (or after) the operation can set
        ``outcome["coding_cli"]``/``outcome["model"]`` inside the ``with``
        block; those values win over the ones passed at span creation.
        """

        started_monotonic = time.monotonic()
        started_at = datetime.now(UTC).isoformat()
        outcome: dict[str, object] = {
            "ok": True,
            "error_code": None,
            "coding_cli": coding_cli,
            "model": model,
        }
        try:
            yield outcome
        except BaseException as exc:
            outcome["ok"] = False
            outcome["error_code"] = getattr(exc, "code", type(exc).__name__)
            raise
        finally:
            duration_ms = round((time.monotonic() - started_monotonic) * 1000)
            recorded_cli = outcome.get("coding_cli")
            recorded_model = outcome.get("model")
            self._write(
                PerformanceSpan(
                    run_id=self.run_id,
                    stage=stage,
                    target_kind=target_kind,
                    target_id=target_id,
                    coding_cli=None if recorded_cli is None else str(recorded_cli),
                    model=None if recorded_model is None else str(recorded_model),
                    started_at=started_at,
                    ended_at=datetime.now(UTC).isoformat(),
                    duration_ms=duration_ms,
                    ok=bool(outcome["ok"]),
                    error_code=(
                        None
                        if outcome["error_code"] is None
                        else str(outcome["error_code"])
                    ),
                    extra=dict(extra or {}),
                )
            )


def read_performance_spans_from_directory(directory: Path) -> list[PerformanceSpan]:
    """Load every recorded span from one exact directory of ``*.jsonl`` logs.

    Unlike :func:`read_performance_spans`, ``directory`` is the literal location
    of the span files themselves -- no ``.litai/perf`` nesting is assumed. This
    is what lets an archived or relocated copy of a project's perf logs (moved
    out from under a disposable build root before it is cleaned) still be
    reported on directly.
    """

    directory = Path(directory)
    if not directory.is_dir():
        return []
    spans: list[PerformanceSpan] = []
    for log_file in sorted(directory.glob("*.jsonl")):
        try:
            lines = log_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                spans.append(PerformanceSpan.from_dict(json.loads(stripped)))
            except (json.JSONDecodeError, KeyError, ValueError, TypeError):
                continue
    return spans


def read_performance_spans(build_root: Path) -> list[PerformanceSpan]:
    """Load legacy and operator-state spans for one selected object root."""

    return [
        span
        for directory in performance_log_directories(build_root)
        for span in read_performance_spans_from_directory(directory)
    ]


__all__ = [
    "PERFORMANCE_SPAN_SCHEMA",
    "PerformanceRecorder",
    "PerformanceSpan",
    "perf_log_directory",
    "performance_log_directories",
    "read_performance_spans",
    "read_performance_spans_from_directory",
]
