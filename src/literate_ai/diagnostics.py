"""Run-scoped, secret-aware CLI diagnostics that preserve stdout contracts."""

from __future__ import annotations

import json
import logging
import os
import shlex
import sys
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

_LOGGER = logging.getLogger("literate_ai")

DEBUG_EVENT_SCHEMA = "literate-ai/debug-event@1"

_VERBOSE: ContextVar[bool] = ContextVar("litai_verbose", default=False)
_STREAM: ContextVar[TextIO | None] = ContextVar("litai_verbose_stream", default=None)
_PROGRESS: ContextVar[TextIO | None] = ContextVar("litai_progress_stream", default=None)
_SINK: ContextVar[StructuredLogSink | None] = ContextVar("litai_log_sink", default=None)
_DEBUG: ContextVar[bool] = ContextVar("litai_debug", default=False)
_DEBUG_STREAM: ContextVar[TextIO | None] = ContextVar(
    "litai_debug_stream", default=None
)
_DEBUG_JSON: ContextVar[bool] = ContextVar("litai_debug_json", default=False)
_DEBUG_LOG_PATH: ContextVar[str | None] = ContextVar(
    "litai_debug_log_path", default=None
)
_SECRET_MARKERS = (
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "password",
    "secret",
    "token",
)
_SECRET_FLAGS = frozenset(
    {
        "--api-key",
        "--authorization",
        "--credential",
        "--key",
        "--password",
        "--secret",
        "--token",
    }
)
# Values shorter than this are ordinary words or path segments that happen to
# sit in a *TOKEN* / *SECRET* environment key. Substituting them would rewrite
# JSON field names such as "root" and evidence roles such as "runtime-root".
_MIN_SECRET_LENGTH = 8


