"""Private, owned sccache processes for exact compiler-cache invocations."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from literate_ai.adapters._processes import (
    create_process_tree_ownership,
    terminate_process_tree,
)
from literate_ai.adapters.builders import BuildError, run_bounded_process
from literate_ai.adapters.read_only_cache import copy_read_only_cache
from literate_ai.adapters.shared_cache import sccache_cache_plan
from literate_ai.adapters.shared_cache_config import BoundSharedCache
from literate_ai.contracts.identity import ContentIdentity
from literate_ai.contracts.shared_cache import (
    SharedCacheAccessMode,
    SharedCacheNamespace,
)


@dataclass(slots=True)
class CompilerCacheSession:
    environment: dict[str, str] = field(repr=False)
    observation: dict[str, object]


def validate_compiler_cache_observation(value: object) -> dict[str, object]:
    """Validate advisory counters; they never substitute for compiler acceptance."""
    required = {"schema", "configuration_identity", "tool_identity", "available"}
    counters = {"cache_hits", "cache_misses", "compile_requests"}
    if not isinstance(value, dict) or not required <= value.keys():
        raise ValueError("invalid compiler-cache observation")
    if (
        value["schema"] != "literate-ai/compiler-cache-observation@1"
        or type(value["available"]) is not bool
    ):
        raise ValueError("invalid compiler-cache observation")
    for key in ("configuration_identity", "tool_identity"):
        ContentIdentity.parse_uri(value[key])
    optional = value.keys() - required
    if optional not in (set(), counters, {"statistics_unavailable"}):
        raise ValueError("invalid compiler-cache statistics fields")
    if optional and not value["available"]:
        raise ValueError("unavailable compiler cache cannot report statistics")
    if optional == counters:
        for key in counters:
            if type(value[key]) is not int or not 0 <= value[key] <= 2**63 - 1:
                raise ValueError("invalid compiler-cache counter")
    if (
        "statistics_unavailable" in value
        and value["statistics_unavailable"] is not True
    ):
        raise ValueError("invalid compiler-cache statistics disposition")
    return dict(value)


def _counter(value: object) -> int:
    if type(value) is int and 0 <= value <= 2**63 - 1:
        return value
    if isinstance(value, dict):
        if "counts" in value:
            return _counter(value["counts"])
        return sum(_counter(item) for item in value.values())
    return 0


def _startup_port(path: Path) -> int | None:
    """Read sccache's bounded startup acknowledgement from our private file.

    Windows sccache accepts an ordinary file for STARTUP_NOTIFY. Let the OS select
    port zero, then admit only the port acknowledged by this owned server; probing
    an unreserved random port could accidentally select another process.
    """
    try:
        with path.open("rb") as stream:
            raw = stream.read(4097)
    except OSError:
        return None
    if not 16 <= len(raw) <= 4096 or int.from_bytes(raw[:4], "big") != len(raw) - 4:
        return None
    if int.from_bytes(raw[4:8], "little") != 0:
        return None
    if int.from_bytes(raw[8:16], "little") != len(raw) - 16:
        return None
    try:
        host, value = raw[16:].decode("ascii").rsplit(":", 1)
        port = int(value)
    except (ValueError, UnicodeError):
        return None
    return port if host == "127.0.0.1" and 1 <= port <= 65535 else None


@contextmanager
def compiler_cache_session(
    binding: BoundSharedCache,
    *,
    environment: Mapping[str, str],
    workspace: Path,
) -> Iterator[CompilerCacheSession]:
    binding.require_unchanged()
    tool = binding.compiler_tool
    if tool is None:
        raise ValueError("compiler cache requires an exact tool binding")
    controlled = {
        key: value
        for key, value in environment.items()
        if not key.upper().startswith("SCCACHE_")
        and key.upper() not in {"RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER"}
    }
    controlled.update({"RUSTC_WRAPPER": "", "RUSTC_WORKSPACE_WRAPPER": ""})
    observation = {
        "schema": "literate-ai/compiler-cache-observation@1",
        "configuration_identity": binding.identity.uri,
        "tool_identity": tool.toolchain_identity.uri,
        "available": False,
    }
    session = CompilerCacheSession(controlled, observation)
    with tempfile.TemporaryDirectory(prefix="lc-") as temporary:
        private = Path(temporary).resolve()
        config = private / "config.toml"
        config.write_text("server_startup_timeout_ms = 5000\n", encoding="utf-8")
        configured = dict(controlled)
        configured.update(
            dict(
                sccache_cache_plan(
                    binding.configuration, local_root=binding.local_root
                ).environment
            )
        )
        if (
            binding.configuration.policy(SharedCacheNamespace.COMPILER).mode
            is SharedCacheAccessMode.READ_ONLY
        ):
            view = private / "cache-view"
            copy_read_only_cache(
                Path(configured["SCCACHE_DIR"]),
                view,
                maximum_bytes=binding.configuration.maximum_bytes,
                retention_seconds=binding.configuration.retention_seconds,
            )
            configured["SCCACHE_DIR"] = str(view)
        configured.update(
            {
                "SCCACHE_CONF": str(config),
                "SCCACHE_CACHED_CONF": str(private / "cached-config"),
                "SCCACHE_IDLE_TIMEOUT": "30",
                "SCCACHE_IGNORE_SERVER_IO_ERROR": "1",
                "SCCACHE_SKIP_CACHE_CHECK": "true",
                "SCCACHE_DIRECT": "false",
                "RUSTC_WRAPPER": tool.executable,
                "RUSTC_WORKSPACE_WRAPPER": "",
                "CARGO_INCREMENTAL": "0",
            }
        )
        token = binding.credential()
        if token is not None:
            configured["SCCACHE_WEBDAV_TOKEN"] = token
        if os.name == "nt":
            notify = private / "ready"
            notify.write_bytes(b"")
            configured["SCCACHE_SERVER_PORT"] = "0"
        else:
            configured["SCCACHE_SERVER_UDS"] = str(private / "s")
        binding.require_compiler_dependencies(configured)
        ownership = create_process_tree_ownership()
        process = None
        try:
            try:
                process = subprocess.Popen(
                    tool.command,
                    cwd=workspace,
                    env=configured
                    | {"SCCACHE_START_SERVER": "1", "SCCACHE_NO_DAEMON": "1"}
                    | (
                        {"SCCACHE_STARTUP_NOTIFY": str(notify)}
                        if os.name == "nt"
                        else {}
                    ),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    **ownership.popen_options,
                )
                ownership.bind(process.pid)
                deadline = time.monotonic() + 5
                while process.poll() is None and time.monotonic() < deadline:
                    if os.name == "nt":
                        port = _startup_port(notify)
                        ready = port is not None
                        if ready:
                            configured["SCCACHE_SERVER_PORT"] = str(port)
                    else:
                        ready = (private / "s").exists()
                    if ready:
                        session.environment = configured
                        observation["available"] = True
                        break
                    time.sleep(0.01)
            except OSError:
                pass
            yield session
            if (
                observation["available"]
                and process is not None
                and process.poll() is None
            ):
                try:
                    result = run_bounded_process(
                        (*tool.command, "--show-stats", "--stats-format=json"),
                        cwd=workspace,
                        environment=configured,
                        timeout_seconds=5,
                        stdout_limit_bytes=1024 * 1024,
                        stderr_limit_bytes=4096,
                        error_prefix="shared_cache.statistics",
                    )
                    document = json.loads(result.stdout)
                    stats = (
                        document.get("stats", {}) if isinstance(document, dict) else {}
                    )
                    if result.returncode == 0 and isinstance(stats, dict):
                        observation.update(
                            {
                                "cache_hits": _counter(stats.get("cache_hits")),
                                "cache_misses": _counter(stats.get("cache_misses")),
                                "compile_requests": _counter(
                                    stats.get("compile_requests")
                                ),
                            }
                        )
                except (BuildError, ValueError):
                    observation["statistics_unavailable"] = True
        finally:
            if process is not None:
                if process.poll() is None and observation["available"]:
                    try:
                        run_bounded_process(
                            (*tool.command, "--stop-server"),
                            cwd=workspace,
                            environment=configured,
                            timeout_seconds=5,
                            stdout_limit_bytes=1024 * 1024,
                            stderr_limit_bytes=4096,
                            error_prefix="shared_cache.shutdown",
                        )
                    except BuildError:
                        pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                # The server may have exited while a compiler descendant still
                # holds resources. Dispose the owned group/job in either case.
                terminate_process_tree(
                    process, environment=configured, ownership=ownership
                )
                process.wait(timeout=5)
            ownership.release()
            binding.require_unchanged()
            binding.require_compiler_dependencies(configured)
