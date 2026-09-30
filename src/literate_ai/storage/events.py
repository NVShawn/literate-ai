"""Hash-chained append-only lifecycle events."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from literate_ai._cache_lock import CacheLockError, exclusive_cache_lock

from .cas import StorageError, StorageSafetyError, canonical_json_bytes

_SAFE_STREAM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


class EventStoreError(StorageError):
    """An event log is malformed or violates its append-only chain."""


class FileLock(AbstractContextManager["FileLock"]):
    """Advisory cross-process lock for one small metadata store.

    Shares its implementation with ``exclusive_cache_lock`` rather than
    maintaining a second lock backend: a bounded (30s) non-blocking acquire on
    every platform, the placeholder lock byte written only once the lock is
    actually held (writing it first can collide with another holder's
    mandatory Windows byte-range lock), plus the symlink/reparse and
    device/inode TOCTOU checks that backend already carries.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._stack: ExitStack | None = None

    def __enter__(self) -> FileLock:
        stack = ExitStack()
        try:
            stack.enter_context(exclusive_cache_lock(self.path))
        except CacheLockError as exc:
            stack.close()
            raise StorageSafetyError(str(exc)) from exc
        except BaseException:
            stack.close()
            raise
        self._stack = stack
        return self

    def __exit__(self, *args: object) -> None:
        if self._stack is not None:
            self._stack.close()
            self._stack = None


@dataclass(frozen=True, slots=True)
class EventRecord:
    schema_version: int
    event_id: str
    stream: str
    sequence: int
    event_type: str
    occurred_at: str
    previous_digest: str | None
    data: dict[str, Any]
    digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "stream": self.stream,
            "sequence": self.sequence,
            "event_type": self.event_type,
            "occurred_at": self.occurred_at,
            "previous_digest": self.previous_digest,
            "data": self.data,
            "digest": self.digest,
        }


class AppendOnlyEventStore:
    """One interleaved JSONL log with an independent hash chain per stream."""

    def __init__(self, root: str | Path) -> None:
        configured = Path(root).expanduser()
        if configured.is_symlink():
            raise StorageSafetyError("event-store root must not be a symbolic link")
        configured.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root = configured.resolve(strict=True)
        self.path = self.root / "events.jsonl"
        self.lock_path = self.root / ".events.lock"

    def append(
        self,
        stream: str,
        event_type: str,
        data: Mapping[str, Any],
        *,
        occurred_at: datetime | None = None,
    ) -> EventRecord:
        self._validate_name(stream, "stream")
        self._validate_name(event_type, "event type")
        # Canonical round-trip both validates JSON and prevents caller mutation.
        try:
            safe_data = json.loads(canonical_json_bytes(dict(data)))
        except (TypeError, ValueError) as exc:
            raise EventStoreError(
                "event data must be finite JSON-compatible data"
            ) from exc

        with FileLock(self.lock_path):
            events = self._read_validated()
            previous = next(
                (item for item in reversed(events) if item.stream == stream), None
            )
            body = {
                "schema_version": 1,
                "stream": stream,
                "sequence": previous.sequence + 1 if previous else 1,
                "event_type": event_type,
                "occurred_at": (occurred_at or datetime.now(UTC)).isoformat(),
                "previous_digest": previous.digest if previous else None,
                "data": safe_data,
            }
            digest = hashlib.sha256(canonical_json_bytes(body)).hexdigest()
            record = EventRecord(
                **body,
                event_id=f"evt_{digest[:24]}",
                digest=digest,
            )
            line = canonical_json_bytes(record.to_dict()) + b"\n"
            if self.path.is_symlink():
                raise StorageSafetyError("event log must not be a symbolic link")
            descriptor = os.open(
                self.path,
                os.O_WRONLY | os.O_APPEND | os.O_CREAT,
                0o600,
            )
            try:
                written = os.write(descriptor, line)
                if written != len(line):
                    raise EventStoreError("event append was incomplete")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            return record

    def read(
        self,
        stream: str | None = None,
        *,
        after_sequence: int = 0,
    ) -> tuple[EventRecord, ...]:
        if stream is not None:
            self._validate_name(stream, "stream")
        if after_sequence < 0:
            raise ValueError("after_sequence must not be negative")
        if stream is None and after_sequence:
            raise ValueError("after_sequence requires a specific event stream")
        with FileLock(self.lock_path):
            records = self._read_validated()
        return tuple(
            item
            for item in records
            if (stream is None or item.stream == stream)
            and (stream is None or item.sequence > after_sequence)
        )

    def streams(self) -> tuple[str, ...]:
        return tuple(sorted({item.stream for item in self.read()}))

    def _read_validated(self) -> list[EventRecord]:
        if not self.path.exists():
            return []
        if self.path.is_symlink() or not self.path.is_file():
            raise StorageSafetyError("event log must be a regular file")
        records: list[EventRecord] = []
        previous_by_stream: dict[str, EventRecord] = {}
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise EventStoreError("event log is unreadable") from exc
        for line_number, line in enumerate(lines, start=1):
            if not line:
                raise EventStoreError(f"empty event at line {line_number}")
            try:
                raw = json.loads(line)
                record = EventRecord(
                    schema_version=int(raw["schema_version"]),
                    event_id=str(raw["event_id"]),
                    stream=str(raw["stream"]),
                    sequence=int(raw["sequence"]),
                    event_type=str(raw["event_type"]),
                    occurred_at=str(raw["occurred_at"]),
                    previous_digest=(
                        str(raw["previous_digest"])
                        if raw.get("previous_digest") is not None
                        else None
                    ),
                    data=dict(raw["data"]),
                    digest=str(raw["digest"]),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise EventStoreError(f"invalid event at line {line_number}") from exc
            self._validate_record(record, previous_by_stream.get(record.stream))
            previous_by_stream[record.stream] = record
            records.append(record)
        return records

    @classmethod
    def _validate_record(
        cls,
        record: EventRecord,
        previous: EventRecord | None,
    ) -> None:
        if record.schema_version != 1:
            raise EventStoreError("unsupported event schema version")
        cls._validate_name(record.stream, "stream")
        cls._validate_name(record.event_type, "event type")
        expected_sequence = previous.sequence + 1 if previous else 1
        expected_previous = previous.digest if previous else None
        if (
            record.sequence != expected_sequence
            or record.previous_digest != expected_previous
        ):
            raise EventStoreError(f"event chain is broken for stream {record.stream!r}")
        body = {
            "schema_version": record.schema_version,
            "stream": record.stream,
            "sequence": record.sequence,
            "event_type": record.event_type,
            "occurred_at": record.occurred_at,
            "previous_digest": record.previous_digest,
            "data": record.data,
        }
        expected_digest = hashlib.sha256(canonical_json_bytes(body)).hexdigest()
        if (
            record.digest != expected_digest
            or record.event_id != f"evt_{expected_digest[:24]}"
        ):
            raise EventStoreError(
                f"event digest is invalid for stream {record.stream!r}"
            )

    @staticmethod
    def _validate_name(value: str, kind: str) -> None:
        if not _SAFE_STREAM.fullmatch(value):
            raise ValueError(f"{kind} contains unsafe characters")


def iter_event_data(
    events: Iterator[EventRecord], event_type: str
) -> Iterator[dict[str, Any]]:
    for event in events:
        if event.event_type == event_type:
            yield event.data
