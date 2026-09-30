"""Bounded opt-in project source-intelligence observation."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from literate_ai.adapters._processes import (
    ProcessTreeOwnership,
    create_process_tree_ownership,
    terminate_process_tree,
)
from literate_ai.contracts import (
    ProjectSourceIntelligencePolicy,
    SemanticVersion,
    SourceIntelligenceMode,
    SourceIntelligenceStage,
)
from literate_ai.contracts.identity import canonical_identity

_MAXIMUM_COMMAND_OUTPUT_BYTES = 1024 * 1024
_MAXIMUM_SNAPSHOT_FILES = 100_000
_MAXIMUM_SNAPSHOT_BYTES = 1024 * 1024 * 1024
_ENVIRONMENT_KEYS = (
    "COMSPEC",
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "WINDIR",
)
_IGNORED_DIRECTORIES = frozenset(
    {
        ".git",
        ".codegraph",
        "node_modules",
        "bower_components",
        "jspm_packages",
        "web_modules",
        ".yarn",
        ".pnpm-store",
        ".next",
        ".nuxt",
        ".svelte-kit",
        ".turbo",
        ".vite",
        ".parcel-cache",
        "__pycache__",
        "__pypackages__",
        ".venv",
        "venv",
        ".pytest_cache",
        ".ruff_cache",
        ".gradle",
        "vendor",
        "Pods",
        "Carthage",
        "DerivedData",
        ".cache",
    }
)


class ProjectSourceIntelligenceError(RuntimeError):
    """A configured project source index is unavailable or unhealthy."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class _ProjectIndexSnapshot:
    runtime_version: str
    executable_identity: str
    file_count: int
    node_count: int
    edge_count: int
    built_with_version: str | None
    extraction_version: int | None
    database_identity: str


class _ProjectIndexEngine(Protocol):
    def preflight(self, working_directory: Path) -> tuple[str, str]: ...

    def capture(
        self, project_root: Path, *, synchronize: bool
    ) -> _ProjectIndexSnapshot: ...


