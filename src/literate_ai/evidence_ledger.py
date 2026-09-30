"""Fail-soft, append-only evidence ledger for release operations.

The ledger is deliberately independent of release execution.  Callers can adopt it
incrementally by opening a run, entering nodes, and passing ``child_environment`` to
subprocesses.  Recording failures are swallowed so adding evidence cannot change the
behaviour of an existing operation.

An explicitly supplied project root always owns a newly opened run.  Ambient
``LITAI_EVIDENCE_RUN`` and ``LITAI_EVIDENCE_PARENT`` values are used only when joining
the matching parent run deliberately through :func:`attach_run(project_root)`, and
only when the ambient run's evidence root belongs to that project.  The unscoped
``attach_run()`` form is for child operations without project context.  In
particular, the step harness deliberately wraps a child of the same operation and
has no project context of its own.  Child environments created by an existing run
continue to inherit that run and parent node.
"""

from __future__ import annotations

import codecs
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ._cache_lock import cache_lock_path, exclusive_cache_lock
from .adapters._processes import (
    create_process_tree_ownership,
    terminate_process_tree,
)
from .diagnostics import (
    inherited_verbose_environment,
    redact_argv,
    redact_secrets,
    report_progress,
)

EVIDENCE_LEDGER_SCHEMA = "literate-ai/evidence-ledger@1"
EVIDENCE_NODE_SCHEMA = "literate-ai/evidence-ledger-node@1"
EVIDENCE_RUN_ENVIRONMENT = "LITAI_EVIDENCE_RUN"
EVIDENCE_PARENT_ENVIRONMENT = "LITAI_EVIDENCE_PARENT"
EVIDENCE_ROOT_NAME = "evidence"
RETENTION_STATES = frozenset({"retained", "pruned", "host-only", "imported"})
_STATE_VALUES = frozenset(
    {"pending", "running", "passed", "failed", "skipped", "unavailable"}
)
_MAX_ERROR_EXCERPT = 4096
_MAX_RENDER_ERROR_DETAIL = 512


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def evidence_root(project_root: Path) -> Path:
    """Return the configured evidence directory for one project."""

    root = Path(os.path.realpath(Path(project_root).resolve()))
    configured = os.environ.get("OBJ_DIR", "").strip()
    if configured:
        obj = Path(configured)
        if not obj.is_absolute():
            obj = root / obj
        else:
            obj = Path(os.path.realpath(obj.resolve()))
    else:
        obj = root / "_build"
    evidence = obj / EVIDENCE_ROOT_NAME
    if configured and Path(configured).is_absolute():
        try:
            obj.relative_to(root)
        except ValueError:
            scope = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]
            evidence = evidence / "projects" / scope
    return evidence


def _json_text(value: object, environment: Mapping[str, str] | None = None) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return redact_secrets(encoded, environment) + "\n"


def _atomic_bytes(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def _safe_name(value: str) -> str:
    name = Path(value).name
    if not name or name in {".", ".."} or name != value:
        raise ValueError("evidence attachment name must be a simple file name")
    return name


def bounded_excerpt(value: object) -> str:
    """Return the bounded, secret-redacted tail used in public diagnostics."""

    text = redact_secrets(str(value))
    return text[-_MAX_ERROR_EXCERPT:]


def _error_record(code: str, message: object, excerpt: object = "") -> dict[str, str]:
    return {
        "code": redact_secrets(code),
        "message": redact_secrets(str(message)),
        "excerpt": bounded_excerpt(excerpt or message),
    }


def _relative_or_absolute(root: Path, path: Path | str) -> str:
    if isinstance(path, str) and path.startswith("/") and not Path(path).exists():
        return path
    resolved = Path(path)
    try:
        return resolved.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError, RuntimeError):
        try:
            return str(resolved.resolve())
        except OSError:
            return str(resolved)