class StructuredLogSink:
    """Thread-safe NDJSON appender for operation and subprocess records."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("a", encoding="utf-8")
        self._lock = threading.Lock()

    def write(self, record: dict[str, object]) -> None:
        line = json.dumps(record, default=str) + "\n"
        with self._lock:
            self._file.write(line)
            self._file.flush()

    def close(self) -> None:
        with self._lock:
            self._file.close()


@contextmanager
def operation_log(path: Path) -> Iterator[None]:
    """Activate a structured NDJSON log sink for the duration of a command."""

    sink = StructuredLogSink(path)
    token = _SINK.set(sink)
    try:
        yield
    finally:
        _SINK.reset(token)
        sink.close()


def emit_log_event(event: str, **ctx: object) -> None:
    """Write one structured event to the NDJSON sink, logger, and progress stream."""

    record = {
        "ts": datetime.now(UTC).isoformat(),
        "event": event,
        **ctx,
    }
    sink = _SINK.get()
    if sink is not None:
        sink.write(record)
    _render_debug_event(record)
    detail = " ".join(
        f"{key}={value!r}" for key, value in ctx.items() if value not in (None, "")
    )
    message = f"{event} {detail}".rstrip()
    if "warning" in event or event.endswith("refused"):
        _LOGGER.warning("%s", message)
        report_progress(f"warning: {message}")
    else:
        _LOGGER.info("%s", message)


def _model_stack_log_fields() -> dict[str, object]:
    try:
        from literate_ai.adapters.model_selection_stack import stack_log_fields
    except ImportError:
        return {}
    return stack_log_fields()


def _publish_operation_record(record: dict[str, object]) -> None:
    sink = _SINK.get()
    if sink is None and not debug_enabled():
        return
    if sink is not None:
        sink.write(record)
    _render_debug_event(record)


@contextmanager
def log_operation(name: str, **ctx: object) -> Iterator[None]:
    """Emit operation.start / operation.end (or operation.error) around a block."""

    started = datetime.now(UTC)
    fields = {**_model_stack_log_fields(), **ctx}
    _publish_operation_record(
        {
            "ts": started.isoformat(),
            "event": "operation.start",
            "operation": name,
            **fields,
        }
    )
    try:
        yield
    except Exception as exc:
        elapsed = round((datetime.now(UTC) - started).total_seconds() * 1000)
        error = _redact_text(str(exc), _secret_values(None))[-4096:]
        _publish_operation_record(
            {
                "ts": datetime.now(UTC).isoformat(),
                "event": "operation.error",
                "operation": name,
                "error": error,
                "duration_ms": elapsed,
            }
        )
        raise
    else:
        elapsed = round((datetime.now(UTC) - started).total_seconds() * 1000)
        _publish_operation_record(
            {
                "ts": datetime.now(UTC).isoformat(),
                "event": "operation.end",
                "operation": name,
                "duration_ms": elapsed,
            }
        )


@contextmanager
def debug_stage(stage: str, **ctx: object) -> Iterator[None]:
    """Emit one named SDLC stage when `--debug` is active; no-op otherwise."""

    with log_operation(stage, stage=stage, **ctx):
        yield


def _flag_environment_enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _environment_enabled() -> bool:
    return _flag_environment_enabled("LITAI_VERBOSE")


def verbose_enabled() -> bool:
    return _VERBOSE.get() or _environment_enabled()


def debug_enabled() -> bool:
    return _DEBUG.get() or _flag_environment_enabled("LITAI_DEBUG")


def _stream_is_tty(stream: TextIO) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, OSError):
        return False


def _render_debug_event(record: Mapping[str, object]) -> None:
    if not debug_enabled():
        return
    stream = _DEBUG_STREAM.get() or sys.stderr
    redacted = {
        key: (
            _redact_text(value, _secret_values(None))
            if isinstance(value, str)
            else value
        )
        for key, value in record.items()
    }
    payload = {"schema": DEBUG_EVENT_SCHEMA, **redacted}
    if _DEBUG_JSON.get() or not _stream_is_tty(stream):
        stream.write(json.dumps(payload, default=str, ensure_ascii=False) + "\n")
        stream.flush()
        return
    event = str(redacted.get("event", "debug"))
    if event.startswith("subprocess."):
        kind = "command"
        stage = event.split(".", 1)[-1]
    else:
        kind = "map" if "spec-map" in event else "stage"
        stage = redacted.get("stage") or redacted.get("operation") or event
    detail_parts = []
    for key, value in redacted.items():
        if key in {"schema", "ts", "event", "stage", "operation"}:
            continue
        if value is None or value == "":
            continue
        if key == "argv" and isinstance(value, (list, tuple)):
            detail_parts.append("argv=" + shlex.join(str(item) for item in value))
            continue
        detail_parts.append(f"{key}={value}")
    suffix = f" {' '.join(str(item) for item in detail_parts)}" if detail_parts else ""
    stream.write(f"[litai:{kind}] {stage}{suffix}\n")
    stream.flush()


@contextmanager
def debug_diagnostics(
    destination: str | None,
    *,
    json_mode: bool,
    stderr: TextIO,
) -> Iterator[None]:
    """Trace SDLC stages and child argv to stderr (`-`) or a truncated FILE."""

    if destination is None and not _flag_environment_enabled("LITAI_DEBUG"):
        yield
        return
    target = destination
    if target is None:
        target = os.environ.get("LITAI_DEBUG_LOG", "").strip() or "-"
    close_file: TextIO | None = None
    log_path: str | None = None
    if target in {"-", ""}:
        stream: TextIO = stderr
        json_out = json_mode or not _stream_is_tty(stderr)
    else:
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        close_file = path.open("w", encoding="utf-8")
        stream = close_file
        json_out = True
        log_path = str(path)
    tokens = (
        _DEBUG.set(True),
        _DEBUG_STREAM.set(stream),
        _DEBUG_JSON.set(json_out),
        _DEBUG_LOG_PATH.set(log_path),
    )
    try:
        yield
    finally:
        _DEBUG.reset(tokens[0])
        _DEBUG_STREAM.reset(tokens[1])
        _DEBUG_JSON.reset(tokens[2])
        _DEBUG_LOG_PATH.reset(tokens[3])
        if close_file is not None:
            close_file.close()


@contextmanager
def verbose_diagnostics(enabled: bool, stream: TextIO | None = None) -> Iterator[None]:
    """Enable diagnostics for this logical command and its in-process children."""

    token = _VERBOSE.set(enabled or _environment_enabled())
    stream_token = _STREAM.set(stream)
    try:
        yield
    finally:
        _STREAM.reset(stream_token)
        _VERBOSE.reset(token)


@contextmanager
def progress_reporting(stream: TextIO | None) -> Iterator[None]:
    """Bind interactive stage output for one command. ``None`` keeps silence."""

    token = _PROGRESS.set(stream)
    try:
        yield
    finally:
        _PROGRESS.reset(token)


def report_progress(message: str) -> None:
    """Write one stage line when an interactive progress stream is bound."""

    stream = _PROGRESS.get()
    if stream is None or not message:
        return
    stream.write(f"Literate AI: {message}\n")
    stream.flush()


def inherited_verbose_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """Copy one child environment and preserve run-scoped verbose/debug diagnostics."""

    inherited = dict(environment)
    if verbose_enabled():
        inherited["LITAI_VERBOSE"] = "1"
    if debug_enabled():
        inherited["LITAI_DEBUG"] = "1"
        if _DEBUG_JSON.get() or _flag_environment_enabled("LITAI_DEBUG_JSON"):
            inherited["LITAI_DEBUG_JSON"] = "1"
        log_path = (
            _DEBUG_LOG_PATH.get() or os.environ.get("LITAI_DEBUG_LOG", "").strip()
        )
        if log_path:
            inherited["LITAI_DEBUG_LOG"] = log_path
    return inherited


def _secret_values(environment: Mapping[str, str] | None) -> tuple[str, ...]:
    values = []
    for key, value in (os.environ if environment is None else environment).items():
        if (
            value
            and len(value) >= _MIN_SECRET_LENGTH
            and any(marker in key.casefold() for marker in _SECRET_MARKERS)
        ):
            values.append(value)
    return tuple(sorted(set(values), key=len, reverse=True))


def _redact_text(value: str, secrets: tuple[str, ...]) -> str:
    redacted = value
    for secret in secrets:
        redacted = redacted.replace(secret, "<redacted>")
    return redacted


def _redact_argv(
    argv: Sequence[str], environment: Mapping[str, str] | None
) -> tuple[str, ...]:
    secrets = _secret_values(environment)
    result: list[str] = []
    redact_next = False
    for raw in argv:
        value = str(raw)
        if redact_next:
            result.append("<redacted>")
            redact_next = False
            continue
        flag, separator, _attached = value.partition("=")
        if flag.casefold() in _SECRET_FLAGS:
            result.append(flag + ("=<redacted>" if separator else ""))
            redact_next = not separator
            continue
        if value.casefold().startswith("bearer "):
            result.append("Bearer <redacted>")
            continue
        result.append(_redact_text(value, secrets))
    return tuple(result)


def redact_argv(
    argv: Sequence[str], environment: Mapping[str, str] | None = None
) -> tuple[str, ...]:
    """Return an argv sequence with credential values replaced."""

    return _redact_argv(argv, environment)


def trace_subprocess(
    argv: Sequence[str],
    *,
    cwd: str | Path,
    environment: Mapping[str, str] | None = None,
    status: int | None = None,
    stdout: str | bytes | None = None,
    stderr: str | bytes | None = None,
    started_at: datetime | None = None,
) -> None:
    """Emit a bounded subprocess event to diagnostic and structured-log sinks."""

    redacted_argv = redact_argv(argv, environment)
    now = datetime.now(UTC)
    if status is None:
        record = {
            "ts": now.isoformat(),
            "event": "subprocess.start",
            "argv": list(redacted_argv),
            "cwd": str(cwd),
        }
    else:
        record = {
            "ts": now.isoformat(),
            "event": "subprocess.exit",
            "argv": list(redacted_argv),
            "cwd": str(cwd),
            "exit_code": status,
        }
        if started_at is not None:
            record["duration_ms"] = round((now - started_at).total_seconds() * 1000)
    _publish_operation_record(record)

    if not verbose_enabled():
        return
    stream = _STREAM.get() or sys.stderr
    secrets = _secret_values(environment)
    command = shlex.join(redacted_argv)
    phase = "start" if status is None else f"exit={status}"
    stream.write(f"[litai:subprocess {phase}] cwd={cwd} argv={command}\n")
    for label, value in (("stdout", stdout), ("stderr", stderr)):
        if value is None or value == b"" or value == "":
            continue
        text = (
            value.decode("utf-8", errors="replace")
            if isinstance(value, bytes)
            else value
        )
        redacted = _redact_text(text, secrets)
        stream.write(f"[litai:subprocess {label}]\n{redacted}")
        if not redacted.endswith("\n"):
            stream.write("\n")
    stream.flush()


def redact_secrets(value: str, environment: Mapping[str, str] | None = None) -> str:
    """Redact one text blob using the same secret-marker convention as subprocess
    tracing.
    """

    return _redact_text(value, _secret_values(environment))


def trace_exception(label: str, cause: BaseException) -> None:
    """Emit one bounded, secret-redacted exception without changing public errors."""

    if not verbose_enabled():
        return
    stream = _STREAM.get() or sys.stderr
    secrets = _secret_values(None)
    detail = _redact_text(str(cause), secrets)[-4096:]
    stream.write(f"[litai:exception] {label}: {type(cause).__name__}: {detail}\n")
    stream.flush()


__all__ = [
    "DEBUG_EVENT_SCHEMA",
    "StructuredLogSink",
    "debug_diagnostics",
    "debug_enabled",
    "debug_stage",
    "inherited_verbose_environment",
    "log_operation",
    "operation_log",
    "progress_reporting",
    "redact_secrets",
    "report_progress",
    "trace_exception",
    "trace_subprocess",
    "verbose_diagnostics",
    "verbose_enabled",
]
