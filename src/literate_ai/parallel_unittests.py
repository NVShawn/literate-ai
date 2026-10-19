"""Process-isolated unittest module execution; the caller owns checkpoints."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from literate_ai.adapters._processes import (
    ProcessTreeOwnership,
    create_process_tree_ownership,
    terminate_process_tree,
)

PROTOCOL = "literate-ai/parallel-unittest@1"
MAX_LOG_BYTES = 16 * 1024 * 1024
MAX_EVENT_BYTES = 256 * 1024
MAX_EVENTS_BYTES = 64 * 1024 * 1024
EVENT_KINDS = frozenset(
    {
        "start",
        "success",
        "skip",
        "expected_failure",
        "failure",
        "error",
        "unexpected_success",
        "subtest_failure",
        "subtest_error",
        "subtest_skip",
        "subtest_end",
        "fixture_skip",
        "fixture_error",
        "done",
    }
)


@dataclass(frozen=True)
class RemoteTest:
    description: str
    order: int

    def id(self) -> str:
        return self.description

    def shortDescription(self) -> None:  # noqa: N802
        return None

    def __str__(self) -> str:
        return self.description


@dataclass(frozen=True)
class ModuleGroup:
    module: str
    indices: tuple[int, ...]
    serial: bool


@dataclass
class RunningModule:
    group: ModuleGroup
    root: Path
    process: subprocess.Popen[bytes]
    ownership: ProcessTreeOwnership
    log: Any
    started: float
    scratch: Path
    offset: int = 0
    sequence: int = 0
    terminal: set[int] = field(default_factory=set)
    begun: set[int] = field(default_factory=set)
    statuses: dict[int, str] = field(default_factory=dict)
    done: bool = False
    failed: bool = False
    invalid: bool = False


def available_workers(value: str | int = "auto") -> int:
    if value == "auto":
        if os.environ.get("LITAI_TEST_WORKER"):
            return 1
        discover = getattr(os, "process_cpu_count", os.cpu_count)
        return max(1, discover() or 1)
    try:
        count = int(value)
    except (ValueError, TypeError) as exc:
        raise ValueError("test workers must be 'auto' or a positive integer") from exc
    if isinstance(value, bool) or count < 1 or str(count) != str(value):
        raise ValueError("test workers must be 'auto' or a positive integer")
    return count


def module_groups(tests: Sequence[unittest.TestCase]) -> tuple[ModuleGroup, ...]:
    grouped: dict[str, list[int]] = {}
    serial: set[str] = set()
    for index, test in enumerate(tests):
        cls = type(test)
        module = cls.__module__
        if module == "__main__" or "<locals>" in cls.__qualname__:
            raise ValueError("parallel tests must be importable; use workers=1")
        if not test.id().startswith(module + "."):
            raise ValueError("parallel test IDs must name importable cases")
        grouped.setdefault(module, []).append(index)
        if getattr(sys.modules.get(module), "LITAI_TEST_SERIAL", False) or getattr(
            cls, "LITAI_TEST_SERIAL", False
        ):
            serial.add(module)
    return tuple(
        ModuleGroup(module, tuple(indices), module in serial)
        for module, indices in grouped.items()
    )


class ParallelSuite:
    """A TextTestRunner-compatible suite using validated worker event streams."""

    def __init__(
        self,
        tests: Sequence[unittest.TestCase],
        *,
        root: Path,
        directory: Path,
        workers: int,
        stream: TextIO,
        verbosity: int,
        outcome: Callable[[int, Mapping[str, object]], None],
        invalidate: Callable[[Sequence[int]], None],
        timeout: float = 3600,
    ) -> None:
        self.tests = tuple(tests)
        self.pending = list(module_groups(tests))
        self.root = root.resolve()
        self.directory = directory / uuid.uuid4().hex[:12]
        self.workers = workers
        self.stream = stream
        self.verbosity = verbosity
        self.outcome = outcome
        self.invalidate = invalidate
        self.timeout = timeout
        self.active: list[RunningModule] = []
        self.failed = False

    def _start(self, group: ModuleGroup) -> RunningModule:
        directory = self.directory / str(group.indices[0])
        directory.mkdir(parents=True)
        # Scratch fixtures must not inherit this checkout's project authority
        # through ancestor discovery. Keep them outside the repository.
        scratch = Path(tempfile.mkdtemp(prefix="litai-pt-"))
        environment = dict(os.environ)
        for key in ("LITAI_EVIDENCE_RUN", "LITAI_EVIDENCE_PARENT"):
            environment.pop(key, None)
        for key, name in (
            ("TMPDIR", "tmp"),
            ("TEMP", "tmp"),
            ("TMP", "tmp"),
            ("OBJ_DIR", "objects"),
            ("BUILD_DIR", "build"),
            ("LITAI_CONFIG_DIR", "config"),
            ("LITAI_STATE_DIR", "state"),
        ):
            path = scratch / name
            path.mkdir(exist_ok=True)
            environment[key] = str(path)
        environment["LITAI_TEST_WORKER"] = str(group.indices[0])
        environment["LITAI_SESSION_ID"] = "pt" + str(group.indices[0])
        environment["PYTHON_TEST_WORKERS"] = "1"
        request = {
            "schema": PROTOCOL,
            "root": str(self.root),
            "scratch": str(scratch),
            "cases": [{"index": i, "id": self.tests[i].id()} for i in group.indices],
        }
        request_path = directory / "request.json"
        request_path.write_text(json.dumps(request), encoding="utf-8")
        ownership = create_process_tree_ownership()
        log = (directory / "output.log").open("wb")
        process = None
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "literate_ai.parallel_unittest_worker",
                    str(request_path),
                ],
                cwd=self.root,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                **ownership.popen_options,
            )
            ownership.bind(process.pid)
        except BaseException:
            if process is not None:
                terminate_process_tree(process, ownership=ownership)
                process.wait(timeout=5)
            log.close()
            ownership.release()
            shutil.rmtree(scratch)
            raise
        return RunningModule(
            group, directory, process, ownership, log, time.monotonic(), scratch
        )

    def _display(self, event: Mapping[str, object], symbol: str) -> None:
        if self.verbosity > 1:
            print(f"{event.get('description', '')} ... {symbol}", file=self.stream)
        elif self.verbosity:
            self.stream.write(symbol)
            self.stream.flush()

    def _event(self, worker: RunningModule, event: object, result: Any) -> None:
        if not isinstance(event, dict) or event.get("schema") != PROTOCOL:
            raise ValueError("invalid worker event schema")
        if (
            type(event.get("sequence")) is not int
            or event["sequence"] != worker.sequence
            or worker.done
        ):
            raise ValueError("worker event sequence is invalid")
        worker.sequence += 1
        kind = event.get("kind")
        if not isinstance(kind, str) or kind not in EVENT_KINDS:
            raise ValueError("unknown worker event kind")
        if kind == "done":
            if not isinstance(event.get("successful"), bool):
                raise ValueError("worker completion lacks its result")
            if event["successful"] and worker.terminal != set(worker.group.indices):
                raise ValueError("worker omitted selected test outcomes")
            if event["successful"] == worker.failed:
                raise ValueError("worker completion contradicts observed outcomes")
            worker.done = True
            return
        indices = event.get("indices")
        if (
            not isinstance(indices, list)
            or not indices
            or any(type(i) is not int or i not in worker.group.indices for i in indices)
            or len(set(indices)) != len(indices)
        ):
            raise ValueError("worker event names an unselected test")
        if not isinstance(event.get("description"), str):
            raise ValueError("worker event lacks a test description")
        if kind == "start":
            if len(indices) != 1 or indices[0] in worker.begun:
                raise ValueError("worker started a test twice")
            worker.begun.add(indices[0])
            result.testsRun += 1
            return
        fixture = kind in {"fixture_skip", "fixture_error"}
        if not fixture and any(i not in worker.begun for i in indices):
            raise ValueError("worker outcome precedes test start")
        if "status" not in event:
            raise ValueError("worker outcome lacks its status")
        status = event["status"]
        expected_status = {
            "success": "passed",
            "skip": "skipped",
            "fixture_skip": "skipped",
            "expected_failure": "skipped",
            "subtest_skip": None,
            "subtest_end": "skipped",
        }.get(kind, "failed")
        if status != expected_status:
            raise ValueError("worker outcome status is invalid")
        if status == "failed":
            from literate_ai.contracts.identity import ContentIdentity

            identity = event.get("failure_identity")
            if not isinstance(identity, str):
                raise ValueError("worker failure lacks its fingerprint")
            ContentIdentity.parse_uri(identity)
        if kind in {
            "failure",
            "error",
            "subtest_failure",
            "subtest_error",
            "fixture_error",
            "expected_failure",
        }:
            if not isinstance(event.get("detail"), str) or len(event["detail"]) > 32768:
                raise ValueError("worker failure diagnostic is invalid")
        if kind in {"skip", "subtest_skip", "fixture_skip"}:
            if not isinstance(event.get("reason"), str) or len(event["reason"]) > 4096:
                raise ValueError("worker skip lacks a bounded reason")
        if kind == "fixture_error":
            invalid = event.get("invalidate")
            if (
                not isinstance(invalid, list)
                or not invalid
                or any(
                    type(i) is not int or i not in worker.group.indices for i in invalid
                )
            ):
                raise ValueError("fixture invalidation names unselected tests")
            self.invalidate(invalid)
            worker.terminal.difference_update(invalid)
        if status is not None:
            for index in indices:
                if index in worker.terminal:
                    previous = worker.statuses[index]
                    if previous != "failed" or status not in {"failed", "skipped"}:
                        raise ValueError("worker emitted duplicate terminal outcomes")
                    if status == "skipped":
                        # A skipped teardown still contributes a raw skip, but
                        # cannot downgrade the case's already observed failure.
                        continue
                worker.terminal.add(index)
                worker.statuses[index] = status
                self.outcome(index, event)
        test = (
            RemoteTest(str(event["description"]), indices[0])
            if fixture
            else self.tests[indices[0]]
        )
        if kind in {
            "failure",
            "error",
            "subtest_failure",
            "subtest_error",
            "fixture_error",
        }:
            detail = event.get("detail")
            if not isinstance(detail, str) or len(detail) > 32768:
                raise ValueError("worker failure diagnostic is invalid")
            target = (
                result.errors
                if kind in {"error", "subtest_error", "fixture_error"}
                else result.failures
            )
            target.append((test, detail))
            worker.failed = self.failed = True
            self._display(event, "E" if target is result.errors else "F")
        elif kind == "unexpected_success":
            result.unexpectedSuccesses.append(test)
            worker.failed = self.failed = True
            self._display(event, "u")
        elif kind in {"skip", "subtest_skip", "fixture_skip"}:
            reason = event.get("reason")
            if not isinstance(reason, str):
                raise ValueError("worker skip lacks a reason")
            result.skipped.append((test, reason))
            self._display(event, "s" if self.verbosity < 2 else f"skipped {reason!r}")
        elif kind == "expected_failure":
            result.expectedFailures.append((test, str(event.get("detail", ""))))
            self._display(event, "x")
        elif kind == "success":
            self._display(event, "." if self.verbosity < 2 else "ok")

    def _drain(self, worker: RunningModule, result: Any) -> None:
        path = worker.root / "events.jsonl"
        if not path.exists():
            return
        if path.stat().st_size > MAX_EVENTS_BYTES:
            raise ValueError("worker event stream exceeds its bound")
        with path.open("rb") as stream:
            stream.seek(worker.offset)
            while True:
                line = stream.readline(MAX_EVENT_BYTES + 1)
                if not line:
                    break
                if len(line) > MAX_EVENT_BYTES:
                    raise ValueError("worker event exceeds its bound")
                if not line.endswith(b"\n"):
                    break
                self._event(worker, json.loads(line), result)
                worker.offset = stream.tell()

    def _error(self, worker: RunningModule, error: BaseException, result: Any) -> None:
        from literate_ai.contracts.identity import canonical_identity

        self.invalidate(worker.group.indices)
        message = f"parallel test worker failed: {error}"
        event = {
            "status": "failed",
            "failure_identity": canonical_identity({"worker_error": message}).uri,
        }
        self.outcome(worker.group.indices[0], event)
        result.errors.append((self.tests[worker.group.indices[0]], message + "\n"))
        worker.failed = self.failed = True
        worker.invalid = True
        print(f"\n{message}; output retained at {worker.root}", file=self.stream)

    def _close(self, worker: RunningModule) -> None:
        try:
            # Also close surviving descendants after a normally exiting root.
            terminate_process_tree(worker.process, ownership=worker.ownership)
            try:
                worker.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                worker.process.kill()
                worker.process.wait(timeout=5)
        finally:
            worker.ownership.release()
            worker.log.close()

    def _finish_failures(self, result: Any) -> None:
        # Other modules stop immediately. Give the failing module's ordinary
        # teardown/cleanup callbacks a bounded chance to report additional
        # diagnostics; unittest itself has already stopped case dispatch.
        for worker in tuple(self.active):
            if not worker.failed:
                self._close(worker)
                self.active.remove(worker)
        deadline = time.monotonic() + 5
        for worker in self.active:
            if worker.invalid:
                continue
            try:
                worker_deadline = min(deadline, worker.started + self.timeout)
                while time.monotonic() < worker_deadline:
                    if (worker.root / "output.log").stat().st_size > MAX_LOG_BYTES:
                        raise ValueError("worker output exceeds its bound")
                    self._drain(worker, result)
                    if worker.process.poll() is not None:
                        self._drain(worker, result)
                        if not worker.done:
                            raise ValueError("worker exited without a complete report")
                        events = worker.root / "events.jsonl"
                        if events.exists() and events.stat().st_size != worker.offset:
                            raise ValueError("worker left an incomplete event")
                        if worker.process.returncode != 1:
                            raise ValueError("worker exit contradicts its report")
                        break
                    time.sleep(0.01)
            except (OSError, ValueError, TimeoutError) as exc:
                self._error(worker, exc, result)

    def __call__(self, result: Any) -> Any:
        print(
            f"parallel tests: {self.workers} workers; logs {self.directory}",
            file=self.stream,
        )
        try:
            while self.pending or self.active:
                finished: list[RunningModule] = []
                for worker in tuple(self.active):
                    try:
                        self._drain(worker, result)
                        if (worker.root / "output.log").stat().st_size > MAX_LOG_BYTES:
                            raise ValueError("worker output exceeds its bound")
                        if time.monotonic() - worker.started > self.timeout:
                            raise TimeoutError("worker exceeded its finite deadline")
                        if worker.process.poll() is not None:
                            self._drain(worker, result)
                            if (
                                worker.root / "events.jsonl"
                            ).exists() and worker.offset != (
                                worker.root / "events.jsonl"
                            ).stat().st_size:
                                raise ValueError("worker left an incomplete event")
                            if not worker.done:
                                raise ValueError(
                                    "worker exited without a complete report"
                                )
                            if worker.process.returncode != (1 if worker.failed else 0):
                                raise ValueError("worker exit contradicts its report")
                            finished.append(worker)
                    except (OSError, ValueError, TimeoutError) as exc:
                        self._error(worker, exc, result)
                if self.failed:
                    self._finish_failures(result)
                    break
                for worker in finished:
                    self._close(worker)
                    self.active.remove(worker)
                    shutil.rmtree(worker.scratch)
                while self.pending and len(self.active) < self.workers:
                    group = self.pending[0]
                    if (group.serial and self.active) or any(
                        w.group.serial for w in self.active
                    ):
                        break
                    self.pending.pop(0)
                    try:
                        self.active.append(self._start(group))
                    except (OSError, ValueError) as exc:
                        from literate_ai.contracts.identity import canonical_identity

                        message = f"parallel test worker could not start: {exc}"
                        self.outcome(
                            group.indices[0],
                            {
                                "status": "failed",
                                "failure_identity": canonical_identity(
                                    {"worker_error": message}
                                ).uri,
                            },
                        )
                        result.errors.append(
                            (self.tests[group.indices[0]], message + "\n")
                        )
                        self.failed = True
                        break
                    if group.serial:
                        break
                if self.active:
                    time.sleep(0.01)
        finally:
            for worker in self.active:
                self._close(worker)
            self.active.clear()
        # Report failures by discovery order, irrespective of worker completion order.
        order = {id(test): index for index, test in enumerate(self.tests)}
        result.unexpectedSuccesses.sort(
            key=lambda test: order.get(id(test), len(order))
        )
        for records in (result.failures, result.errors):
            records.sort(
                key=lambda pair: getattr(
                    pair[0], "order", order.get(id(pair[0]), len(order))
                )
            )
        return result