class _CodeGraphCommandEngine:
    """Run one external CodeGraph command against race-checked snapshots."""

    def __init__(
        self,
        binary: str,
        *,
        timeout_seconds: int,
        maximum_snapshot_files: int = _MAXIMUM_SNAPSHOT_FILES,
        maximum_snapshot_bytes: int = _MAXIMUM_SNAPSHOT_BYTES,
    ) -> None:
        if not binary or timeout_seconds <= 0:
            raise ValueError("source-index command and timeout must be positive")
        if maximum_snapshot_files <= 0 or maximum_snapshot_bytes <= 0:
            raise ValueError("source-index snapshot limits must be positive")
        self.binary = binary
        self.timeout_seconds = timeout_seconds
        self.maximum_snapshot_files = maximum_snapshot_files
        self.maximum_snapshot_bytes = maximum_snapshot_bytes
        self._executable: Path | None = None
        self._executable_identity: str | None = None

    def preflight(self, working_directory: Path) -> tuple[str, str]:
        version = self._run(working_directory, "--version").strip()
        if not version or "\n" in version or "\r" in version:
            self._fail(
                "project.source_intelligence_version_invalid",
                "the CodeGraph command did not report one semantic version",
            )
        return version, self._require_executable_identity()

    def capture(
        self, project_root: Path, *, synchronize: bool
    ) -> _ProjectIndexSnapshot:
        root = self._safe_root(project_root)
        index = root / ".codegraph"
        database = index / "codegraph.db"
        runtime_version, executable_identity = self.preflight(root)
        self._require_safe_database(root, database, allow_missing=synchronize)
        if synchronize:
            if database.is_file():
                self._run(root, "sync", str(root), "--quiet")
            else:
                self._run(root, "init", str(root))
        self._require_safe_database(root, database, allow_missing=False)
        source_paths = self._source_paths(root)
        with tempfile.TemporaryDirectory(
            prefix="literate-codegraph-project-status-"
        ) as temporary:
            shadow = (Path(temporary) / "project").resolve()
            shadow.mkdir()
            inventory = self._copy_source_snapshot(root, shadow, source_paths)
            shadow_index = shadow / ".codegraph"
            shadow_index.mkdir()
            database_identity, live_database_identity = self._copy_database_snapshot(
                root, database, shadow_index / "codegraph.db"
            )
            status = self._status(shadow)
            self._require_source_unchanged(root, inventory)
            self._require_safe_database(root, database, allow_missing=False)
            final_database_identity, _final_live_identity = (
                self._copy_database_snapshot(
                    root, database, shadow_index / "codegraph-final.db"
                )
            )
            if final_database_identity != database_identity:
                self._fail(
                    "project.source_intelligence_database_changed",
                    "the CodeGraph database changed while status was observed",
                )
        return self._validated_snapshot(
            status,
            project_path=shadow,
            runtime_version=runtime_version,
            executable_identity=executable_identity,
            database_identity=database_identity,
        )

    @staticmethod
    def _safe_root(project_root: Path) -> Path:
        if project_root.is_symlink():
            _CodeGraphCommandEngine._fail(
                "project.source_intelligence_root_unsafe",
                "the project root must be a regular directory",
            )
        try:
            root = project_root.resolve(strict=True)
        except OSError as exc:
            raise ProjectSourceIntelligenceError(
                "project.source_intelligence_root_unavailable",
                "the project root is unavailable",
            ) from exc
        if not root.is_dir() or root.is_symlink():
            _CodeGraphCommandEngine._fail(
                "project.source_intelligence_root_unsafe",
                "the project root must be a regular directory",
            )
        return root

    @staticmethod
    def _require_safe_database(
        root: Path, database: Path, *, allow_missing: bool
    ) -> None:
        index = database.parent
        if index.is_symlink() or (index.exists() and not index.is_dir()):
            _CodeGraphCommandEngine._fail(
                "project.source_intelligence_path_unsafe",
                "the CodeGraph index directory is unsafe",
            )
        for name in ("codegraph.db-wal", "codegraph.db-shm"):
            sidecar = index / name
            if sidecar.exists() or sidecar.is_symlink():
                if sidecar.is_symlink() or not sidecar.is_file():
                    _CodeGraphCommandEngine._fail(
                        "project.source_intelligence_path_unsafe",
                        "the CodeGraph database has an unsafe SQLite sidecar",
                    )
        for name in ("daemon.pid", "daemon.sock"):
            sidecar = index / name
            if sidecar.exists() or sidecar.is_symlink():
                _CodeGraphCommandEngine._fail(
                    "project.source_intelligence_unhealthy",
                    "the CodeGraph database has active daemon sidecars",
                )
        if database.exists() or database.is_symlink():
            if database.is_symlink() or not database.is_file():
                _CodeGraphCommandEngine._fail(
                    "project.source_intelligence_path_unsafe",
                    "the CodeGraph database must be a regular non-symlink file",
                )
            try:
                resolved = database.resolve(strict=True)
            except OSError as exc:
                raise ProjectSourceIntelligenceError(
                    "project.source_intelligence_path_unsafe",
                    "the CodeGraph database is unavailable",
                ) from exc
            if resolved != database or not resolved.is_relative_to(root):
                _CodeGraphCommandEngine._fail(
                    "project.source_intelligence_path_unsafe",
                    "the CodeGraph database escapes the project root",
                )
        elif not allow_missing:
            _CodeGraphCommandEngine._fail(
                "project.source_intelligence_missing",
                "the CodeGraph database is missing; run source-intelligence sync",
            )

    def _source_paths(self, root: Path) -> tuple[str, ...]:
        paths = self._git_visible_paths(root)
        if paths is not None:
            return paths
        result: list[str] = []
        try:
            for directory, names, filenames in os.walk(root, followlinks=False):
                current = Path(directory)
                for name in tuple(names):
                    candidate = current / name
                    if (
                        name in _IGNORED_DIRECTORIES
                        or name.startswith(".venv-")
                        or name.startswith("venv-")
                        or name.startswith(".codegraph-")
                    ):
                        names.remove(name)
                    elif candidate.is_symlink():
                        self._fail(
                            "project.source_intelligence_snapshot_unsafe",
                            "the source snapshot contains a directory symlink",
                        )
                for name in filenames:
                    relative = PurePosixPath(*(current / name).relative_to(root).parts)
                    result.append(relative.as_posix())
                    if len(result) > self.maximum_snapshot_files:
                        self._snapshot_limit()
        except OSError as exc:
            raise ProjectSourceIntelligenceError(
                "project.source_intelligence_snapshot_unavailable",
                "the source snapshot inventory is unavailable",
            ) from exc
        return tuple(sorted(result))

    def _git_visible_paths(self, root: Path) -> tuple[str, ...] | None:
        git = shutil.which("git")
        if git is None:
            return None
        tracked = self._run_git(
            root, git, "ls-files", "-z", "--cached", "--recurse-submodules"
        )
        untracked = self._run_git(
            root, git, "ls-files", "-z", "-o", "--exclude-standard"
        )
        deleted = self._run_git(root, git, "ls-files", "-z", "--deleted")
        if tracked is None or untracked is None or deleted is None:
            return None
        deleted_paths = {item for item in deleted.split(b"\0") if item}
        paths: set[str] = set()
        for raw in (tracked + untracked).split(b"\0"):
            if not raw or raw in deleted_paths:
                continue
            try:
                relative = PurePosixPath(os.fsdecode(raw))
            except UnicodeError:
                self._fail(
                    "project.source_intelligence_snapshot_unsafe",
                    "the source snapshot contains a non-portable path",
                )
            if relative.is_absolute() or any(
                part in {"", ".", ".."} for part in relative.parts
            ):
                self._fail(
                    "project.source_intelligence_snapshot_unsafe",
                    "the source snapshot contains an unsafe path",
                )
            if relative.parts[0] == ".codegraph" or relative.parts[0].startswith(
                ".codegraph-"
            ):
                continue
            paths.add(relative.as_posix())
            if len(paths) > self.maximum_snapshot_files:
                self._snapshot_limit()
        return tuple(sorted(paths))

    def _run_git(self, root: Path, git: str, *arguments: str) -> bytes | None:
        environment = self._environment()
        environment["GIT_OPTIONAL_LOCKS"] = "0"
        try:
            result = subprocess.run(
                (git, "-c", "core.fsmonitor=false", *arguments),
                cwd=root,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=min(self.timeout_seconds, 30),
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        if len(result.stdout) > 50 * 1024 * 1024:
            self._snapshot_limit()
        return result.stdout

    def _copy_source_snapshot(
        self, root: Path, shadow: Path, paths: tuple[str, ...]
    ) -> tuple[tuple[str, tuple[int, int, int, int], str], ...]:
        inventory: list[tuple[str, tuple[int, int, int, int], str]] = []
        total_bytes = 0
        for relative in paths:
            source = root.joinpath(*PurePosixPath(relative).parts)
            try:
                before = source.stat(follow_symlinks=False)
                resolved = source.resolve(strict=True)
            except OSError as exc:
                raise ProjectSourceIntelligenceError(
                    "project.source_intelligence_snapshot_changed",
                    "the source snapshot changed while it was captured",
                ) from exc
            if (
                root not in resolved.parents
                or source.is_symlink()
                or not resolved.is_file()
            ):
                self._fail(
                    "project.source_intelligence_snapshot_unsafe",
                    "the source snapshot contains an unsafe file",
                )
            total_bytes += before.st_size
            if total_bytes > self.maximum_snapshot_bytes:
                self._snapshot_limit()
            destination = shadow.joinpath(*PurePosixPath(relative).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                content = resolved.read_bytes()
                destination.write_bytes(content)
                after = source.stat(follow_symlinks=False)
            except OSError as exc:
                raise ProjectSourceIntelligenceError(
                    "project.source_intelligence_snapshot_changed",
                    "the source snapshot changed while it was captured",
                ) from exc
            identity = "sha256:" + hashlib.sha256(content).hexdigest()
            stable = (before.st_mode, before.st_size, before.st_mtime_ns, before.st_ino)
            if stable != (
                after.st_mode,
                after.st_size,
                after.st_mtime_ns,
                after.st_ino,
            ):
                self._fail(
                    "project.source_intelligence_snapshot_changed",
                    "the source snapshot changed while it was captured",
                )
            inventory.append((relative, stable, identity))
        return tuple(inventory)

    def _require_source_unchanged(
        self,
        root: Path,
        inventory: tuple[tuple[str, tuple[int, int, int, int], str], ...],
    ) -> None:
        if tuple(path for path, _stable, _identity in inventory) != self._source_paths(
            root
        ):
            self._fail(
                "project.source_intelligence_snapshot_changed",
                "the source inventory changed while status was observed",
            )
        for relative, expected_stable, expected_identity in inventory:
            source = root.joinpath(*PurePosixPath(relative).parts)
            try:
                stat = source.stat(follow_symlinks=False)
                content = source.read_bytes()
            except OSError as exc:
                raise ProjectSourceIntelligenceError(
                    "project.source_intelligence_snapshot_changed",
                    "the source snapshot changed while status was observed",
                ) from exc
            stable = (stat.st_mode, stat.st_size, stat.st_mtime_ns, stat.st_ino)
            identity = "sha256:" + hashlib.sha256(content).hexdigest()
            if (
                source.is_symlink()
                or stable != expected_stable
                or identity != expected_identity
            ):
                self._fail(
                    "project.source_intelligence_snapshot_changed",
                    "the source snapshot changed while status was observed",
                )

    @staticmethod
    def _copy_database_snapshot(
        root: Path, source: Path, target: Path
    ) -> tuple[str, str]:
        del root
        try:
            source_connection = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
            try:
                before_version = source_connection.execute(
                    "PRAGMA data_version"
                ).fetchone()
                target_connection = sqlite3.connect(target)
                try:
                    source_connection.backup(target_connection)
                finally:
                    target_connection.close()
                after_version = source_connection.execute(
                    "PRAGMA data_version"
                ).fetchone()
            finally:
                source_connection.close()
            live_identity = _CodeGraphCommandEngine._database_identity(source)
            snapshot = "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()
        except (OSError, sqlite3.Error) as exc:
            raise ProjectSourceIntelligenceError(
                "project.source_intelligence_unhealthy",
                "the CodeGraph database could not be snapshotted safely",
            ) from exc
        if before_version != after_version:
            _CodeGraphCommandEngine._fail(
                "project.source_intelligence_database_changed",
                "the CodeGraph database changed while it was snapshotted",
            )
        return snapshot, live_identity

    @staticmethod
    def _require_database_unchanged(database: Path, expected_identity: str) -> None:
        try:
            current = _CodeGraphCommandEngine._database_identity(database)
        except OSError as exc:
            raise ProjectSourceIntelligenceError(
                "project.source_intelligence_database_changed",
                "the CodeGraph database changed while status was observed",
            ) from exc
        if current != expected_identity:
            _CodeGraphCommandEngine._fail(
                "project.source_intelligence_database_changed",
                "the CodeGraph database changed while status was observed",
            )

    @staticmethod
    def _database_identity(database: Path) -> str:
        digest = hashlib.sha256()
        for candidate in (
            database,
            database.with_name(database.name + "-wal"),
            database.with_name(database.name + "-shm"),
        ):
            digest.update(candidate.name.encode("utf-8"))
            if not candidate.exists():
                digest.update(b"\0missing")
                continue
            if candidate.is_symlink() or not candidate.is_file():
                _CodeGraphCommandEngine._fail(
                    "project.source_intelligence_path_unsafe",
                    "the CodeGraph database has an unsafe SQLite sidecar",
                )
            digest.update(b"\0present\0")
            if not candidate.name.endswith("-shm"):
                digest.update(candidate.read_bytes())
        return "sha256:" + digest.hexdigest()

    def _status(self, root: Path) -> Mapping[str, object]:
        raw = self._run(root, "status", "--json", str(root))
        try:
            value = json.loads(raw)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ProjectSourceIntelligenceError(
                "project.source_intelligence_status_invalid",
                "the CodeGraph status command did not return JSON",
            ) from exc
        if not isinstance(value, dict):
            self._fail(
                "project.source_intelligence_status_invalid",
                "the CodeGraph status command did not return an object",
            )
        return value

    def _validated_snapshot(
        self,
        status: Mapping[str, object],
        *,
        project_path: Path,
        runtime_version: str,
        executable_identity: str,
        database_identity: str,
    ) -> _ProjectIndexSnapshot:
        if status.get("initialized") is not True:
            self._invalid_status("the CodeGraph index is not initialized")
        if status.get("projectPath") != str(project_path):
            self._invalid_status("CodeGraph status names another project root")
        if status.get("indexPath") != str(project_path / ".codegraph"):
            self._invalid_status("CodeGraph status names another index path")
        if status.get("version") != runtime_version:
            self._fail(
                "project.source_intelligence_version_mismatch",
                "CodeGraph status was produced by another runtime version",
            )
        pending = status.get("pendingChanges")
        if (
            not isinstance(pending, Mapping)
            or set(pending) != {"added", "modified", "removed"}
            or any(type(pending.get(name)) is not int for name in pending)
        ):
            self._invalid_status("CodeGraph pending-change status is invalid")
        if any(pending.get(name) != 0 for name in pending):
            self._fail(
                "project.source_intelligence_stale",
                "the CodeGraph index has pending source changes",
            )
        if status.get("worktreeMismatch") is not None:
            self._fail(
                "project.source_intelligence_stale",
                "the CodeGraph index reports worktree drift",
            )
        counts: list[int] = []
        for field in ("fileCount", "nodeCount", "edgeCount"):
            value = status.get(field)
            if type(value) is not int or value < 0:
                self._invalid_status(f"CodeGraph {field} is invalid")
            counts.append(value)
        metadata = status.get("index")
        if not isinstance(metadata, Mapping):
            self._invalid_status("CodeGraph status has no index metadata")
        extraction = metadata.get("builtWithExtractionVersion")
        current_extraction = metadata.get("currentExtractionVersion")
        built_with = metadata.get("builtWithVersion")
        if counts[0] == 0:
            valid = (
                counts[1] == 0
                and counts[2] == 0
                and built_with is None
                and extraction is None
            )
        else:
            valid = (
                built_with == runtime_version
                and type(extraction) is int
                and extraction >= 0
                and extraction == current_extraction
            )
        if not valid or metadata.get("reindexRecommended") is not False:
            self._fail(
                "project.source_intelligence_unhealthy",
                "the CodeGraph index requires a compatible full rebuild",
            )
        return _ProjectIndexSnapshot(
            runtime_version,
            executable_identity,
            counts[0],
            counts[1],
            counts[2],
            built_with if isinstance(built_with, str) else None,
            extraction if type(extraction) is int else None,
            database_identity,
        )

    def _run(self, cwd: Path, *arguments: str) -> str:
        executable = self._require_executable()
        expected_identity = self._require_executable_identity()
        stdout = bytearray()
        stderr = bytearray()
        exceeded = threading.Event()
        ownership = create_process_tree_ownership()
        try:
            process = subprocess.Popen(
                (str(executable), *arguments),
                cwd=cwd,
                env=self._environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                **ownership.popen_options,
            )
        except OSError as exc:
            ownership.release()
            raise ProjectSourceIntelligenceError(
                "project.source_intelligence_command_unavailable",
                "the configured CodeGraph command is unavailable or unusable",
            ) from exc
        ownership.bind(process.pid)

        def drain(stream, target: bytearray) -> None:
            try:
                while chunk := stream.read(64 * 1024):
                    if len(target) + len(chunk) > _MAXIMUM_COMMAND_OUTPUT_BYTES:
                        if not exceeded.is_set():
                            exceeded.set()
                            self._terminate_process_tree(process, ownership)
                        return
                    target.extend(chunk)
            finally:
                stream.close()

        assert process.stdout is not None
        assert process.stderr is not None
        threads = (
            threading.Thread(target=drain, args=(process.stdout, stdout), daemon=True),
            threading.Thread(target=drain, args=(process.stderr, stderr), daemon=True),
        )
        for thread in threads:
            thread.start()
        try:
            returncode = process.wait(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            self._terminate_process_tree(process, ownership)
            process.wait(timeout=5)
            for thread in threads:
                thread.join(timeout=5)
            ownership.release()
            raise ProjectSourceIntelligenceError(
                "project.source_intelligence_command_timeout",
                f"the CodeGraph {arguments[0]} command exceeded its timeout",
            ) from exc
        for thread in threads:
            thread.join(timeout=5)
        if any(thread.is_alive() for thread in threads):
            self._terminate_process_tree(process, ownership)
            ownership.release()
            self._fail(
                "project.source_intelligence_command_termination_failed",
                "the CodeGraph command retained output streams after termination",
            )
        if exceeded.is_set():
            ownership.release()
            self._fail(
                "project.source_intelligence_command_output_limit",
                "the CodeGraph command exceeded its output limit",
            )
        try:
            output = stdout.decode("utf-8", errors="strict")
            stderr.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            ownership.release()
            raise ProjectSourceIntelligenceError(
                "project.source_intelligence_command_output_invalid",
                "the CodeGraph command returned non-UTF-8 output",
            ) from exc
        if returncode != 0:
            ownership.release()
            self._fail(
                "project.source_intelligence_unhealthy",
                f"the CodeGraph {arguments[0]} command failed",
            )
        if self._require_executable_identity() != expected_identity:
            ownership.release()
            self._fail(
                "project.source_intelligence_command_changed",
                "the CodeGraph executable changed during use",
            )
        ownership.release()
        return output

    @staticmethod
    def _terminate_process_tree(
        process: subprocess.Popen[bytes], ownership: ProcessTreeOwnership | None = None
    ) -> None:
        terminate_process_tree(process, ownership=ownership)

    def _require_executable(self) -> Path:
        if self._executable is None:
            candidate = shutil.which(self.binary)
            if candidate is None:
                self._fail(
                    "project.source_intelligence_command_unavailable",
                    "the configured CodeGraph command is unavailable or unusable",
                )
            try:
                executable = Path(candidate).resolve(strict=True)
            except OSError as exc:
                raise ProjectSourceIntelligenceError(
                    "project.source_intelligence_command_unavailable",
                    "the configured CodeGraph command is unavailable or unusable",
                ) from exc
            if not executable.is_file() or not os.access(executable, os.X_OK):
                self._fail(
                    "project.source_intelligence_command_unavailable",
                    "the configured CodeGraph command is not a regular executable",
                )
            self._executable = executable
            try:
                self._executable_identity = (
                    "sha256:" + hashlib.sha256(executable.read_bytes()).hexdigest()
                )
            except OSError as exc:
                raise ProjectSourceIntelligenceError(
                    "project.source_intelligence_command_unavailable",
                    "the configured CodeGraph command is unavailable or unusable",
                ) from exc
        return self._executable

    def _require_executable_identity(self) -> str:
        executable = self._require_executable()
        assert self._executable_identity is not None
        try:
            current = "sha256:" + hashlib.sha256(executable.read_bytes()).hexdigest()
        except OSError as exc:
            raise ProjectSourceIntelligenceError(
                "project.source_intelligence_command_unavailable",
                "the configured CodeGraph command became unavailable",
            ) from exc
        if current != self._executable_identity:
            self._fail(
                "project.source_intelligence_command_changed",
                "the CodeGraph executable changed during use",
            )
        return current

    @staticmethod
    def _environment() -> dict[str, str]:
        environment = {
            key: os.environ[key] for key in _ENVIRONMENT_KEYS if key in os.environ
        }
        environment.update(
            {
                "CODEGRAPH_NO_DAEMON": "1",
                "LC_ALL": "C.UTF-8",
                "NO_COLOR": "1",
            }
        )
        return environment

    @staticmethod
    def _snapshot_limit() -> None:
        _CodeGraphCommandEngine._fail(
            "project.source_intelligence_snapshot_limit",
            "the project source snapshot exceeded its configured limit",
        )

    @staticmethod
    def _invalid_status(message: str) -> None:
        _CodeGraphCommandEngine._fail(
            "project.source_intelligence_status_invalid", message
        )

    @staticmethod
    def _fail(code: str, message: str) -> None:
        raise ProjectSourceIntelligenceError(code, message)


class CodeGraphProjectSourceIntelligence:
    """Apply project policy around one explicitly selected external provider."""

    def __init__(
        self,
        policy: ProjectSourceIntelligencePolicy,
        *,
        binary: str | None = None,
        engine: _ProjectIndexEngine | None = None,
        timeout_seconds: int = 600,
    ) -> None:
        if policy.provider_id != "codegraph-cli":
            raise ProjectSourceIntelligenceError(
                "project.source_intelligence_provider_unsupported",
                "the configured project source-intelligence provider is unsupported",
            )
        if policy.artifact_path != ".codegraph/codegraph.db":
            raise ProjectSourceIntelligenceError(
                "project.source_intelligence_path_unsupported",
                "CodeGraph project intelligence must use .codegraph/codegraph.db",
            )
        if engine is not None and binary is not None:
            raise ValueError("inject either a source-index engine or binary, not both")
        command = binary or policy.command
        if not command:
            raise ValueError("source-index command must not be empty")
        self.policy = policy
        self.engine = engine or _CodeGraphCommandEngine(
            command, timeout_seconds=timeout_seconds
        )

    def probe(self, working_directory: Path) -> str:
        runtime_version, _identity = self.engine.preflight(working_directory)
        return self._validated_version(runtime_version)

    def check(self, project_root: Path) -> dict[str, object]:
        return self._inspect(project_root, synchronize=False)

    def sync(self, project_root: Path) -> dict[str, object]:
        return self._inspect(project_root, synchronize=True)

    def _inspect(self, project_root: Path, *, synchronize: bool) -> dict[str, object]:
        preflight_version, _identity = self.engine.preflight(project_root)
        self._validated_version(preflight_version)
        snapshot = self.engine.capture(project_root, synchronize=synchronize)
        runtime_version = self._validated_version(snapshot.runtime_version)
        return {
            "schema": "literate-ai/project-source-intelligence-status@1",
            "state": "current",
            "provider_id": self.policy.provider_id,
            "runtime_version": runtime_version,
            "minimum_version": self.policy.minimum_version,
            "artifact_path": self.policy.artifact_path,
            "file_count": snapshot.file_count,
            "node_count": snapshot.node_count,
            "edge_count": snapshot.edge_count,
            "built_with_version": snapshot.built_with_version,
            "extraction_version": snapshot.extraction_version,
            "database_identity": snapshot.database_identity,
            "executable_identity": snapshot.executable_identity,
        }

    def _validated_version(self, value: str) -> str:
        try:
            runtime = SemanticVersion.parse(value)
            assert self.policy.minimum_version is not None
            minimum = SemanticVersion.parse(self.policy.minimum_version)
        except ValueError as exc:
            raise ProjectSourceIntelligenceError(
                "project.source_intelligence_version_invalid",
                "the CodeGraph command did not report canonical semantic versioning",
            ) from exc
        if str(runtime) != value or runtime < minimum:
            raise ProjectSourceIntelligenceError(
                "project.source_intelligence_version_unsupported",
                "the CodeGraph command is older than the configured minimum version",
            )
        return value


def require_lifecycle_project_index(
    project_root: Path,
    policy: ProjectSourceIntelligencePolicy,
    *,
    stage: SourceIntelligenceStage = SourceIntelligenceStage.PROJECT_MAINTENANCE,
    synchronize: bool = True,
) -> dict[str, object]:
    """Bind project source-intelligence evidence before an effectful lifecycle phase."""

    mode = policy.mode_for(stage)
    if policy.provider_id == "none" or mode is SourceIntelligenceMode.OFF:
        return _disabled_lifecycle_index(policy)
    if policy.provider_id != "codegraph-cli":
        if mode is SourceIntelligenceMode.REQUIRED:
            raise ProjectSourceIntelligenceError(
                "project.source_intelligence_lifecycle_required",
                "required project source intelligence selected an unsupported provider",
            )
        return _unavailable_lifecycle_index(
            policy, stage, "project.source_intelligence_provider_unsupported"
        )
    provider = CodeGraphProjectSourceIntelligence(policy)
    try:
        return (
            provider.sync(project_root) if synchronize else provider.check(project_root)
        )
    except ProjectSourceIntelligenceError as exc:
        if mode is SourceIntelligenceMode.REQUIRED:
            raise
        return _unavailable_lifecycle_index(policy, stage, exc.code)


def _unavailable_lifecycle_index(
    policy: ProjectSourceIntelligencePolicy,
    stage: SourceIntelligenceStage,
    reason_code: str,
) -> dict[str, object]:
    identity = canonical_identity(
        {
            "schema": "literate-ai/project-source-intelligence-status@1",
            "state": "unavailable",
            "provider_id": policy.provider_id,
            "stage": stage.value,
            "reason_code": reason_code,
        }
    )
    return {
        "schema": "literate-ai/project-source-intelligence-status@1",
        "state": "unavailable",
        "provider_id": policy.provider_id,
        "runtime_version": None,
        "minimum_version": policy.minimum_version,
        "artifact_path": policy.artifact_path,
        "file_count": 0,
        "node_count": 0,
        "edge_count": 0,
        "built_with_version": None,
        "extraction_version": None,
        "database_identity": identity.uri,
        "executable_identity": identity.uri,
        "reason_code": reason_code,
        "stage": stage.value,
    }


def _disabled_lifecycle_index(
    policy: ProjectSourceIntelligencePolicy,
) -> dict[str, object]:
    identity = canonical_identity(
        {
            "schema": "literate-ai/project-source-intelligence-status@1",
            "state": "off",
            "provider_id": policy.provider_id,
        }
    )
    return {
        "schema": "literate-ai/project-source-intelligence-status@1",
        "state": "off",
        "provider_id": policy.provider_id,
        "runtime_version": None,
        "minimum_version": policy.minimum_version,
        "artifact_path": policy.artifact_path,
        "file_count": 0,
        "node_count": 0,
        "edge_count": 0,
        "built_with_version": None,
        "extraction_version": None,
        "database_identity": identity.uri,
        "executable_identity": identity.uri,
    }


__all__ = [
    "CodeGraphProjectSourceIntelligence",
    "ProjectSourceIntelligenceError",
    "require_lifecycle_project_index",
]
