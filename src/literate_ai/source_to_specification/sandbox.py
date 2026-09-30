"""Fail-closed local sandbox adapter for authorized dynamic observations."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path

from literate_ai.adapters._processes import (
    create_process_tree_ownership,
    terminate_process_tree,
)
from literate_ai.adapters.user_paths import resolve_host_system_paths

from .contracts import canonical_digest
from .errors import SourceToSpecificationError
from .workflow import SourceTreeFingerprint


def _digest_harness(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _default_runtime_paths(command: tuple[str, ...], system: str) -> tuple[Path, ...]:
    candidates: list[Path] = []
    executable = shutil.which(command[0])
    if executable is None and Path(command[0]).is_absolute():
        executable = command[0]
    if executable is not None:
        configured = Path(executable).absolute()
        resolved = configured.resolve()
        candidates.append(
            configured.parents[1] if len(configured.parents) > 1 else configured
        )
        candidates.append(
            resolved.parents[1] if len(resolved.parents) > 1 else resolved
        )
    platform_family = "macos" if system == "Darwin" else "linux"
    conventional = resolve_host_system_paths(
        platform_family=platform_family
    ).sandbox_runtime_roots
    candidates.extend(Path(item) for item in conventional)
    return tuple(dict.fromkeys(path.absolute() for path in candidates if path.exists()))


def _sandbox_path_rule(operation: str, path: Path) -> str:
    escaped = _sandbox_escape(path)
    selector = "subpath" if path.is_dir() else "literal"
    return f'(allow {operation} ({selector} "{escaped}"))'


def _sandbox_escape(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace('"', '\\"')


def _sandbox_ancestor_rules(paths: Sequence[Path]) -> tuple[str, ...]:
    ancestors = sorted(
        {parent for path in paths for parent in path.parents},
        key=lambda item: (len(item.parts), str(item)),
    )
    return tuple(
        f'(allow file-read-metadata (literal "{_sandbox_escape(path)}"))'
        for path in ancestors
    )


# `ulimit -f` counts 512-byte blocks in the strict POSIX interpretation and
# 1024-byte blocks on macOS's bash-based /bin/sh -- the exact multiplier
# varies by shell/OS. Round up against the smaller (512-byte) unit so the
# resulting limit is never *tighter* than `maximum_output_bytes` on any
# POSIX shell; on a shell using the larger 1024-byte unit (e.g. macOS's
# bash-based /bin/sh) it may end up looser by up to roughly double. That is
# acceptable because this rlimit is only a defense-in-depth backstop
# against runaway disk usage during the run -- the authoritative,
# byte-exact enforcement is the post-exit `stdout_path.stat().st_size`
# check in `run()` below.
_ULIMIT_FSIZE_BLOCK_BYTES = 512


def _resource_limited_command(
    command: Sequence[str],
    *,
    shell_executable: str,
    maximum_output_bytes: int,
    timeout_seconds: float,
) -> tuple[str, ...]:
    """Wrap `command` so its own `exec` applies the FSIZE/CPU rlimits.

    `subprocess.Popen(..., preexec_fn=...)` runs arbitrary Python bytecode
    inside the `fork()`ed child of a process that may hold other threads
    (thread-pool workers, HTTPS clients, logging) at fork time. `fork()`
    only carries over the calling thread; any lock a non-forking thread held
    at that instant (notably CPython's internal allocator lock) stays held
    forever in the child. Since virtually all Python bytecode allocates,
    running `resource.setrlimit(...)` from a `preexec_fn` can deadlock the
    child before it ever reaches `exec()` -- silently and only under load
    (issue #73).

    The fix is to never run Python code between `fork()` and `exec()` at
    all. `ulimit` is a POSIX shell builtin, so wrapping the real command in
    `sh -c 'ulimit -f ... && ulimit -t ... && exec "$0" "$@"'` applies the
    same resource limits, but entirely inside the *new* process image after
    `exec()` -- there is no fork-after-threads window for it to run in.

    `exec "$0" "$@"` (with `command[0]` supplied as `$0` via `sh -c`'s
    trailing positional arguments, and the rest of `command` as `$@`)
    replaces the shell with the original harness once the limits are set,
    so the exec'd process is indistinguishable from the unwrapped command
    except for the two rlimits now bounding it.
    """

    fsize_blocks = -(-maximum_output_bytes // _ULIMIT_FSIZE_BLOCK_BYTES)
    cpu_seconds = max(1, int(timeout_seconds) + 1)
    script = f'ulimit -f {fsize_blocks} && ulimit -t {cpu_seconds} && exec "$0" "$@"'
    return (shell_executable, "-c", script, *command)


_SCRATCH_CLEANUP_ATTEMPTS = 6
_SCRATCH_CLEANUP_INITIAL_DELAY_SECONDS = 0.05


def _remove_scratch_directory(scratch: Path) -> None:
    """Remove the sandbox scratch directory, retrying past a live-handle race.

    A killed (or just-completed) child's descendants can outlive the parent's
    `wait()` for a brief window and keep files inside `scratch` open. Deleting
    the directory immediately in that window can fail (`OSError`/`WinError
    32` on Windows; a `FileNotFoundError`/`OSError` race on POSIX). Retrying
    with a short bounded backoff lets those handles close before we give up,
    without changing the fail-closed error surface if cleanup never succeeds.
    """

    delay = _SCRATCH_CLEANUP_INITIAL_DELAY_SECONDS
    last_error: OSError | None = None
    for attempt in range(_SCRATCH_CLEANUP_ATTEMPTS):
        try:
            shutil.rmtree(scratch)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            last_error = exc
            if attempt + 1 == _SCRATCH_CLEANUP_ATTEMPTS:
                break
            time.sleep(delay)
            delay *= 2
    in_flight = sys.exc_info()[1] is not None
    if in_flight:
        # An observation-path error is already propagating; a stale scratch
        # directory left under the OS temp root is a leak, not a correctness
        # risk, so it must not shadow the original failure.
        return
    assert last_error is not None
    raise SourceToSpecificationError(
        "sandbox.cleanup_failed",
        "sandbox scratch directory could not be removed after termination",
    ) from last_error


class LocalObservationSandbox:
    """Run one fixed harness through sandbox-exec or bubblewrap, never directly."""

    runner_id = "runner:local-os-sandbox@1"

    def __init__(
        self,
        *,
        source_root: str | Path,
        command: Sequence[str],
        timeout_seconds: float = 30.0,
        maximum_output_bytes: int = 1024 * 1024,
        system: str | None = None,
        system_runtime_paths: Sequence[str | Path] | None = None,
        tool_resolver=shutil.which,
    ) -> None:
        configured_source = Path(source_root)
        if configured_source.is_symlink():
            raise SourceToSpecificationError(
                "sandbox.configuration_invalid",
                "sandbox source directory cannot be a symbolic link",
            )
        self.source_root = configured_source.resolve()
        configured_command = tuple(command)
        resolved_command = (
            shutil.which(configured_command[0]) if configured_command else None
        )
        self.command = (
            (
                str(Path(resolved_command).resolve()),
                *configured_command[1:],
            )
            if resolved_command is not None
            else configured_command
        )
        self.timeout_seconds = timeout_seconds
        self.maximum_output_bytes = maximum_output_bytes
        self.system = system or platform.system()
        runtime_paths = (
            _default_runtime_paths(self.command, self.system)
            if system_runtime_paths is None
            else tuple(
                Path(item).expanduser().resolve() for item in system_runtime_paths
            )
        )
        if any(
            not path.is_absolute() or not path.exists() or path == Path(path.anchor)
            for path in runtime_paths
        ):
            raise SourceToSpecificationError(
                "sandbox.configuration_invalid",
                "sandbox runtime paths must be existing absolute paths below root",
            )
        self.system_runtime_paths = tuple(dict.fromkeys(runtime_paths))
        self._tool_resolver = tool_resolver
        if (
            not self.source_root.is_dir()
            or not self.command
            or not all(isinstance(item, str) and item for item in self.command)
        ):
            raise SourceToSpecificationError(
                "sandbox.configuration_invalid",
                "sandbox requires a source directory and fixed harness command",
            )
        if timeout_seconds <= 0 or maximum_output_bytes <= 0:
            raise SourceToSpecificationError(
                "sandbox.configuration_invalid",
                "sandbox timeout and output limit must be positive",
            )

    @property
    def harness_digest(self) -> str:
        return _digest_harness(
            {
                "command": list(self.command),
                "system": self.system,
                "runtime_paths": [str(path) for path in self.system_runtime_paths],
                "timeout_seconds": self.timeout_seconds,
                "maximum_output_bytes": self.maximum_output_bytes,
            }
        )

    @property
    def source_digest(self) -> str:
        return SourceTreeFingerprint(self.source_root).digest

    def command_for(self, scratch: str | Path) -> tuple[str, ...]:
        """Build the platform sandbox command or fail without a direct fallback."""

        configured_scratch = Path(scratch)
        if configured_scratch.is_symlink():
            raise SourceToSpecificationError(
                "sandbox.configuration_invalid",
                "sandbox scratch directory cannot be a symbolic link",
            )
        scratch_path = configured_scratch.resolve()
        protected_paths = (self.source_root, *self.system_runtime_paths)
        if any(
            scratch_path == protected
            or protected in scratch_path.parents
            or scratch_path in protected.parents
            for protected in protected_paths
        ):
            raise SourceToSpecificationError(
                "sandbox.configuration_invalid",
                "sandbox scratch directory must not overlap source or runtime paths",
            )
        if not scratch_path.is_dir():
            raise SourceToSpecificationError(
                "sandbox.configuration_invalid",
                "sandbox scratch path must be an existing directory",
            )
        if self.system == "Darwin":
            executable = self._tool_resolver("sandbox-exec")
            if executable is None:
                raise SourceToSpecificationError(
                    "sandbox.tool_unavailable", "macOS sandbox-exec is unavailable"
                )
            visible_paths = (
                self.source_root,
                *self.system_runtime_paths,
                scratch_path,
            )
            profile = "\n".join(
                (
                    "(version 1)",
                    '(import "dyld-support.sb")',
                    "(deny default)",
                    "(allow process-fork)",
                    "(allow process-exec*)",
                    *(
                        _sandbox_path_rule("file-map-executable", path)
                        for path in (self.source_root, *self.system_runtime_paths)
                    ),
                    "(deny network*)",
                    *_sandbox_ancestor_rules(visible_paths),
                    _sandbox_path_rule("file-read*", self.source_root),
                    *(
                        _sandbox_path_rule("file-read*", path)
                        for path in self.system_runtime_paths
                    ),
                    _sandbox_path_rule("file-read*", scratch_path),
                    _sandbox_path_rule("file-write*", scratch_path),
                )
            )
            return (executable, "-p", profile, *self._limited_command())
        if self.system == "Linux":
            executable = self._tool_resolver("bwrap")
            if executable is None:
                raise SourceToSpecificationError(
                    "sandbox.tool_unavailable", "Linux bubblewrap is unavailable"
                )
            system_paths = resolve_host_system_paths(platform_family="linux")
            proc_root = system_paths.bubblewrap_proc_root
            device_root = system_paths.bubblewrap_device_root
            temporary_root = system_paths.bubblewrap_temporary_root
            if proc_root is None or device_root is None or temporary_root is None:
                raise SourceToSpecificationError(
                    "sandbox.configuration_invalid",
                    "Linux bubblewrap virtual roots are unavailable",
                )
            runtime_mounts = tuple(
                item
                for path in self.system_runtime_paths
                for item in ("--ro-bind", str(path), str(path))
            )
            return (
                executable,
                "--die-with-parent",
                "--new-session",
                "--unshare-all",
                *runtime_mounts,
                "--proc",
                str(proc_root),
                "--dev",
                str(device_root),
                "--tmpfs",
                str(temporary_root),
                "--ro-bind",
                str(self.source_root),
                str(self.source_root),
                "--bind",
                str(scratch_path),
                str(scratch_path),
                "--chdir",
                str(self.source_root),
                "--clearenv",
                "--setenv",
                "PATH",
                os.defpath,
                "--setenv",
                "LANG",
                "C.UTF-8",
                *self._limited_command(),
            )
        raise SourceToSpecificationError(
            "sandbox.platform_unsupported",
            "local observation sandbox supports only macOS and Linux",
        )

    def _limited_command(self) -> tuple[str, ...]:
        """Resolve a POSIX shell and wrap `self.command` in its rlimits.

        Resolved through the same `tool_resolver` used for `sandbox-exec`/
        `bwrap` so tests can inject or withhold it identically, and checked
        after the platform sandbox tool itself so an unavailable sandbox
        tool is still reported first (unchanged diagnostic precedence).
        """

        shell_executable = self._tool_resolver("sh")
        if shell_executable is None:
            raise SourceToSpecificationError(
                "sandbox.tool_unavailable",
                "a POSIX shell is unavailable for resource-limited execution",
            )
        return _resource_limited_command(
            self.command,
            shell_executable=shell_executable,
            maximum_output_bytes=self.maximum_output_bytes,
            timeout_seconds=self.timeout_seconds,
        )

    # Bound on how long we wait for the process tree to actually die after
    # `terminate_process_tree`, before escalating to `kill()` and giving up.
    # This is cleanup after `self.timeout_seconds` has already expired, not
    # additional budget for the sandboxed observation itself.
    _TERMINATE_GRACE_SECONDS = 5

    def _await_termination_or_give_up(self, process: subprocess.Popen[bytes]) -> None:
        """Bound the post-terminate wait; never block indefinitely.

        `terminate_process_tree` is best-effort: on a host where the kill
        signal doesn't fully reach the process tree (e.g. a grandchild
        spawned after the process group was established), the process can
        still be alive afterward. A bare `process.wait()` here would then
        hang the parent forever (the other half of issue #73, alongside the
        fork-after-threads `preexec_fn` hazard). Give the tree a short grace
        period to actually exit, escalate to `kill()` once, and then give up
        bounded rather than block indefinitely.
        """

        try:
            process.wait(timeout=self._TERMINATE_GRACE_SECONDS)
            return
        except subprocess.TimeoutExpired:
            pass
        with suppress(OSError):
            process.kill()
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=self._TERMINATE_GRACE_SECONDS)

    def run(
        self,
        request: Mapping[str, object],
        authorization: Mapping[str, object],
    ) -> Mapping[str, object]:
        if request.get("runner_id") != self.runner_id:
            raise SourceToSpecificationError(
                "sandbox.runner_mismatch", "observation request targets another runner"
            )
        if request.get("harness_digest") != self.harness_digest:
            raise SourceToSpecificationError(
                "sandbox.harness_mismatch",
                "observation request targets another harness",
            )
        source_digests = request.get("source_digests")
        actual_source_digest = self.source_digest
        if (
            not isinstance(source_digests, (list, tuple))
            or actual_source_digest not in source_digests
        ):
            raise SourceToSpecificationError(
                "sandbox.source_mismatch",
                "observation request does not bind the sandbox source tree",
            )
        privileges = request.get("requested_privileges")
        if not isinstance(privileges, (list, tuple)) or set(privileges) - {"processes"}:
            raise SourceToSpecificationError(
                "sandbox.privilege_denied",
                "local observation sandbox permits only the processes privilege",
            )
        authorization_id = authorization.get("authorization_id")
        authorized_privileges = authorization.get("privileges")
        if (
            not isinstance(authorization_id, str)
            or not authorization_id
            or authorization.get("request_digest") != canonical_digest(request)
            or not isinstance(authorized_privileges, (list, tuple))
            or set(authorized_privileges) != set(privileges)
        ):
            raise SourceToSpecificationError(
                "sandbox.authorization_mismatch",
                "dynamic observation lacks an exact execution authorization",
            )
        environment = {"PATH": os.defpath, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
        # `scratch` is created with `mkdtemp` (not the `TemporaryDirectory`
        # context manager) so cleanup below can retry past a still-alive
        # child holding open handles inside it instead of raising
        # unconditionally on the first attempt (issue #80).
        scratch = Path(tempfile.mkdtemp(prefix="literate-observe-"))
        try:
            command = self.command_for(scratch)
            stdout_path = scratch / "stdout.json"
            stderr_path = scratch / "stderr.txt"

            # No `preexec_fn` here: the FSIZE/CPU resource limits are applied
            # by `command`'s own `sh -c 'ulimit ... && exec ...'` wrapper
            # (built in `_limited_command`), entirely inside the exec'd
            # process image. Running arbitrary Python between `fork()` and
            # `exec()` in a process that may already have other threads
            # (this one runs generation workers, HTTPS clients, logging) can
            # deadlock the child on an internal CPython lock a non-forking
            # thread held at fork time -- issue #73.
            try:
                ownership = create_process_tree_ownership()
                try:
                    with (
                        stdout_path.open("wb") as stdout,
                        stderr_path.open("wb") as stderr,
                    ):
                        process = subprocess.Popen(
                            command,
                            cwd=self.source_root,
                            env=environment,
                            stdin=subprocess.DEVNULL,
                            stdout=stdout,
                            stderr=stderr,
                            **ownership.popen_options,
                        )
                        ownership.bind(process.pid)
                        try:
                            returncode = process.wait(timeout=self.timeout_seconds)
                        except subprocess.TimeoutExpired:
                            terminate_process_tree(process, ownership=ownership)
                            self._await_termination_or_give_up(process)
                            raise
                finally:
                    ownership.release()
            except subprocess.TimeoutExpired as exc:
                raise SourceToSpecificationError(
                    "sandbox.timeout", "dynamic observation exceeded its timeout"
                ) from exc
            except OSError as exc:
                raise SourceToSpecificationError(
                    "sandbox.execution_unavailable",
                    "sandboxed observation could not be started",
                ) from exc
            if self.source_digest != actual_source_digest:
                raise SourceToSpecificationError(
                    "sandbox.source_mutated",
                    "sandbox source tree changed during dynamic observation",
                )
            if returncode != 0:
                raise SourceToSpecificationError(
                    "sandbox.execution_failed",
                    "sandboxed observation failed without exposing process output",
                )
            if stdout_path.stat().st_size > self.maximum_output_bytes:
                raise SourceToSpecificationError(
                    "sandbox.output_limit",
                    "dynamic observation exceeded its output limit",
                )
            try:
                value = json.loads(stdout_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise SourceToSpecificationError(
                    "sandbox.output_invalid",
                    "sandboxed observation must emit one JSON object",
                ) from exc
            if not isinstance(value, dict):
                raise SourceToSpecificationError(
                    "sandbox.output_invalid",
                    "sandboxed observation must emit one JSON object",
                )
            return value
        finally:
            _remove_scratch_directory(scratch)


__all__ = ["LocalObservationSandbox"]