def _digest_bytes(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


class EvidenceRun:
    """One run directory and its append-only ledger."""

    def __init__(
        self,
        run_id: str,
        root: Path,
        *,
        project_root: Path | None = None,
        operation: str = "unknown",
        created_at: str | None = None,
        inherit_parent: bool = True,
    ) -> None:
        self.run_id = run_id
        self.root = Path(root)
        self.project_root = (
            Path(project_root).resolve()
            if project_root is not None
            else Path.cwd().resolve()
        )
        self.operation = operation
        self.created_at = created_at or _now()
        self._inherit_parent = inherit_parent
        self.ledger_path = self.root / "ledger.ndjson"
        self._state = "running"

    @property
    def _ledger_lock(self) -> Path:
        return cache_lock_path(self.project_root, self.root, "ledger-write")

    @property
    def _counter_lock(self) -> Path:
        return cache_lock_path(self.project_root, self.root, "node-counter")

    def _record(self, record: Mapping[str, object]) -> None:
        """Append one flushed, redacted record, swallowing ledger failures."""

        try:
            self.root.mkdir(parents=True, exist_ok=True)
            line = _json_text(dict(record))
            with exclusive_cache_lock(self._ledger_lock):
                with self.ledger_path.open(
                    "a", encoding="utf-8", newline="\n"
                ) as handle:
                    handle.write(line)
                    handle.flush()
        except Exception:
            return

    def _allocate_node_id(self) -> str | None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            counter = self.root / "node-counter"
            with exclusive_cache_lock(self._counter_lock):
                try:
                    value = int(counter.read_text(encoding="ascii").strip())
                except (FileNotFoundError, ValueError):
                    value = 0
                node_id = f"n{value + 1:04d}"
                _atomic_bytes(counter, f"{value + 1}\n".encode("ascii"))
            return node_id
        except Exception:
            return None

    def node(
        self,
        path: str,
        *,
        operation: str,
        parent: str | None = None,
        host: Mapping[str, object] | None = None,
        pins: Mapping[str, object] | None = None,
    ) -> AbstractContextManager[EvidenceNode]:
        """Return a context manager for a node; failed allocation becomes a no-op."""

        node_id = self._allocate_node_id()
        if node_id is None:
            return _NullEvidenceNode()
        return EvidenceNode(
            self,
            node_id,
            path,
            operation=operation,
            parent=(
                parent
                if parent is not None
                else (
                    os.environ.get(EVIDENCE_PARENT_ENVIRONMENT)
                    if self._inherit_parent
                    else None
                )
            ),
            host=host,
            pins=pins,
        )

    def child_environment(
        self,
        node_id: str,
        environment: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        try:
            inherited = inherited_verbose_environment(
                dict(os.environ if environment is None else environment)
            )
            inherited[EVIDENCE_RUN_ENVIRONMENT] = str(self.root.resolve())
            inherited[EVIDENCE_PARENT_ENVIRONMENT] = node_id
            return inherited
        except Exception:
            return dict(os.environ if environment is None else environment)

    def _read_records(self) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        try:
            with self.ledger_path.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        value = json.loads(line)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if isinstance(value, dict):
                        records.append(value)
        except (OSError, UnicodeDecodeError):
            pass
        return records

    def reduced(self) -> dict[str, object]:
        """Reduce the ledger using the last record for each node."""

        records = self._read_records()
        nodes: dict[str, dict[str, object]] = {}
        closed = False
        run_record: dict[str, object] = {
            "schema": EVIDENCE_LEDGER_SCHEMA,
            "run_id": self.run_id,
            "operation": self.operation,
            "root": str(self.root.resolve()),
            "created_at": self.created_at,
            "state": self._state,
        }
        for record in records:
            if record.get("schema") == EVIDENCE_LEDGER_SCHEMA:
                run_record.update(record)
                closed = isinstance(record.get("closed_at"), str)
            node_id = record.get("node_id")
            if record.get("schema") == EVIDENCE_NODE_SCHEMA and isinstance(
                node_id, str
            ):
                nodes[node_id] = dict(record)
        children: dict[str, list[str]] = {}
        for node_id, node in nodes.items():
            parent = node.get("parent_id")
            if isinstance(parent, str) and parent in nodes:
                children.setdefault(parent, []).append(node_id)
        for node_id, node in nodes.items():
            node["children"] = sorted(children.get(node_id, []))
        if not closed:
            node_states = {str(node.get("state")) for node in nodes.values()}
            if node_states & {"failed", "unavailable"}:
                run_record["state"] = "failed"
            elif node_states & {"pending", "running"}:
                run_record["state"] = "running"
            elif node_states:
                run_record["state"] = "passed"
        run_record["nodes"] = [nodes[node_id] for node_id in sorted(nodes)]
        run_record["node_count"] = len(nodes)
        return run_record

    def close(self, state: str) -> None:
        """Append an authoritative terminal run record when recording is available."""

        try:
            if state not in {"passed", "failed", "skipped", "unavailable"}:
                return
            self._state = state
            self._record(
                {
                    "schema": EVIDENCE_LEDGER_SCHEMA,
                    "run_id": self.run_id,
                    "operation": self.operation,
                    "root": str(self.root.resolve()),
                    "created_at": self.created_at,
                    "closed_at": _now(),
                    "state": state,
                }
            )
            self.write_index()
        except Exception:
            return

    def _finish_interrupted_descendants(
        self, parent_id: str, exc: BaseException
    ) -> None:
        """Finalize unfinished child evidence after its owned process tree stops."""
        try:
            nodes = self.reduced()["nodes"]
            descendants = {parent_id}
            while True:
                found = {
                    node["node_id"]
                    for node in nodes
                    if node.get("parent_id") in descendants
                }
                if found <= descendants:
                    break
                descendants.update(found)
            for node in nodes:
                if (
                    node["node_id"] != parent_id
                    and node["node_id"] in descendants
                    and node.get("state") in {"pending", "running"}
                ):
                    record = {
                        key: value for key, value in node.items() if key != "children"
                    }
                    record.update(
                        state="failed",
                        ended_at=_now(),
                        error=_error_record(
                            f"exception.{type(exc).__name__.casefold()}",
                            "owning subprocess was interrupted",
                            str(exc) or type(exc).__name__,
                        ),
                    )
                    self._record(record)
            self.write_index()
        except Exception:
            return

    def write_index(self) -> Path | None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            index = self.reduced()
            target = self.root / "index.json"
            _atomic_bytes(target, _json_text(index).encode("utf-8"))
            return target
        except Exception:
            return None


class EvidenceNode(AbstractContextManager["EvidenceNode"]):
    """A node whose lifecycle records are append-only snapshots."""

    def __init__(
        self,
        run: EvidenceRun,
        node_id: str,
        path: str,
        *,
        operation: str,
        parent: str | None,
        host: Mapping[str, object] | None,
        pins: Mapping[str, object] | None,
    ) -> None:
        self.run = run
        self.node_id = node_id
        self.path = str(path)
        self.operation = operation
        self.parent_id = parent
        self.directory = run.root / node_id
        self._record_state: dict[str, object] = {
            "schema": EVIDENCE_NODE_SCHEMA,
            "run_id": run.run_id,
            "node_id": node_id,
            "path": self.path,
            "operation": operation,
            "parent_id": parent,
            "host": dict(host or {}),
            "pins": dict(pins or {}),
            "outputs": [],
            "state": "pending",
            "started_at": None,
            "ended_at": None,
            "error": None,
        }
        self._finalized = False

    def _append(self) -> None:
        self.run._record(self._record_state)

    def __enter__(self) -> EvidenceNode:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        self._record_state["state"] = "running"
        self._record_state["started_at"] = _now()
        self._append()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> bool:
        if not self._finalized:
            if exc is None:
                self._finish("passed")
            else:
                self._finish_exception(exc)
        return False

    def _finish(self, state: str, error: Mapping[str, object] | None = None) -> None:
        if state not in _STATE_VALUES:
            return
        self._record_state["state"] = state
        self._record_state["error"] = dict(error) if error is not None else None
        self._record_state["ended_at"] = _now()
        self._finalized = True
        self._append()
        self.run.write_index()

    def _finish_exception(self, exc: BaseException) -> None:
        self._finish(
            "failed",
            _error_record(
                f"exception.{type(exc).__name__.casefold()}",
                str(exc) or type(exc).__name__,
                str(exc),
            ),
        )

    def finish(self, exit_status: int) -> None:
        if exit_status:
            self._finish(
                "failed",
                _error_record(
                    "subprocess.exit",
                    f"subprocess exited with status {exit_status}",
                    f"subprocess exited with status {exit_status}",
                ),
            )
        else:
            self._finish("passed")

    def add_pins(self, **pins: object) -> None:
        try:
            current = self._record_state.setdefault("pins", {})
            if isinstance(current, dict):
                current.update(pins)
            self._append()
        except Exception:
            return

    def add_output(
        self,
        *,
        role: str,
        path: Path | str,
        media_type: str,
        retention: str = "retained",
        digest: str | None = None,
        bytes: int | None = None,
    ) -> None:
        try:
            if retention not in RETENTION_STATES:
                raise ValueError("unsupported evidence retention state")
            output_path = Path(path)
            if media_type == "inode/directory" or output_path.is_dir():
                digest = None
                bytes = None
                media_type = "inode/directory"
            elif digest is None or bytes is None:
                hasher = hashlib.sha256()
                total = 0
                with output_path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        hasher.update(chunk)
                        total += len(chunk)
                digest = "sha256:" + hasher.hexdigest()
                bytes = total
            pointer = {
                "role": role,
                "path": _relative_or_absolute(self.run.root, path),
                "bytes": bytes,
                "digest": digest,
                "media_type": media_type,
                "retention": retention,
            }
            outputs = self._record_state.setdefault("outputs", [])
            if isinstance(outputs, list):
                outputs.append(pointer)
            self._append()
        except Exception:
            return

    def update_output_retention(
        self,
        *,
        role: str,
        path: Path | str,
        retention: str,
    ) -> None:
        """Record a retention transition for one previously registered output."""

        try:
            if retention not in RETENTION_STATES:
                raise ValueError("unsupported evidence retention state")
            expected = _relative_or_absolute(self.run.root, path)
            outputs = self._record_state.get("outputs", [])
            if not isinstance(outputs, list):
                return
            for output in outputs:
                if (
                    isinstance(output, dict)
                    and output.get("role") == role
                    and output.get("path") == expected
                ):
                    output["retention"] = retention
                    self._append()
                    return
        except Exception:
            return

    def attach_text(
        self,
        name: str,
        text: str,
        *,
        role: str,
        media_type: str = "text/plain",
    ) -> None:
        try:
            filename = _safe_name(name)
            target = self.directory / filename
            redacted = redact_secrets(text)
            encoded = redacted.encode("utf-8")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(encoded)
            self.add_output(
                role=role,
                path=target,
                media_type=media_type,
                digest=_digest_bytes(encoded),
                bytes=len(encoded),
            )
        except Exception:
            return

    def skip(self, reason: str, *, evidence_node_id: str | None = None) -> None:
        try:
            self.add_pins(
                skip_reason=reason,
                **(
                    {"evidence_node_id": evidence_node_id}
                    if evidence_node_id is not None
                    else {}
                ),
            )
            self._finish("skipped")
        except Exception:
            return

    def mark_unavailable(self, reason: str) -> None:
        try:
            self.add_pins(unavailable_reason=reason)
            self._finish(
                "unavailable",
                _error_record("evidence.unavailable", reason, reason),
            )
        except Exception:
            return

    def fail(self, reason: str) -> None:
        """Finalize this node as failed without raising into its caller."""

        try:
            self._finish("failed", _error_record("evidence.failed", reason, reason))
        except Exception:
            return


class _NullEvidenceNode(AbstractContextManager["_NullEvidenceNode"]):
    node_id = ""
    directory = Path(".")

    def __enter__(self) -> _NullEvidenceNode:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> bool:
        return False

    def add_pins(self, **pins: object) -> None:
        return

    def add_output(self, **kwargs: object) -> None:
        return

    def finish(self, exit_status: int) -> None:
        return

    def fail(self, reason: str) -> None:
        return

    def attach_text(self, *args: object, **kwargs: object) -> None:
        return

    def skip(self, reason: str, *, evidence_node_id: str | None = None) -> None:
        return

    def mark_unavailable(self, reason: str) -> None:
        return


def _new_run_id(operation: str, created_at: str) -> str:
    try:
        timestamp = datetime.fromisoformat(created_at.replace("Z", "+00:00")).strftime(
            "%Y%m%dT%H%M%SZ"
        )
    except ValueError:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    identity = (
        f"{operation}\0{created_at}\0{os.getpid()}\0{os.urandom(8).hex()}".encode()
    )
    digest = hashlib.sha256(identity).hexdigest()[:6]
    return f"{timestamp}-{digest}"


def _run_from_root(
    root: Path, *, project_root: Path | None = None, inherit_parent: bool = False
) -> EvidenceRun | None:
    try:
        if root.is_symlink() or not root.is_dir():
            return None
        run_id = root.name
        if not run_id or not (root / "ledger.ndjson").is_file():
            return None
        records = EvidenceRun(
            run_id,
            root,
            project_root=project_root,
            inherit_parent=inherit_parent,
        )._read_records()
        first = next(
            (item for item in records if item.get("schema") == EVIDENCE_LEDGER_SCHEMA),
            {},
        )
        return EvidenceRun(
            run_id,
            root,
            project_root=project_root,
            operation=str(first.get("operation", "unknown")),
            created_at=str(first.get("created_at", _now())),
            inherit_parent=inherit_parent,
        )
    except Exception:
        return None


def open_run(project_root: Path, *, operation: str) -> EvidenceRun | None:
    """Create a project-owned run without adopting an unrelated ambient run."""

    try:
        project = Path(project_root).resolve(strict=True)
        root = evidence_root(project)
        root.mkdir(parents=True, exist_ok=True)
        for _ in range(8):
            created_at = _now()
            run_id = _new_run_id(operation, created_at)
            run_root = root / run_id
            try:
                run_root.mkdir()
                run_id = run_root.name
                run = EvidenceRun(
                    run_id,
                    run_root,
                    project_root=project,
                    operation=operation,
                    inherit_parent=False,
                )
                run._record(
                    {
                        "schema": EVIDENCE_LEDGER_SCHEMA,
                        "run_id": run_id,
                        "operation": operation,
                        "root": str(run_root.resolve()),
                        "created_at": run.created_at,
                        "state": "running",
                    }
                )
                if not run.ledger_path.is_file():
                    return None
                _update_latest(root, run_id)
                return run
            except FileExistsError:
                continue
        return None
    except Exception:
        return None


def attach_run(project_root: Path | None = None) -> EvidenceRun | None:
    configured = os.environ.get(EVIDENCE_RUN_ENVIRONMENT)
    if not configured:
        return None
    try:
        ambient_root = Path(configured).resolve()
        if project_root is not None:
            project = Path(project_root).resolve(strict=True)
            if ambient_root.parent != evidence_root(project).resolve():
                return None
            return _run_from_root(
                ambient_root,
                project_root=project,
                inherit_parent=True,
            )
        return _run_from_root(ambient_root, inherit_parent=True)
    except (OSError, RuntimeError, ValueError):
        return None


def record_retained_output(
    path: Path | str,
    *,
    node_path: str,
    operation: str,
    role: str,
    media_type: str = "inode/directory",
    retention: str = "retained",
    pins: Mapping[str, object] | None = None,
    digest: str | None = None,
    bytes: int | None = None,
    failed: bool = False,
) -> str | None:
    """Best-effort pointer registration for an inherited evidence run."""

    try:
        run = attach_run()
        resolved = Path(path)
        if run is None:
            report_progress(f"Retained {role}: {resolved.resolve()}")
            return None
        context = run.node(
            node_path,
            operation=operation,
            parent=os.environ.get(EVIDENCE_PARENT_ENVIRONMENT),
            pins=pins,
        )
        with context as node:
            if not isinstance(node, EvidenceNode):
                return None
            node.add_output(
                role=role,
                path=resolved,
                media_type=media_type,
                retention=retention,
                digest=digest,
                bytes=bytes,
            )
            if failed:
                node.fail(f"retained {role} after failure")
            absolute = str(resolved.resolve())
            report_progress(f"Retained {role}: {absolute}")
            return absolute
    except Exception:
        return None


@contextmanager
def retained_directory(
    path: Path | str,
    *,
    node_path: str,
    operation: str,
    role: str,
    success: Callable[[], bool] | None = None,
) -> Iterator[Path]:
    """Register a directory at ownership and prune it only after success."""

    resolved = Path(path)
    run = attach_run()
    if run is None:
        try:
            yield resolved
        except BaseException:
            report_progress(f"Retained {role}: {resolved.resolve()}")
            raise
        else:
            if success is not None and not success():
                report_progress(f"Retained {role}: {resolved.resolve()}")
                return
            shutil.rmtree(resolved, ignore_errors=True)
        return

    context = run.node(
        node_path,
        operation=operation,
        parent=os.environ.get(EVIDENCE_PARENT_ENVIRONMENT),
    )
    with context as node:
        if isinstance(node, EvidenceNode):
            node.add_output(
                role=role,
                path=resolved,
                media_type="inode/directory",
                retention="retained",
            )
        try:
            yield resolved
        except BaseException:
            report_progress(f"Retained {role}: {resolved.resolve()}")
            raise
        else:
            if success is not None and not success():
                if isinstance(node, EvidenceNode):
                    node.fail(f"retained {role} after unsuccessful operation")
                report_progress(f"Retained {role}: {resolved.resolve()}")
                return
            shutil.rmtree(resolved, ignore_errors=True)
            if isinstance(node, EvidenceNode):
                node.update_output_retention(
                    role=role,
                    path=resolved,
                    retention="pruned",
                )


def load_run(project_root: Path, run: str | Path) -> EvidenceRun | None:
    """Load a run by id or absolute run root."""

    try:
        project = Path(project_root).resolve(strict=True)
        candidate = Path(run)
        if not candidate.is_absolute():
            candidate = evidence_root(project) / candidate
        return _run_from_root(candidate.resolve(), project_root=project)
    except Exception:
        return None


def _update_latest(root: Path, run_id: str) -> None:
    latest = root / "latest"
    temporary = root / f".latest.{os.getpid()}.tmp"
    try:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        try:
            temporary.symlink_to(run_id, target_is_directory=True)
            os.replace(temporary, latest)
        except (OSError, NotImplementedError):
            try:
                temporary.unlink()
            except OSError:
                pass
            _atomic_bytes(temporary, (run_id + "\n").encode("utf-8"))
            os.replace(temporary, latest)
    except Exception:
        try:
            temporary.unlink()
        except OSError:
            pass


def _latest_run_id(root: Path) -> str | None:
    try:
        latest = root / "latest"
        if latest.is_symlink():
            value = Path(os.readlink(latest)).name
        elif latest.is_file():
            value = latest.read_text(encoding="utf-8").strip()
        else:
            return None
        return value if value and Path(value).name == value else None
    except Exception:
        return None


def _remove_latest(root: Path) -> None:
    try:
        (root / "latest").unlink(missing_ok=True)
    except Exception:
        return


def latest_run(project_root: Path) -> EvidenceRun | None:
    try:
        root = evidence_root(Path(project_root).resolve(strict=True))
        latest = root / "latest"
        if latest.is_symlink():
            return _run_from_root(
                (root / os.readlink(latest)).resolve(),
                project_root=Path(project_root),
            )
        if latest.is_file():
            run_id = latest.read_text(encoding="utf-8").strip()
            if run_id and Path(run_id).name == run_id:
                return _run_from_root(root / run_id, project_root=Path(project_root))
    except Exception:
        return None
    return None


def _run_streaming(
    argv: Sequence[str],
    *,
    cwd: Path | str | None,
    environment: Mapping[str, str],
    timeout: float | None,
    stdout_file: Path | None,
    stderr_file: Path | None,
    pointer_stats: dict[str, tuple[Any, int]] | None = None,
    tee: bool = True,
) -> subprocess.CompletedProcess[str]:
    ownership = create_process_tree_ownership()
    deadline = None if timeout is None else time.monotonic() + timeout

    def remaining_timeout() -> float | None:
        return None if deadline is None else max(0.0, deadline - time.monotonic())

    process = None
    streams: dict[str, list[str]] = {"stdout": [], "stderr": []}
    files: dict[str, Any] = {}
    threads: list[threading.Thread] = []
    try:
        process = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **ownership.popen_options,
        )
        ownership.bind(process.pid)
        if stdout_file is not None:
            try:
                stdout_file.parent.mkdir(parents=True, exist_ok=True)
                files["stdout"] = stdout_file.open("wb")
            except Exception:
                pass
        if stderr_file is not None:
            try:
                stderr_file.parent.mkdir(parents=True, exist_ok=True)
                files["stderr"] = stderr_file.open("wb")
            except Exception:
                pass

        def consume(label: str, pipe: Any, parent: Any, target: Any) -> None:
            try:
                decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                while True:
                    chunk = pipe.read(4096)
                    text = decoder.decode(chunk, final=not chunk)
                    streams[label].append(text)
                    encoded = text.encode("utf-8")
                    buffer = getattr(parent, "buffer", None)
                    if tee and buffer is not None:
                        buffer.write(encoded)
                        buffer.flush()
                    elif tee:
                        parent.write(text)
                        parent.flush()
                    if tee and target is not None:
                        persisted = redact_secrets(text, environment).encode("utf-8")
                        target.write(persisted)
                        target.flush()
                        if pointer_stats is not None:
                            hasher, total = pointer_stats.setdefault(
                                label, (hashlib.sha256(), 0)
                            )
                            hasher.update(persisted)
                            pointer_stats[label] = (hasher, total + len(persisted))
                    if not chunk:
                        break
            except BaseException:
                return

        threads = [
            threading.Thread(
                target=consume,
                args=(
                    "stdout",
                    process.stdout,
                    sys.stdout,
                    files.get("stdout"),
                ),
                daemon=True,
            ),
            threading.Thread(
                target=consume,
                args=(
                    "stderr",
                    process.stderr,
                    sys.stderr,
                    files.get("stderr"),
                ),
                daemon=True,
            ),
        ]
        for thread in threads:
            thread.start()
        process.wait(timeout=remaining_timeout())
        for thread in threads:
            thread.join(timeout=remaining_timeout())
            if thread.is_alive():
                raise subprocess.TimeoutExpired(list(argv), timeout)
    except BaseException:
        if process is not None:
            try:
                terminate_process_tree(process, ownership=ownership)
            except Exception:
                process.kill()
            process.wait()
            for thread in threads:
                if thread.ident is not None:
                    thread.join()
        raise
    finally:
        if not tee:
            for label, target in files.items():
                try:
                    text = (
                        "".join(streams[label])
                        .replace("\r\n", "\n")
                        .replace("\r", "\n")
                    )
                    persisted = redact_secrets(text, environment).encode("utf-8")
                    target.write(persisted)
                    if pointer_stats is not None:
                        pointer_stats[label] = (
                            hashlib.sha256(persisted),
                            len(persisted),
                        )
                except Exception:
                    pass
        for target in files.values():
            try:
                target.close()
            except Exception:
                pass
        if process is not None:
            for pipe in (process.stdout, process.stderr):
                if pipe is not None:
                    try:
                        pipe.close()
                    except Exception:
                        pass
        try:
            ownership.release()
        except Exception:
            pass
    captured = {label: "".join(chunks) for label, chunks in streams.items()}
    if not tee:
        # Match subprocess.run(text=True)'s universal-newline return values.
        captured = {
            label: text.replace("\r\n", "\n").replace("\r", "\n")
            for label, text in captured.items()
        }
    return subprocess.CompletedProcess(
        list(argv),
        process.returncode,
        captured["stdout"],
        captured["stderr"],
    )


def _add_transcript_outputs(
    node: EvidenceNode,
    stdout_file: Path | None,
    stderr_file: Path | None,
    pointer_stats: dict[str, tuple[Any, int]],
) -> None:
    for label, target in (("stdout", stdout_file), ("stderr", stderr_file)):
        try:
            if target is not None and target.is_file():
                hasher, total = pointer_stats.get(label, (hashlib.sha256(), 0))
                node.add_output(
                    role=label,
                    path=target,
                    media_type="text/plain",
                    digest="sha256:" + hasher.hexdigest(),
                    bytes=total,
                )
        except Exception:
            continue


def record_subprocess(
    argv: Sequence[str],
    *,
    cwd: Path | str | None,
    run: EvidenceRun | None,
    parent: str | None,
    path: str,
    operation: str,
    environment: Mapping[str, str] | None = None,
    timeout: float | None = None,
    tee: bool = True,
    node: EvidenceNode | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one child, teeing live output and retaining redacted transcripts."""

    base_environment = dict(os.environ if environment is None else environment)
    try:
        child_environment = inherited_verbose_environment(base_environment)
    except Exception:
        child_environment = base_environment
    active_node: EvidenceNode | _NullEvidenceNode | None = node
    context: AbstractContextManager[Any] | None = None
    stdout_file: Path | None = None
    stderr_file: Path | None = None
    pointer_stats: dict[str, tuple[Any, int]] = {}
    if active_node is None and run is not None:
        context = run.node(path, operation=operation, parent=parent)
        active_node = context.__enter__()
    if isinstance(active_node, EvidenceNode):
        if run is not None:
            child_environment = run.child_environment(
                active_node.node_id, child_environment
            )
        active_node.add_pins(
            argv=list(redact_argv(argv, child_environment)),
            cwd=str(cwd) if cwd is not None else None,
        )
        stdout_file = active_node.directory / "stdout.log"
        stderr_file = active_node.directory / "stderr.log"
    try:
        completed = _run_streaming(
            argv,
            cwd=cwd,
            environment=child_environment,
            timeout=timeout,
            stdout_file=stdout_file,
            stderr_file=stderr_file,
            pointer_stats=pointer_stats,
            tee=tee,
        )
        if isinstance(active_node, EvidenceNode):
            _add_transcript_outputs(
                active_node, stdout_file, stderr_file, pointer_stats
            )
            active_node.finish(completed.returncode)
        return completed
    except BaseException as exc:
        if isinstance(active_node, EvidenceNode):
            active_node.run._finish_interrupted_descendants(active_node.node_id, exc)
            _add_transcript_outputs(
                active_node, stdout_file, stderr_file, pointer_stats
            )
            active_node._finish_exception(exc)
        raise
    finally:
        if context is not None:
            context.__exit__(None, None, None)


def _read_index(run: EvidenceRun) -> dict[str, object]:
    try:
        if not (run.root / "index.json").is_file():
            run.write_index()
        value = json.loads((run.root / "index.json").read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else run.reduced()
    except Exception:
        return run.reduced()


def explain_run(run: EvidenceRun) -> dict[str, object]:
    """Return a bounded root-to-failure explanation for one run."""

    index = _read_index(run)
    raw_nodes = index.get("nodes", [])
    nodes = {
        item["node_id"]: item
        for item in raw_nodes
        if isinstance(item, dict) and isinstance(item.get("node_id"), str)
    }
    failures = [
        node
        for node in nodes.values()
        if node.get("state") in {"failed", "unavailable"}
    ]
    failure_ancestors: set[str] = set()
    for failure in failures:
        parent = failure.get("parent_id")
        seen: set[str] = set()
        while isinstance(parent, str) and parent in nodes and parent not in seen:
            failure_ancestors.add(parent)
            seen.add(parent)
            parent = nodes[parent].get("parent_id")
    leaves = [
        node for node in failures if str(node["node_id"]) not in failure_ancestors
    ]
    leaf = (
        max(
            leaves,
            key=lambda item: (
                str(item.get("started_at") or ""),
                str(item["node_id"]),
            ),
        )
        if leaves
        else None
    )
    path: list[dict[str, object]] = []
    cursor = leaf
    while isinstance(cursor, dict):
        path.append(cursor)
        parent = cursor.get("parent_id")
        cursor = nodes.get(parent) if isinstance(parent, str) else None
    path.reverse()
    path_ids = {str(node["node_id"]) for node in path}
    passing_siblings: dict[str, dict[str, int]] = {}
    for node in path:
        counts: dict[str, int] = {}
        for child_id in node.get("children", []):
            if child_id in path_ids:
                continue
            child = nodes.get(child_id)
            if child is None:
                continue
            state = str(child.get("state", "unknown"))
            counts[state] = counts.get(state, 0) + 1
        if counts:
            passing_siblings[str(node["node_id"])] = counts

    def human_size(value: object) -> str:
        try:
            size = float(value)
        except (TypeError, ValueError):
            return "unknown size"
        units = ("B", "KiB", "MiB", "GiB")
        unit = units[0]
        for candidate in units:
            unit = candidate
            if size < 1024 or candidate == units[-1]:
                break
            size /= 1024
        return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"

    def rendered_output_path(pointer: Mapping[str, object]) -> str:
        value = pointer.get("path")
        if not isinstance(value, str) or not value:
            return "<unknown>"
        try:
            return str((run.root / value).resolve())
        except (OSError, RuntimeError):
            return str(run.root / value)

    rendered_errors: set[str] = set()

    def output_pointers(node: Mapping[str, object]) -> list[dict[str, object]]:
        return [
            pointer for pointer in node.get("outputs", []) if isinstance(pointer, dict)
        ]

    transcript_node = leaf
    if isinstance(leaf, dict) and not output_pointers(leaf):
        transcript_node = next(
            (
                candidate
                for candidate in reversed(path[:-1])
                if output_pointers(candidate)
            ),
            None,
        )

    render: list[str] = []
    leaf_id = str(leaf["node_id"]) if leaf is not None else None
    transcript_node_id = (
        str(transcript_node["node_id"]) if isinstance(transcript_node, dict) else None
    )
    for node in path:
        node_id = str(node["node_id"])
        render.append(f"- {node.get('path', node_id)} [{node.get('state')}]")
        error = node.get("error")
        if isinstance(error, Mapping):
            code = error.get("code")
            detail = error.get("excerpt") or error.get("message")
            if isinstance(detail, str):
                collapsed = " ".join(detail.split())
                if len(collapsed) > _MAX_RENDER_ERROR_DETAIL:
                    collapsed = collapsed[-_MAX_RENDER_ERROR_DETAIL:]
                rendered_error = f"{code}: {collapsed}" if code else collapsed
                if rendered_error not in rendered_errors:
                    rendered_errors.add(rendered_error)
                    render.append(f"  error: {rendered_error}")
        if node_id == leaf_id and leaf is not None:
            pins = node.get("pins", {})
            render.append(f"  pins: {json.dumps(pins, sort_keys=True, default=str)}")
            if transcript_node_id is not None and transcript_node_id != node_id:
                render.append(
                    "  transcripts: enclosing step "
                    f"{transcript_node.get('path', transcript_node_id)}"
                )
            for pointer in (
                output_pointers(transcript_node)
                if isinstance(transcript_node, dict)
                else []
            ):
                render.append(
                    "  output: "
                    f"{pointer.get('role', 'output')} "
                    f"{rendered_output_path(pointer)} "
                    f"({human_size(pointer.get('bytes'))}; "
                    f"{pointer.get('retention', 'unknown')})"
                )
        counts = passing_siblings.get(node_id)
        if counts:
            collapsed = ", ".join(
                f"{state}={count}" for state, count in sorted(counts.items())
            )
            render.append(f"  ({collapsed} sibling(s) collapsed)")
    return {
        "run_id": run.run_id,
        "root": str(run.root.resolve()),
        "failure": leaf,
        "path": path,
        "collapsed_siblings": passing_siblings,
        "other_failure_leaves": max(0, len(leaves) - 1),
        "render": render[:64],
    }


def prune_runs(
    project_root: Path,
    *,
    keep: int = 1,
    include_failed: bool = False,
) -> dict[str, object]:
    """Remove eligible historical runs without deleting failures by default."""

    try:
        root = evidence_root(Path(project_root).resolve(strict=True))
        runs: list[tuple[EvidenceRun, dict[str, object]]] = []
        for candidate in root.iterdir():
            if (
                not candidate.is_dir()
                or candidate.is_symlink()
                or candidate.name == "latest"
            ):
                continue
            run = _run_from_root(candidate, project_root=Path(project_root))
            if run is None:
                continue
            report = _read_index(run)
            runs.append((run, report))
        keep = max(0, int(keep))
        latest_id = _latest_run_id(root)
        removable = [
            item
            for item in runs
            if (item[1].get("state") not in {"failed", "unavailable"} or include_failed)
            and item[0].run_id != latest_id
        ]
        removable.sort(
            key=lambda item: (
                0 if item[1].get("state") == "passed" else 1,
                str(item[1].get("created_at", "")),
                item[0].run_id,
            )
        )
        eligible_count = len(removable)
        removed: list[str] = []
        reclaimed = 0
        for run, _report in removable:
            if eligible_count <= keep:
                break
            size = sum(
                path.stat().st_size
                for path in run.root.rglob("*")
                if path.is_file() and not path.is_symlink()
            )
            shutil.rmtree(run.root)
            removed.append(run.run_id)
            reclaimed += size
            eligible_count -= 1
        remaining = [(run, report) for run, report in runs if run.root.is_dir()]
        latest_id = _latest_run_id(root)
        if latest_id not in {run.run_id for run, _report in remaining}:
            _remove_latest(root)
            if remaining:
                newest = max(
                    remaining,
                    key=lambda item: (
                        str(item[1].get("created_at", "")),
                        item[0].run_id,
                    ),
                )
                _update_latest(root, newest[0].run_id)
        return {
            "removed_run_ids": removed,
            "reclaimed_bytes": reclaimed,
            "keep": keep,
            "include_failed": include_failed,
        }
    except Exception:
        return {
            "removed_run_ids": [],
            "reclaimed_bytes": 0,
            "keep": max(0, int(keep)),
            "include_failed": include_failed,
        }


__all__ = [
    "EVIDENCE_LEDGER_SCHEMA",
    "EVIDENCE_NODE_SCHEMA",
    "EVIDENCE_PARENT_ENVIRONMENT",
    "EVIDENCE_RUN_ENVIRONMENT",
    "EvidenceNode",
    "EvidenceRun",
    "attach_run",
    "bounded_excerpt",
    "evidence_root",
    "explain_run",
    "latest_run",
    "load_run",
    "open_run",
    "prune_runs",
    "record_subprocess",
    "record_retained_output",
    "retained_directory",
]
