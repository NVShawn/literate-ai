"""Deterministic build-harness inspection for ``litai init --convert``.

Convert must inspect the quarantined legacy tree before any agent-authored
specification exists: which build system, test runner, packaging surface, supported
operating systems, and release workflow the project already has. Every finding names
its evidence path; every derived command is traceable to one detector. Nothing here
executes the detected toolchain — inspection only reads files.
"""

from __future__ import annotations

import errno
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import BinaryIO

from literate_ai.adapters._processes import (
    create_process_tree_ownership,
    terminate_process_tree,
)
from literate_ai.adapters.harness_test_discovery import (
    discover_retained_test_command,
    observe_test_collection,
)
from literate_ai.adapters.harness_tree import (
    EXCLUDED_DIRECTORY_NAMES,
    capture_retained_source_scope,
    copy_retained_source_tree,
    observe_retained_tree,
)
from literate_ai.adapters.harness_workspace import (
    HarnessWorkspaceLink,
    harness_workspace_link_evidence,
    materialize_harness_workspace_links,
    require_harness_workspace_link_evidence,
)
from literate_ai.diagnostics import redact_secrets, trace_subprocess

HARNESS_INVENTORY_SCHEMA = "urn:literate-ai:schema:v1:harness-inventory"
HARNESS_BASELINE_SCHEMA = "literate-ai/legacy-harness-baseline@2"
HARNESS_PARITY_SCHEMA = "literate-ai/legacy-wrapper-parity@2"
RETAINED_HARNESS_RUN_SCHEMA = "literate-ai/retained-harness-run@1"
HARNESS_WRAPPER_FILENAME = "litai.harness.mk"
MAX_HARNESS_OUTPUT_BYTES = 1024 * 1024
MAX_HARNESS_DIAGNOSTIC_BYTES = 256 * 1024
HARNESS_COMMAND_TIMEOUT_SECONDS = 1800
HARNESS_COMMAND_TIMEOUT_MIN_SECONDS = 1
HARNESS_COMMAND_TIMEOUT_MAX_SECONDS = 86_400
HARNESS_DIAGNOSTIC_CHARS = 8192
HARNESS_DIAGNOSTIC_MIN_CHARS = 512
HARNESS_DIAGNOSTIC_MAX_CHARS = 65_536
HARNESS_DIAGNOSTIC_ENVIRONMENT = "LITAI_CONVERT_DIAGNOSTIC_CHARS"

_OUTPUT_READ_CHUNK_BYTES = 64 * 1024
_ERROR_SCAN_SEGMENT_BYTES = 4096
_ERROR_CONTEXT_PATTERN = re.compile(
    rb"(?:fatal(?:\s+error)?|error|exception|traceback|panic|"
    rb"undefined\s+reference|failed|failure|cannot|could\s+not)",
    re.IGNORECASE,
)
_STORAGE_FAILURE_PATTERN = re.compile(
    rb"\b(?:ENOSPC|EDQUOT)\b|no space left on device|disk quota exceeded|"
    rb"not enough space on (?:the )?disk",
    re.IGNORECASE,
)

_MAKEFILES = ("Makefile", "GNUMakefile", "GNUmakefile", "makefile")
_REPO_DRIVERS = ("repo.sh", "repo.bat")
_REPO_BUILD_WRAPPERS = {"nt": "build.bat", "posix": "build.sh"}
_SKIP_DIR_NAMES = frozenset({".git", "_build", "_legacy", "node_modules"})
CONVERT_BASELINE_EXCLUDED_NAMES = EXCLUDED_DIRECTORY_NAMES
_ENV_BOUND_FRAGMENTS = (
    ".venv/",
    "venv/bin/",
    "venv/Scripts/",
    "node_modules/",
)
_REMOTE_CI_DETECTORS = frozenset({"ci.os-matrix", "ci.gitlab"})
_GITHUB_RUNNER = re.compile(
    r"runs-on:\s*['\"]?(?!\$)([\w.-]+)",
)
_MATRIX_OS_LIST = re.compile(r"(?:os|platform):\s*\[([^\]]+)\]")


def validate_harness_command_timeout(value: object) -> int:
    """Return one bounded conversion/retained-harness command deadline."""

    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not HARNESS_COMMAND_TIMEOUT_MIN_SECONDS
        <= value
        <= HARNESS_COMMAND_TIMEOUT_MAX_SECONDS
    ):
        raise ValueError(
            "harness command timeout must be an integer from "
            f"{HARNESS_COMMAND_TIMEOUT_MIN_SECONDS} to "
            f"{HARNESS_COMMAND_TIMEOUT_MAX_SECONDS} seconds"
        )
    return value


def validate_harness_diagnostic_limit(value: object) -> int:
    """Return one finite combined conversion failure-diagnostic budget."""

    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not HARNESS_DIAGNOSTIC_MIN_CHARS <= value <= HARNESS_DIAGNOSTIC_MAX_CHARS
    ):
        raise ValueError(
            "harness diagnostic limit must be an integer from "
            f"{HARNESS_DIAGNOSTIC_MIN_CHARS} to "
            f"{HARNESS_DIAGNOSTIC_MAX_CHARS} characters"
        )
    return value


def _finding(detector_id: str, path: str, detail: str) -> dict[str, str]:
    return {"detector_id": detector_id, "path": path, "detail": detail}


def _makefile_declares_target(text: str, name: str) -> bool:
    """True when `name` is a Make rule, matching how GNU Make parses a target."""

    return re.search(rf"(?m)^{re.escape(name)}\s*:", text) is not None


def _stream_size(stream: BinaryIO) -> int:
    stream.seek(0, os.SEEK_END)
    return stream.tell()


def _stream_identity(stream: BinaryIO) -> str:
    """Hash the complete file-backed stream with constant memory."""

    digest = hashlib.sha256()
    stream.seek(0)
    while chunk := stream.read(_OUTPUT_READ_CHUNK_BYTES):
        digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _bounded_stream_sample(stream: BinaryIO, *, size: int, limit: int) -> bytes:
    """Return a bounded head and physical tail for tracing and test discovery."""

    marker = b"\n[litai: bounded output omitted]\n"
    if size <= limit:
        stream.seek(0)
        return stream.read(limit)
    payload_limit = max(0, limit - len(marker))
    head_limit = payload_limit // 2
    tail_limit = payload_limit - head_limit
    stream.seek(0)
    head = stream.read(head_limit)
    stream.seek(max(0, size - tail_limit))
    tail = stream.read(tail_limit)
    return head + marker + tail


def _first_error_contexts(stream: BinaryIO, *, limit: int) -> bytes:
    """Return bounded context preceding the first error-like output lines."""

    stream.seek(0)
    contexts: list[bytes] = []
    previous: list[bytes] = []
    observed: set[bytes] = set()
    remaining = limit
    while remaining > 0:
        segment = stream.readline(_ERROR_SCAN_SEGMENT_BYTES)
        if not segment:
            break
        stripped = segment.strip()
        if stripped and _ERROR_CONTEXT_PATTERN.search(stripped):
            context = b"\n".join((*previous, stripped))
            if context not in observed:
                observed.add(context)
                selected = context[:remaining]
                contexts.append(selected)
                remaining -= len(selected) + 1
                if len(contexts) >= 4:
                    break
        if stripped:
            previous = [*previous[-1:], stripped[:_ERROR_SCAN_SEGMENT_BYTES]]
    return b"\n".join(contexts)[:limit]


def _informative_text(raw: bytes, *, limit: int, from_end: bool = False) -> str:
    text = raw.decode("utf-8", errors="replace")
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    normalized = "\n".join(lines)
    return normalized[-limit:] if from_end else normalized[:limit]


def _reported_storage_failure(stream: BinaryIO) -> str:
    """Scan file-backed output without retaining untrusted matching text."""

    stream.seek(0)
    overlap = b""
    found = False
    omitted_prefix = False
    while chunk := stream.read(_OUTPUT_READ_CHUNK_BYTES):
        window = overlap + chunk
        match = _STORAGE_FAILURE_PATTERN.search(window, int(omitted_prefix))
        # Defer a match at the read boundary until its trailing context is known.
        if match is not None and match.end() < len(window):
            found = True
            break
        # Longer than any fixed diagnostic phrase; catches chunk-boundary matches.
        overlap = window[-64:]
        omitted_prefix = omitted_prefix or len(window) > 64
    if not found and not _STORAGE_FAILURE_PATTERN.search(overlap, int(omitted_prefix)):
        return ""
    return (
        "[reported storage failure]\n"
        "Command output reports ENOSPC/disk-full or EDQUOT/quota exhaustion; "
        "this is not a capacity measurement. Check worker workspace, temp "
        "and cache free space, quotas and inodes."
    )


def _command_output_excerpt(
    stream: BinaryIO,
    *,
    size: int,
    limit: int,
) -> str:
    """Prioritize reported storage failures and the physical tail within a bound."""

    hint = _reported_storage_failure(stream)
    prefix = hint + "\n" if hint else ""
    content_limit = max(1, limit - len(prefix))

    if size <= content_limit:
        stream.seek(0)
        return (prefix + _informative_text(stream.read(), limit=content_limit))[:limit]

    markers = ("[output head]", "[first error context]", "[output tail]")
    available = max(3, content_limit - sum(len(marker) + 1 for marker in markers) - 2)
    head_limit = available // 4
    error_limit = available // 4
    tail_limit = available - head_limit - error_limit

    stream.seek(0)
    head = _informative_text(stream.read(head_limit), limit=head_limit)
    errors = _informative_text(
        _first_error_contexts(stream, limit=error_limit),
        limit=error_limit,
    )
    if not errors:
        tail_limit += error_limit
    stream.seek(max(0, size - tail_limit))
    tail = _informative_text(stream.read(tail_limit), limit=tail_limit, from_end=True)
    sections = [(markers[2], tail)]
    if errors:
        sections.append((markers[1], errors))
    sections.append((markers[0], head))
    return (
        prefix
        + "\n".join(f"{marker}\n{content}" for marker, content in sections if content)
    )[:limit]


def _gate_failure_message(stage_id: str, result: dict[str, object]) -> str:
    command = result.get("command")
    status = result.get("exit_code")
    if result.get("timed_out"):
        headline = (
            f"legacy {stage_id} gate timed out after "
            f"{result.get('timeout_seconds')} seconds ({command})"
        )
    elif not result.get("output_within_limits", True):
        headline = (
            f"legacy {stage_id} gate failed with status {status} and exceeded its "
            "bounded output policy "
            f"(stdout {result.get('stdout_size')}/{result.get('stdout_limit_bytes')} "
            f"bytes, stderr {result.get('stderr_size')}/"
            f"{result.get('stderr_limit_bytes')} bytes; {command})"
        )
    else:
        headline = f"legacy {stage_id} gate failed with status {status} ({command})"
    # Reserve the CLI's finite headline allowance for the headline and newline;
    # a long shell command must not consume the separately bounded stream tails.
    if len(headline) > 511:
        headline = headline[:508] + "..."
    excerpts = []
    labels = sorted(
        ("stderr", "stdout"),
        key=lambda label: (
            not str(result.get(f"{label}_excerpt", "")).startswith(
                "[reported storage failure]"
            )
        ),
    )
    for label in labels:
        excerpt = result.get(f"{label}_excerpt")
        if isinstance(excerpt, str) and excerpt.strip():
            excerpts.append(f"[{label}]\n{excerpt}")
    if not excerpts:
        return headline
    return f"{headline}\n" + "\n".join(excerpts)


def _command_cost(command: str) -> str:
    if "repo.sh" in command or "repo.bat" in command:
        return "host-heavy"
    return "local-cheap"


def _stage(
    stage_id: str,
    command: str,
    evidence: str,
    *,
    cwd: str = ".",
    cost: str | None = None,
) -> dict[str, str]:
    return {
        "id": stage_id,
        "command": command,
        "evidence": evidence,
        "cwd": cwd,
        "cost": _command_cost(command) if cost is None else cost,
    }


def _add_stage_if_absent(
    stages: list[dict[str, str]],
    stage_id: str,
    command: str,
    evidence: str,
    *,
    cwd: str = ".",
) -> None:
    if any(item["id"] == stage_id for item in stages):
        return
    stages.append(_stage(stage_id, command, evidence, cwd=cwd))


def _project_children(root: Path) -> list[Path]:
    try:
        children = sorted(root.iterdir(), key=lambda item: item.name)
    except OSError:
        return []
    return [
        child
        for child in children
        if child.is_dir()
        and not child.is_symlink()
        and not child.name.startswith(".")
        and child.name not in _SKIP_DIR_NAMES
    ]


def _legacy_relative_cwd(cwd: object, *, legacy_root: Path | None = None) -> str | None:
    """Return a project-relative cwd, or None when it would escape the legacy tree."""

    if not isinstance(cwd, str) or cwd in {".", ""}:
        relative = "."
    else:
        path = Path(cwd)
        if path.is_absolute() or ".." in path.parts:
            return None
        relative = path.as_posix()
    if legacy_root is not None and relative != ".":
        try:
            (legacy_root / relative).resolve().relative_to(legacy_root.resolve())
        except ValueError:
            return None
    return relative


def _regular_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def _shallow_named_files(
    root: Path, names: tuple[str, ...], *, first_match_per_dir: bool = True
) -> list[tuple[str, str]]:
    """Return ``(relative_dir, filename)`` matches at root and one child level."""

    found: list[tuple[str, str]] = []
    for name in names:
        if _regular_file(root / name):
            found.append((".", name))
            if first_match_per_dir:
                break
    for child in _project_children(root):
        for name in names:
            if _regular_file(child / name):
                found.append((child.name, name))
                if first_match_per_dir:
                    break
    return found


def _relative_marker(directory: str, name: str) -> str:
    return name if directory == "." else f"{directory}/{name}"


def repo_driver_locations(root: Path) -> list[tuple[str, str]]:
    """Return ``(relative_dir, filename)`` for repo.sh/bat at root and one child."""

    return _shallow_named_files(root, _REPO_DRIVERS)


def repo_toml_locations(root: Path) -> list[str]:
    """Return relative paths for ``repo.toml`` at root and one child level."""

    return [
        _relative_marker(directory, name)
        for directory, name in _shallow_named_files(root, ("repo.toml",))
    ]


def _repo_man_build_entrypoint(
    root: Path, *, relative: str, driver_name: str
) -> tuple[str, str]:
    """Return the authoritative build command and evidence for one repo_man root."""

    project_root = root if relative == "." else root / relative
    wrapper_name = _REPO_BUILD_WRAPPERS.get(os.name)
    if wrapper_name is not None:
        wrapper = project_root / wrapper_name
        runnable = _regular_file(wrapper) and (
            os.name == "nt" or os.access(wrapper, os.X_OK)
        )
        if runnable:
            prefix = ".\\" if os.name == "nt" else "./"
            return f"{prefix}{wrapper_name} --release", wrapper_name
    prefix = ".\\" if os.name == "nt" and driver_name.endswith(".bat") else "./"
    return f"{prefix}{driver_name} build --release", driver_name


_BAZEL_WORKSPACE_MARKERS = ("MODULE.bazel", "WORKSPACE", "WORKSPACE.bazel")


def bazel_marker_path(root: Path) -> str | None:
    """Return a Bazel workspace marker at root or one child directory."""

    found = _shallow_named_files(root, _BAZEL_WORKSPACE_MARKERS)
    if not found:
        return None
    return _relative_marker(*found[0])


def has_bazel_markers(root: Path) -> bool:
    """True when Bazel manifests exist at the root or one child project directory."""

    return bazel_marker_path(root) is not None


def source_language_markers(root: Path) -> set[str]:
    """Return detected source languages without requiring a full-tree count."""

    found: set[str] = set()
    for _dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            name
            for name in dirnames
            if name not in _SKIP_DIR_NAMES and not name.startswith(".")
        ]
        for name in filenames:
            lower = name.lower()
            if lower.endswith(".py"):
                found.add("python")
            elif lower.endswith((".c", ".cpp", ".cc", ".cxx", ".cu", ".cuh")):
                found.add("cpp")
            elif lower.endswith((".ex", ".exs")):
                found.add("elixir")
        if {"python", "cpp", "elixir"} <= found:
            break
    return found


def classify_ci(
    findings: list[object], commands: dict[str, object]
) -> dict[str, object]:
    """Classify CI as local-executable, remote-configured, or not-configured."""

    remote = any(
        isinstance(item, dict) and item.get("detector_id") in _REMOTE_CI_DETECTORS
        for item in findings
    )
    local = isinstance(commands.get("ci"), dict)
    if local:
        return {
            "configured": True,
            "class": "local-executable",
            "state": "local-executable",
        }
    if remote:
        return {
            "configured": True,
            "class": "remote-configured",
            "state": "remote-configured",
        }
    return {"configured": False, "class": "not-configured", "state": "not-configured"}


def _commands_from_stages(stages: list[dict[str, str]]) -> dict[str, object]:
    commands: dict[str, object] = {}
    for stage in stages:
        key = stage["id"]
        if "." in key:
            continue
        commands[key] = {
            "command": stage["command"],
            "evidence": stage["evidence"],
            "cwd": stage.get("cwd", "."),
            "cost": stage.get("cost", _command_cost(stage["command"])),
            "id": key,
        }
    return commands


def _inventory_stages(inventory: dict[str, object]) -> list[dict[str, str]]:
    raw = inventory.get("stages")
    if isinstance(raw, list) and raw:
        stages: list[dict[str, str]] = []
        for item in raw:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                continue
            command = item.get("command")
            evidence = item.get("evidence")
            if not isinstance(command, str) or not isinstance(evidence, str):
                continue
            stages.append(
                _stage(
                    item["id"],
                    command,
                    evidence,
                    cwd=str(item.get("cwd") or "."),
                    cost=str(item.get("cost") or _command_cost(command)),
                )
            )
        if stages:
            return stages
    commands = inventory.get("commands")
    stages = []
    if isinstance(commands, dict):
        for phase in ("build", "test", "package", "run", "ci"):
            entry = commands.get(phase)
            if not isinstance(entry, dict):
                continue
            command = entry.get("command")
            evidence = entry.get("evidence")
            if not isinstance(command, str) or not isinstance(evidence, str):
                continue
            stages.append(
                _stage(
                    phase,
                    command,
                    evidence,
                    cwd=str(entry.get("cwd") or "."),
                    cost=str(entry.get("cost") or _command_cost(command)),
                )
            )
    return stages


def _python_module_from_command(command: str) -> str | None:
    tokens = command.split()
    for index, token in enumerate(tokens[:-2]):
        if token in {"python", "python3"} or token.endswith("python3"):
            if tokens[index + 1] == "-m":
                return tokens[index + 2]
    return None


def _stage_driver_available(command: str) -> bool:
    """Return True when the recorded command's driver is importable or on PATH."""

    module = _python_module_from_command(command)
    if module is not None:
        if module == "unittest":
            return True
        try:
            return importlib.util.find_spec(module) is not None
        except (ModuleNotFoundError, ValueError):
            return False
    first = command.strip().split(None, 1)[0] if command.strip() else ""
    if not first:
        return False
    if first.startswith("./") or first.startswith(".\\"):
        return True
    return shutil.which(first) is not None


_EXECUTABLE_BASELINE_PHASES = frozenset({"build", "test", "package", "ci"})


def _require_recorded_stage(stage: dict[str, str]) -> None:
    command = stage.get("command")
    evidence = stage.get("evidence")
    if not isinstance(command, str) or not command.strip():
        raise HarnessBaselineError(
            "project.convert_command_invalid",
            "harness command must be a nonempty string",
            {"schema": HARNESS_BASELINE_SCHEMA, "state": "failed", "phases": []},
        )
    if not isinstance(evidence, str) or not evidence.strip():
        raise HarnessBaselineError(
            "project.convert_evidence_missing",
            "harness command must cite evidence",
            {"schema": HARNESS_BASELINE_SCHEMA, "state": "failed", "phases": []},
        )


def convert_copy_ignore(_directory: str, names: list[str]) -> list[str]:
    """Omit local environments and caches from convert baseline copies."""

    return [name for name in names if name in CONVERT_BASELINE_EXCLUDED_NAMES]


def command_is_environment_bound(command: str) -> bool:
    """True when a recorded command names a local interpreter or package tree."""

    return any(
        fragment in command.replace("\\", "/") for fragment in _ENV_BOUND_FRAGMENTS
    )


def _parse_makefile_goals(text: str) -> dict[str, dict[str, list[str]]]:
    """Map each explicit Make goal to its prerequisites and recipe lines.

    A goal may be defined across several rule lines; prerequisites and recipes
    accumulate. Special targets (names beginning with ``.`` such as ``.PHONY``),
    variable assignments (``:=``/``::=``), and target-specific variable settings
    are not treated as buildable goals or prerequisites.
    """

    goals: dict[str, dict[str, list[str]]] = {}
    current: list[str] = []
    for raw_line in text.splitlines():
        if raw_line.startswith("\t"):
            recipe = raw_line[1:]
            for goal in current:
                goals[goal]["recipes"].append(recipe)
            continue
        stripped = raw_line.split("#", 1)[0].rstrip()
        current = []
        if ":" not in stripped:
            continue
        head, _, tail = stripped.partition(":")
        if tail.startswith("="):
            # ``target := value`` variable assignment, not a rule.
            continue
        if tail.startswith(":"):
            tail = tail[1:]
            if tail.startswith("="):
                # ``target ::= value`` variable assignment, not a rule.
                continue
        names = [name for name in head.split() if not name.startswith(".")]
        if not names:
            continue
        # A real prerequisite never contains ``=``; drop target-specific
        # variable settings such as ``target: VAR = value``.
        prerequisites = [token for token in tail.split() if "=" not in token]
        for goal in names:
            entry = goals.setdefault(goal, {"prereqs": [], "recipes": []})
            entry["prereqs"].extend(prerequisites)
            current.append(goal)
    return goals


def makefile_recipe_is_environment_bound(
    makefile_path: Path, *, goal: str = "all"
) -> bool:
    """True when the named Make goal, or any goal it depends on, names a local
    environment.

    Aggregate targets commonly delegate real work to prerequisites (for example
    ``test: test-python test-node``), so the whole transitive prerequisite
    closure is inspected rather than only the named goal's own recipe.
    """

    try:
        text = makefile_path.read_text(encoding="utf-8")
    except OSError:
        return False
    goals = _parse_makefile_goals(text)
    seen: set[str] = set()
    pending = [goal]
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        entry = goals.get(current)
        if entry is None:
            continue
        if any(
            command_is_environment_bound(recipe.rstrip()) for recipe in entry["recipes"]
        ):
            return True
        pending.extend(entry["prereqs"])
    return False


def _make_goal(command: str) -> str:
    tokens = command.split()
    rest = tokens[1:]
    index = 0
    while index < len(rest):
        token = rest[index]
        if token in {"-f", "--file", "--makefile"} and index + 1 < len(rest):
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        return token
    return "all"


def _require_not_environment_bound(command: str, legacy_root: Path) -> None:
    """Fail closed when a recorded gate still names a host-local environment."""

    bound = command_is_environment_bound(command)
    if not bound:
        first = command.strip().split(None, 1)[0] if command.strip() else ""
        if first in {"make", "gmake", "mingw32-make"}:
            goal = _make_goal(command)
            bound = any(
                makefile_recipe_is_environment_bound(legacy_root / name, goal=goal)
                for name in _MAKEFILES
                if (legacy_root / name).is_file()
            )
    if not bound:
        return
    raise HarnessBaselineError(
        "project.convert_environment_bound_command",
        "recorded command names a host-local environment "
        f"({', '.join(_ENV_BOUND_FRAGMENTS)}): {command}",
        {
            "schema": HARNESS_BASELINE_SCHEMA,
            "state": "failed",
            "phases": [],
            "command": command,
        },
    )


def stages_requiring_execution(
    inventory: dict[str, object], *, run_baseline: bool
) -> list[dict[str, str]]:
    """Return stages convert should execute during the recorded baseline."""

    to_run, _omitted = _partition_execution_stages(inventory, run_baseline=run_baseline)
    return to_run


def stages_covered_by_root(inventory: dict[str, object]) -> frozenset[str]:
    """Return nested repo_man build stages covered by its recorded root build.

    Repo-man discovery keeps every shallow project root as a manually callable
    wrapper target.  When the same inventory also has the repository's undotted
    build entrypoint, that entrypoint is the automatic baseline and retained-run
    authority: nested repo drivers are internal stages of that root orchestration,
    not additional standalone qualification commands.  Restrict the relationship to
    inventories carrying the repo-man finding and to ``build.*`` so an arbitrary
    dotted test or build stage is never hidden merely because its name is dotted.
    """

    findings = inventory.get("findings")
    if not isinstance(findings, list) or not any(
        isinstance(item, dict) and item.get("detector_id") == "build-system.repo-man"
        for item in findings
    ):
        return frozenset()
    stages = _inventory_stages(inventory)
    if not any(stage["id"] == "build" for stage in stages):
        return frozenset()
    return frozenset(
        stage["id"] for stage in stages if stage["id"].startswith("build.")
    )


def _partition_execution_stages(
    inventory: dict[str, object], *, run_baseline: bool
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Split executable stages into run vs recorded-skip lists."""

    to_run: list[dict[str, str]] = []
    omitted: list[dict[str, str]] = []
    covered = stages_covered_by_root(inventory)
    for stage in _inventory_stages(inventory):
        phase = stage["id"].split(".", 1)[0]
        if phase not in _EXECUTABLE_BASELINE_PHASES:
            continue
        if stage["id"] in covered:
            omitted.append({**stage, "reason": "covered-by-root-stage"})
            continue
        cost = stage.get("cost") or _command_cost(stage["command"])
        if cost == "host-heavy" and not run_baseline:
            omitted.append({**stage, "reason": "host-heavy"})
            continue
        if not _stage_driver_available(stage["command"]):
            omitted.append({**stage, "reason": "driver-unavailable"})
            continue
        to_run.append(stage)
    return to_run, omitted


def _detect_build_system(
    root: Path,
) -> tuple[list[dict[str, str]], dict[str, object], list[dict[str, str]]]:
    findings: list[dict[str, str]] = []
    stages: list[dict[str, str]] = []

    repo_locs = repo_driver_locations(root)
    if repo_locs:
        first_dir, first_name = repo_locs[0]
        evidence = first_name if first_dir == "." else f"{first_dir}/{first_name}"
        findings.append(
            _finding(
                "build-system.repo-man",
                evidence,
                "repo.sh / packman project",
            )
        )
        for relative, name in repo_locs:
            stage_id = "build" if relative == "." else f"build.{relative}"
            command, entrypoint = _repo_man_build_entrypoint(
                root, relative=relative, driver_name=name
            )
            path = entrypoint if relative == "." else f"{relative}/{entrypoint}"
            # Baselines run with CI=true, and supported repo_build versions
            # require an explicit configuration in CI. Release is the
            # deterministic conversion baseline configuration. A repository-owned
            # build wrapper is authoritative because it may establish prerequisites
            # before delegating to the lower-level repo_man driver.
            stages.append(
                _stage(stage_id, command, path, cwd=relative, cost="host-heavy")
            )
        for toml_path in repo_toml_locations(root):
            findings.append(
                _finding("build-system.repo-toml", toml_path, "repo_man configuration")
            )

    makefile = next((name for name in _MAKEFILES if (root / name).is_file()), None)
    cmake = root / "CMakeLists.txt"
    if cmake.is_file():
        findings.append(
            _finding("build-system.cmake", "CMakeLists.txt", "CMake project")
        )
        if not repo_locs:
            stages.append(
                _stage(
                    "build",
                    "cmake -S . -B build && cmake --build build",
                    "CMakeLists.txt",
                )
            )
            stages.append(
                _stage("test", "ctest --test-dir build -C Release", "CMakeLists.txt")
            )
    elif makefile is not None:
        findings.append(_finding("build-system.make", makefile, "GNU Make project"))
        if not repo_locs:
            stages.append(_stage("build", f"make -f {makefile}", makefile))
            make_text = (root / makefile).read_text(encoding="utf-8", errors="replace")
            if _makefile_declares_target(make_text, "test"):
                stages.append(_stage("test", f"make -f {makefile} test", makefile))
            if _makefile_declares_target(make_text, "package"):
                stages.append(
                    _stage("package", f"make -f {makefile} package", makefile)
                )
            if _makefile_declares_target(make_text, "ci"):
                stages.append(_stage("ci", f"make -f {makefile} ci", makefile))

    bazel_evidence = bazel_marker_path(root)
    if bazel_evidence is not None:
        findings.append(
            _finding("build-system.bazel", bazel_evidence, "Bazel workspace")
        )
        if not stages:
            stages.append(_stage("build", "bazel build //...", bazel_evidence))
            stages.append(_stage("test", "bazel test //...", bazel_evidence))

    if (root / "Cargo.toml").is_file():
        findings.append(_finding("language.rust", "Cargo.toml", "Rust package"))
        _add_stage_if_absent(stages, "build", "cargo build", "Cargo.toml")
        _add_stage_if_absent(stages, "test", "cargo test", "Cargo.toml")
        _add_stage_if_absent(stages, "run", "cargo run", "Cargo.toml")
        _add_stage_if_absent(stages, "package", "cargo package --list", "Cargo.toml")

    if (root / "go.mod").is_file():
        findings.append(_finding("language.go", "go.mod", "Go module"))
        _add_stage_if_absent(stages, "build", "go build ./...", "go.mod")
        _add_stage_if_absent(stages, "test", "go test ./...", "go.mod")
        _add_stage_if_absent(stages, "run", "go run .", "go.mod")

    package_json = root / "package.json"
    if package_json.is_file():
        try:
            scripts = json.loads(package_json.read_text(encoding="utf-8")).get(
                "scripts",
                {},
            )
        except (OSError, ValueError):
            scripts = {}
        findings.append(
            _finding(
                "language.javascript",
                "package.json",
                f"Node package with scripts: {', '.join(sorted(scripts)) or 'none'}",
            )
        )
        _add_stage_if_absent(stages, "build", "npm run build", "package.json")
        _add_stage_if_absent(stages, "test", "npm test", "package.json")
        _add_stage_if_absent(stages, "run", "npm start", "package.json")
        _add_stage_if_absent(stages, "package", "npm pack", "package.json")

    pyproject = root / "pyproject.toml"
    setup_cfg = root / "setup.cfg"
    requirements = root / "requirements.txt"
    python_package = next(
        (path.name for path in (pyproject, setup_cfg) if path.is_file()),
        None,
    )
    python_manifest = python_package or (
        requirements.name if requirements.is_file() else None
    )
    if python_manifest is not None:
        findings.append(_finding("language.python", python_manifest, "Python package"))
        if python_package is not None:
            build_command = "python -m build"
            if pyproject.is_file() and "[tool.uv]" in pyproject.read_text(
                encoding="utf-8", errors="replace"
            ):
                build_command = "uv build"
            _add_stage_if_absent(stages, "build", build_command, python_package)
        test_command = discover_retained_test_command(root)
        if test_command is not None:
            findings.append(
                _finding(
                    test_command.detector_id,
                    test_command.evidence,
                    test_command.detail,
                )
            )
            if not any(item["id"] == "test" for item in stages):
                stages.append(
                    _stage(
                        "test",
                        test_command.command,
                        test_command.evidence,
                        cwd=test_command.cwd,
                        cost=test_command.cost,
                    )
                )
        elif (root / "tests").is_dir():
            findings.append(
                _finding(
                    "test-runner.unproven",
                    "tests",
                    "test tree present but no authoritative root test command proven",
                )
            )

    return findings, _commands_from_stages(stages), stages


def _detect_packaging(root: Path) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    markers = (
        ("packaging.python-wheel", "pyproject.toml"),
        ("packaging.python-requirements", "requirements.txt"),
        ("packaging.conda", "environment.yml"),
        ("packaging.containerfile", "Dockerfile"),
    )
    for detector_id, name in markers:
        if (root / name).is_file():
            findings.append(
                _finding(detector_id, name, f"packaging marker present: {name}")
            )
    return findings


def _detect_operating_systems(root: Path) -> list[dict[str, str]]:
    """Extract supported OS hints from CI workflow matrices, textually."""

    findings: list[dict[str, str]] = []
    workflows = root / ".github" / "workflows"
    if not workflows.is_dir():
        return findings
    for workflow in sorted(workflows.glob("*.y*ml")):
        try:
            text = workflow.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        runners = sorted(set(_GITHUB_RUNNER.findall(text)))
        matrix_oses = sorted(
            {
                item.strip().strip("'\"")
                for match in _MATRIX_OS_LIST.finditer(text)
                for item in match.group(1).split(",")
                if item.strip().strip("'\"")
            }
        )
        observed = sorted({*runners, *matrix_oses})
        uses_matrix = "matrix.os" in text or "${{ matrix.os }}" in text
        relative = workflow.relative_to(root).as_posix()
        if observed:
            findings.append(
                _finding(
                    "ci.os-matrix",
                    relative,
                    "runner/matrix entries: " + ", ".join(observed),
                )
            )
        elif uses_matrix:
            findings.append(
                _finding(
                    "ci.os-matrix",
                    relative,
                    "GitHub Actions matrix OS (remote CI)",
                )
            )
        else:
            findings.append(
                _finding(
                    "ci.os-matrix",
                    relative,
                    "GitHub Actions workflow (remote CI)",
                )
            )
    return findings


def _detect_gitlab_ci(root: Path) -> list[dict[str, str]]:
    path = root / ".gitlab-ci.yml"
    if not path.is_file() or path.is_symlink():
        return []
    return [
        _finding("ci.gitlab", ".gitlab-ci.yml", "GitLab CI (remote CI)"),
    ]


def _detect_release_workflow(root: Path) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    workflows = root / ".github" / "workflows"
    if workflows.is_dir():
        for workflow in sorted(workflows.glob("*.y*ml")):
            try:
                text = workflow.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if re.search(r"\brelease\b", text, re.IGNORECASE) and re.search(
                r"\b(tag|publish|deploy)\b", text, re.IGNORECASE
            ):
                findings.append(
                    _finding(
                        "release.workflow",
                        workflow.relative_to(root).as_posix(),
                        "release/publish workflow detected",
                    )
                )
    if (root / "CHANGELOG.md").is_file():
        findings.append(
            _finding("release.changelog", "CHANGELOG.md", "changelog maintained")
        )
    return findings


def inspect_harness(legacy_root: Path) -> dict[str, object]:
    """Return one versioned harness-inventory document for a legacy tree."""

    if not legacy_root.is_dir():
        raise ValueError("legacy_root must be an existing directory")
    build_findings, commands, stages = _detect_build_system(legacy_root)
    findings = [
        *build_findings,
        *_detect_packaging(legacy_root),
        *_detect_operating_systems(legacy_root),
        *_detect_gitlab_ci(legacy_root),
        *_detect_release_workflow(legacy_root),
    ]
    source_scope = capture_retained_source_scope(
        legacy_root,
        required_paths=tuple(stage["evidence"] for stage in stages),
    )
    return {
        "schema": HARNESS_INVENTORY_SCHEMA,
        "findings": findings,
        "commands": commands,
        "stages": stages,
        "ci": classify_ci(findings, commands),
        "source_scope": source_scope,
    }


class HarnessBaselineError(RuntimeError):
    """The quarantined legacy project could not establish a passing baseline."""

    def __init__(self, code: str, message: str, report: dict[str, object]) -> None:
        self.code = code
        self.message = message
        self.report = report
        super().__init__(f"{code}: {message}")


_DISPOSABLE_GIT_IDENTITY_NAME = "Literate AI Disposable Harness"
_DISPOSABLE_GIT_IDENTITY_EMAIL = "disposable-harness@literate-ai.invalid"
_DISPOSABLE_GIT_IDENTITY_DATE = "1970-01-01T00:00:00Z"


def _sanitized_git_environment() -> dict[str, str]:
    """A Git environment scoped to one command: no host config or credentials.

    Mirrors the sanitization already used for source-materialization Git reads
    (``GIT_CONFIG_NOSYSTEM``/``GIT_CONFIG_GLOBAL``) so a disposable harness root
    never inherits the host's Git identity, credential helpers, or hooks.
    """

    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    environment["GIT_AUTHOR_NAME"] = _DISPOSABLE_GIT_IDENTITY_NAME
    environment["GIT_AUTHOR_EMAIL"] = _DISPOSABLE_GIT_IDENTITY_EMAIL
    environment["GIT_AUTHOR_DATE"] = _DISPOSABLE_GIT_IDENTITY_DATE
    environment["GIT_COMMITTER_NAME"] = _DISPOSABLE_GIT_IDENTITY_NAME
    environment["GIT_COMMITTER_EMAIL"] = _DISPOSABLE_GIT_IDENTITY_EMAIL
    environment["GIT_COMMITTER_DATE"] = _DISPOSABLE_GIT_IDENTITY_DATE
    return environment


def _ensure_disposable_git_identity(observed_root: Path) -> None:
    """Give one disposable harness phase root its own deterministic Git identity.

    Nested linbuild-style tooling resolves a build revision with ``git
    rev-parse`` and, failing that inside the phase root, may walk up to the
    first ancestor ``.git`` it finds -- selecting and mounting the outer
    checkout instead of the disposable phase root being observed (issue
    #315). A phase root copied out of quarantine carries no Git metadata of
    its own, so give it one: a local repository, scoped to exactly this
    root, with no host credentials, global config, or hooks, and one
    deterministic commit so ``git rev-parse HEAD`` always resolves to a
    valid revision.
    """

    if (observed_root / ".git").exists():
        return
    git_environment = _sanitized_git_environment()
    common_arguments = (
        "-c",
        f"core.hooksPath={os.devnull}",
        "-C",
        str(observed_root),
    )
    try:
        subprocess.run(
            ("git", *common_arguments, "init", "--quiet"),
            env=git_environment,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            (
                "git",
                *common_arguments,
                "config",
                "user.name",
                _DISPOSABLE_GIT_IDENTITY_NAME,
            ),
            env=git_environment,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            (
                "git",
                *common_arguments,
                "config",
                "user.email",
                _DISPOSABLE_GIT_IDENTITY_EMAIL,
            ),
            env=git_environment,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ("git", *common_arguments, "add", "--all"),
            env=git_environment,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            (
                "git",
                *common_arguments,
                "commit",
                "--quiet",
                "--allow-empty",
                "--no-gpg-sign",
                "-m",
                "literate-ai disposable harness baseline",
            ),
            env=git_environment,
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise HarnessBaselineError(
            "project.convert_disposable_git_identity_failed",
            "could not initialize a sanitized Git identity in the disposable "
            "harness phase root",
            {"schema": HARNESS_BASELINE_SCHEMA, "state": "failed", "phases": []},
        ) from exc


def _run_harness_command(
    phase: str,
    entry: dict[str, object],
    *,
    legacy_root: Path,
    timeout_seconds: int,
    diagnostic_limit_chars: int = HARNESS_DIAGNOSTIC_CHARS,
    observation_root: Path | None = None,
    source_scope: object | None = None,
) -> dict[str, object]:
    diagnostic_limit_chars = validate_harness_diagnostic_limit(diagnostic_limit_chars)
    command = entry.get("command")
    evidence = entry.get("evidence")
    if not isinstance(command, str) or not command.strip():
        raise HarnessBaselineError(
            "project.convert_command_invalid",
            "harness command must be a nonempty string",
            {"schema": HARNESS_BASELINE_SCHEMA, "state": "failed", "phases": []},
        )
    if not isinstance(evidence, str) or not evidence.strip():
        raise HarnessBaselineError(
            "project.convert_evidence_missing",
            "harness command must cite evidence",
            {"schema": HARNESS_BASELINE_SCHEMA, "state": "failed", "phases": []},
        )
    working_directory = legacy_root
    relative_cwd = _legacy_relative_cwd(entry.get("cwd"), legacy_root=legacy_root)
    if relative_cwd is None:
        raise HarnessBaselineError(
            "project.convert_cwd_escapes_legacy",
            "harness cwd escapes the legacy root",
            {"schema": HARNESS_BASELINE_SCHEMA, "state": "failed", "phases": []},
        )
    if relative_cwd != ".":
        working_directory = legacy_root / relative_cwd
    observed_root = observation_root or legacy_root
    _ensure_disposable_git_identity(observed_root)
    environment = _sanitized_git_environment()
    environment.update({"CI": "true", "NO_COLOR": "1"})
    # GitLab and nested container builders (linbuild, repo_man, ...) use
    # CI_PROJECT_DIR/OMNI_REPO_ROOT as the workspace mount source and Git
    # identity boundary. Project both, unconditionally, at the disposable
    # phase root being observed -- never the converted project root or an
    # inherited host value -- so a nested build selects and mounts the phase
    # root instead of the outer checkout (issue #315).
    environment["CI_PROJECT_DIR"] = str(observed_root)
    environment["OMNI_REPO_ROOT"] = str(observed_root)
    started = datetime.now(UTC)
    with (
        tempfile.TemporaryFile() as stdout_file,
        tempfile.TemporaryFile() as stderr_file,
    ):
        trace_subprocess((command,), cwd=working_directory, environment=environment)
        ownership = create_process_tree_ownership()
        try:
            process = subprocess.Popen(
                command,
                cwd=working_directory,
                env=environment,
                shell=True,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                **ownership.popen_options,
            )
        except OSError as exc:
            ownership.release()
            if exc.errno in {errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)}:
                raise HarnessBaselineError(
                    "project.host_storage_exhausted",
                    "Starting the retained harness reported exhausted space or quota. "
                    "Check worker workspace, temp and cache free space, quotas and "
                    "inodes before retrying.",
                    {
                        "schema": HARNESS_BASELINE_SCHEMA,
                        "state": "failed",
                        "phases": [],
                    },
                ) from exc
            raise HarnessBaselineError(
                "project.convert_command_unavailable",
                f"legacy {phase} command could not start",
                {"schema": HARNESS_BASELINE_SCHEMA, "state": "failed", "phases": []},
            ) from exc
        ownership.bind(process.pid)
        timed_out = False
        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            terminate_process_tree(
                process, environment=environment, ownership=ownership
            )
            process.wait(timeout=10)
        finally:
            ownership.release()
        stdout_file.flush()
        stderr_file.flush()
        stdout_size = _stream_size(stdout_file)
        stderr_size = _stream_size(stderr_file)
        populated_streams = sum(size > 0 for size in (stdout_size, stderr_size))
        label_allowance = 18 * populated_streams
        excerpt_allowance = max(1, diagnostic_limit_chars - label_allowance)
        per_stream_limit = max(1, excerpt_allowance // max(1, populated_streams))
        stdout_identity = _stream_identity(stdout_file)
        stderr_identity = _stream_identity(stderr_file)
        stdout_excerpt = _command_output_excerpt(
            stdout_file,
            size=stdout_size,
            limit=per_stream_limit,
        )
        stderr_excerpt = _command_output_excerpt(
            stderr_file,
            size=stderr_size,
            limit=per_stream_limit,
        )
        stdout = _bounded_stream_sample(
            stdout_file,
            size=stdout_size,
            limit=MAX_HARNESS_OUTPUT_BYTES,
        )
        stderr = _bounded_stream_sample(
            stderr_file,
            size=stderr_size,
            limit=MAX_HARNESS_DIAGNOSTIC_BYTES,
        )
    # Redaction can expand short matched values. Retain the priority ordering and
    # enforce the same per-stream bound after replacing sensitive text.
    stdout_excerpt = redact_secrets(stdout_excerpt, environment)[:per_stream_limit]
    stderr_excerpt = redact_secrets(stderr_excerpt, environment)[:per_stream_limit]
    observed_scope = source_scope or capture_retained_source_scope(observed_root)
    tree_observation = observe_retained_tree(observed_root, observed_scope)
    result = {
        "phase": phase,
        "command": command,
        "evidence": evidence,
        "exit_code": process.returncode,
        "timed_out": timed_out,
        "timeout_seconds": timeout_seconds,
        "stdout_size": stdout_size,
        "stderr_size": stderr_size,
        "stdout_identity": stdout_identity,
        "stderr_identity": stderr_identity,
        "stdout_excerpt": stdout_excerpt,
        "stderr_excerpt": stderr_excerpt,
        "stdout_limit_bytes": MAX_HARNESS_OUTPUT_BYTES,
        "stderr_limit_bytes": MAX_HARNESS_DIAGNOSTIC_BYTES,
        "diagnostic_limit_chars": diagnostic_limit_chars,
        "output_within_limits": (
            stdout_size <= MAX_HARNESS_OUTPUT_BYTES
            and stderr_size <= MAX_HARNESS_DIAGNOSTIC_BYTES
        ),
        "duration_milliseconds": max(
            0, int((datetime.now(UTC) - started).total_seconds() * 1000)
        ),
        "test_collection": observe_test_collection(phase, stdout, stderr),
        **tree_observation,
    }
    trace_subprocess(
        (command,),
        cwd=working_directory,
        environment=environment,
        status=process.returncode,
        stdout=stdout[:MAX_HARNESS_OUTPUT_BYTES],
        stderr=stderr[:MAX_HARNESS_DIAGNOSTIC_BYTES],
        started_at=started,
    )
    return result


def execute_harness_baseline(
    inventory: dict[str, object],
    *,
    legacy_root: Path,
    timeout_seconds: int = HARNESS_COMMAND_TIMEOUT_SECONDS,
    diagnostic_limit_chars: int = HARNESS_DIAGNOSTIC_CHARS,
    run_baseline: bool = False,
) -> dict[str, object]:
    """Execute local-cheap legacy quality gates and return digest-only evidence."""

    timeout_seconds = validate_harness_command_timeout(timeout_seconds)
    diagnostic_limit_chars = validate_harness_diagnostic_limit(diagnostic_limit_chars)

    commands = inventory.get("commands")
    findings = inventory.get("findings")
    if not isinstance(commands, dict) or not isinstance(findings, list):
        raise HarnessBaselineError(
            "project.convert_inventory_invalid",
            "harness inventory is malformed",
            {"schema": HARNESS_BASELINE_SCHEMA, "state": "failed", "phases": []},
        )
    for stage in _inventory_stages(inventory):
        phase = stage["id"].split(".", 1)[0]
        if phase not in _EXECUTABLE_BASELINE_PHASES:
            continue
        _require_recorded_stage(stage)
    ci_info = classify_ci(findings, commands)
    to_run, omitted = _partition_execution_stages(inventory, run_baseline=run_baseline)
    omitted_records = [
        {
            "id": stage["id"],
            "command": stage["command"],
            "reason": stage["reason"],
        }
        for stage in omitted
    ]
    workspace_links = inventory.get("workspace_links")
    if not to_run:
        skipped = {
            "schema": HARNESS_BASELINE_SCHEMA,
            "state": "skipped",
            "phases": [],
            "phase_count": 0,
            "omitted": omitted_records,
            "ci": {**ci_info, "state": ci_info["class"]},
            "observation": "skipped",
            "timeout_seconds": timeout_seconds,
            "diagnostic_limit_chars": diagnostic_limit_chars,
        }
        if workspace_links is not None:
            skipped["workspace_links"] = workspace_links
        return skipped
    for stage in to_run:
        _require_not_environment_bound(stage["command"], legacy_root)
    source_scope = inventory.get("source_scope") or capture_retained_source_scope(
        legacy_root,
        required_paths=tuple(stage["evidence"] for stage in to_run),
    )
    before = observe_retained_tree(legacy_root, source_scope)
    report: dict[str, object] = {
        "schema": HARNESS_BASELINE_SCHEMA,
        "state": "running",
        "timeout_seconds": timeout_seconds,
        "diagnostic_limit_chars": diagnostic_limit_chars,
        "source_scope": source_scope,
        "legacy_tree_before": before["tree"],
        "legacy_source_tree_before": before["source_tree"],
        "phases": [],
        "omitted": omitted_records,
        "ci": {**ci_info, "state": "pending"},
    }
    if workspace_links is not None:
        report["workspace_links"] = workspace_links
    phases = report["phases"]
    assert isinstance(phases, list)
    for stage in to_run:
        try:
            result = _run_harness_command(
                stage["id"],
                stage,
                legacy_root=legacy_root,
                timeout_seconds=timeout_seconds,
                diagnostic_limit_chars=diagnostic_limit_chars,
                source_scope=source_scope,
            )
        except HarnessBaselineError as exc:
            report["state"] = "failed"
            report["phase_count"] = len(phases)
            raise HarnessBaselineError(exc.code, exc.message, report) from exc
        phases.append(result)
        test_collection = result.get("test_collection")
        if (
            isinstance(test_collection, dict)
            and test_collection.get("state") == "empty"
            and result["exit_code"] == 0
        ):
            report["state"] = "failed"
            raise HarnessBaselineError(
                "project.convert_empty_test_suite",
                f"legacy {stage['id']} gate reported zero collected tests",
                report,
            )
        if result["timed_out"] or result["exit_code"] != 0:
            report["state"] = "failed"
            if stage["id"] == "ci" or stage["id"].startswith("ci."):
                report["ci"] = {**ci_info, "state": "failed"}
                raise HarnessBaselineError(
                    "project.convert_ci_failed",
                    _gate_failure_message("ci", result),
                    report,
                )
            raise HarnessBaselineError(
                "project.convert_legacy_gate_failed",
                _gate_failure_message(str(stage["id"]), result),
                report,
            )
    if ci_info["class"] == "local-executable":
        report["ci"] = {**ci_info, "state": "passed"}
    elif ci_info["class"] == "remote-configured":
        report["ci"] = {**ci_info, "state": "recorded"}
    else:
        report["ci"] = {**ci_info, "state": "not-configured"}
    after = observe_retained_tree(legacy_root, source_scope)
    report["legacy_tree_after"] = after["tree"]
    report["legacy_source_tree_after"] = after["source_tree"]
    report["state"] = "passed"
    report["phase_count"] = len(phases)
    return report


def execute_retained_harness(
    inventory: dict[str, object],
    *,
    legacy_root: Path,
    timeout_seconds: int = HARNESS_COMMAND_TIMEOUT_SECONDS,
    diagnostic_limit_chars: int = HARNESS_DIAGNOSTIC_CHARS,
) -> dict[str, object]:
    """Execute every admitted retained quality gate for receipt evidence.

    The inventory remains the command authority. Unlike conversion-time baseline
    discovery, receipt execution is strict: an unavailable admitted driver, an absent
    test gate, or a test gate without exact non-empty all-passing counts cannot produce
    release evidence.
    """

    covered = stages_covered_by_root(inventory)
    admitted = tuple(
        stage
        for stage in _inventory_stages(inventory)
        if stage["id"].split(".", 1)[0] in _EXECUTABLE_BASELINE_PHASES
        and stage["id"] not in covered
    )
    if not any(stage["id"].split(".", 1)[0] == "test" for stage in admitted):
        raise HarnessBaselineError(
            "retained_receipt.test_stage_missing",
            "retained receipt requires an admitted test stage",
            {
                "schema": RETAINED_HARNESS_RUN_SCHEMA,
                "state": "failed",
                "phases": [],
            },
        )
    runnable = tuple(stages_requiring_execution(inventory, run_baseline=True))
    runnable_ids = {stage["id"] for stage in runnable}
    unavailable = tuple(
        stage["id"] for stage in admitted if stage["id"] not in runnable_ids
    )
    if unavailable:
        raise HarnessBaselineError(
            "retained_receipt.driver_unavailable",
            "admitted retained harness drivers are unavailable: "
            + ", ".join(unavailable),
            {
                "schema": RETAINED_HARNESS_RUN_SCHEMA,
                "state": "failed",
                "phases": [],
                "unavailable": list(unavailable),
            },
        )
    report = execute_harness_baseline(
        inventory,
        legacy_root=legacy_root,
        timeout_seconds=timeout_seconds,
        diagnostic_limit_chars=diagnostic_limit_chars,
        run_baseline=True,
    )
    report["schema"] = RETAINED_HARNESS_RUN_SCHEMA
    if report.get("state") != "passed":
        raise HarnessBaselineError(
            "retained_receipt.harness_not_passed",
            "retained harness did not complete every admitted quality gate",
            report,
        )
    phases = report.get("phases")
    assert isinstance(phases, list)
    expected_source_tree = report.get("legacy_source_tree_before")
    if not isinstance(expected_source_tree, dict):
        raise HarnessBaselineError(
            "retained_receipt.source_observation_missing",
            "retained harness did not capture its initial authored-source identity",
            report,
        )
    for phase in phases:
        if (
            not isinstance(phase, dict)
            or phase.get("source_tree") != expected_source_tree
        ):
            raise HarnessBaselineError(
                "retained_receipt.source_mutated",
                "retained harness changed authored source while producing evidence",
                report,
            )
        if (
            not isinstance(phase, dict)
            or str(phase.get("phase", "")).split(".", 1)[0] != "test"
        ):
            continue
        collection = phase.get("test_collection")
        if not isinstance(collection, dict) or collection.get("state") != "nonempty":
            raise HarnessBaselineError(
                "retained_receipt.test_count_unreported",
                "retained test stage did not report an exact non-empty test count",
                report,
            )
        nonpassing = sum(
            int(collection.get(name) or 0)
            for name in ("failed", "skipped", "known_failed")
        )
        passed = collection.get("passed")
        total = collection.get("total")
        if (
            nonpassing
            or not isinstance(passed, int)
            or not isinstance(total, int)
            or passed != total
        ):
            raise HarnessBaselineError(
                "retained_receipt.tests_not_all_passing",
                "retained test stage includes failed, skipped, or known-failure "
                "outcomes",
                report,
            )
    if report.get("legacy_source_tree_after") != expected_source_tree:
        raise HarnessBaselineError(
            "retained_receipt.source_mutated",
            "retained harness changed authored source while producing evidence",
            report,
        )
    return report


def execute_harness_wrapper_parity(
    inventory: dict[str, object],
    baseline: dict[str, object],
    *,
    project_root: Path,
    legacy_root: Path,
    timeout_seconds: int = HARNESS_COMMAND_TIMEOUT_SECONDS,
    diagnostic_limit_chars: int = HARNESS_DIAGNOSTIC_CHARS,
    workspace_links: tuple[HarnessWorkspaceLink, ...] = (),
) -> dict[str, object]:
    """Run the generated wrapper and compare each stage with direct baseline output."""

    timeout_seconds = validate_harness_command_timeout(timeout_seconds)
    diagnostic_limit_chars = validate_harness_diagnostic_limit(diagnostic_limit_chars)
    require_harness_workspace_link_evidence(
        inventory.get("workspace_links"), workspace_links
    )

    baseline_phases = baseline.get("phases")
    if not isinstance(baseline_phases, list):
        raise HarnessBaselineError(
            "project.convert_inventory_invalid",
            "legacy baseline has no phases",
            {
                "schema": HARNESS_PARITY_SCHEMA,
                "state": "failed",
                "phases": [],
            },
        )
    if baseline.get("state") == "skipped" or not baseline_phases:
        skipped = {
            "schema": HARNESS_PARITY_SCHEMA,
            "state": "skipped",
            "baseline_schema": baseline.get("schema"),
            "phases": [],
            "phase_count": 0,
            "observation": "skipped",
            "timeout_seconds": timeout_seconds,
            "diagnostic_limit_chars": diagnostic_limit_chars,
        }
        if inventory.get("workspace_links") is not None:
            skipped["workspace_links"] = harness_workspace_link_evidence(
                workspace_links
            )
        return skipped
    wrapper = project_root / HARNESS_WRAPPER_FILENAME
    if not wrapper.is_file() or wrapper.is_symlink():
        raise HarnessBaselineError(
            "project.convert_wrapper_unavailable",
            "legacy wrapper is unavailable",
            {
                "schema": HARNESS_PARITY_SCHEMA,
                "state": "failed",
                "phases": [],
            },
        )
    report: dict[str, object] = {
        "schema": HARNESS_PARITY_SCHEMA,
        "state": "running",
        "timeout_seconds": timeout_seconds,
        "diagnostic_limit_chars": diagnostic_limit_chars,
        "baseline_schema": baseline.get("schema"),
        "phases": [],
    }
    if inventory.get("workspace_links") is not None:
        report["workspace_links"] = harness_workspace_link_evidence(workspace_links)
    parity_phases = report["phases"]
    assert isinstance(parity_phases, list)
    with tempfile.TemporaryDirectory(
        prefix=".literate-ai-convert-wrapper-",
        dir=project_root,
    ) as parity_directory:
        workspace_root = Path(parity_directory)
        parity_root = workspace_root / "legacy"
        source_scope = baseline.get("source_scope") or inventory.get("source_scope")
        if source_scope is None:
            source_scope = capture_retained_source_scope(legacy_root)
        copy_retained_source_tree(legacy_root, parity_root, source_scope)
        with materialize_harness_workspace_links(workspace_root, workspace_links):
            for baseline_phase in baseline_phases:
                if not isinstance(baseline_phase, dict):
                    raise HarnessBaselineError(
                        "project.convert_inventory_invalid",
                        "legacy baseline phase is malformed",
                        report,
                    )
                phase = baseline_phase.get("phase")
                if not isinstance(phase, str):
                    raise HarnessBaselineError(
                        "project.convert_inventory_invalid",
                        "legacy baseline phase lacks its name",
                        report,
                    )
                wrapper_entry = {
                    "command": (
                        f"make -f {wrapper.as_posix()} {phase} "
                        f"LITAI_LEGACY={parity_root.as_posix()}"
                    ),
                    "evidence": HARNESS_WRAPPER_FILENAME,
                }
                observed = _run_harness_command(
                    phase,
                    wrapper_entry,
                    legacy_root=project_root,
                    observation_root=parity_root,
                    timeout_seconds=timeout_seconds,
                    diagnostic_limit_chars=diagnostic_limit_chars,
                    source_scope=source_scope,
                )
                artifact_tree_equal = observed["tree"] == baseline_phase.get("tree")
                equal = observed["exit_code"] == baseline_phase.get(
                    "exit_code"
                ) and observed["source_tree"] == baseline_phase.get("source_tree")
                if phase.split(".", 1)[0] == "test":
                    equal = equal and observed["test_collection"] == baseline_phase.get(
                        "test_collection"
                    )
                parity_phases.append(
                    {
                        **observed,
                        "baseline_tree": baseline_phase.get("tree"),
                        "baseline_source_tree": baseline_phase.get("source_tree"),
                        # Build trees can embed temporary absolute paths, timestamps,
                        # or tool IDs. Preserve exact comparison as evidence, but gate
                        # parity on success plus unchanged source authority.
                        "artifact_tree_equal": artifact_tree_equal,
                        "parity": equal,
                    }
                )
                if not equal:
                    report["state"] = "failed"
                    baseline_source = baseline_phase.get("source_tree")
                    observed_source = observed.get("source_tree")
                    raise HarnessBaselineError(
                        "project.convert_wrapper_parity_failed",
                        (
                            f"legacy wrapper {phase} result differs from the direct "
                            f"baseline (exit {observed['exit_code']} vs "
                            f"{baseline_phase.get('exit_code')}; source "
                            f"{observed_source} vs {baseline_source})"
                        ),
                        report,
                    )
    report["state"] = "passed"
    report["phase_count"] = len(parity_phases)
    return report


def legacy_shim_authority(
    inventory: dict[str, object],
    baseline: dict[str, object],
) -> dict[str, str]:
    """Render the minimal first-class authority wrapping one proven legacy pipeline."""

    commands = inventory.get("commands")
    if not isinstance(commands, dict):
        raise HarnessBaselineError(
            "project.convert_inventory_invalid",
            "harness inventory has no commands",
            {"schema": HARNESS_BASELINE_SCHEMA, "state": "failed", "phases": []},
        )
    executed: set[str] = set()
    baseline_phases = baseline.get("phases")
    if isinstance(baseline_phases, list):
        for item in baseline_phases:
            if isinstance(item, dict) and isinstance(item.get("phase"), str):
                executed.add(str(item["phase"]))
    proven = tuple(
        phase for phase in ("build", "test", "package", "ci") if phase in executed
    )
    recorded = tuple(
        phase for phase in ("build", "test", "package", "ci") if phase in commands
    )
    unproven = tuple(phase for phase in recorded if phase not in executed)
    missing = tuple(
        phase for phase in ("build", "test", "package") if phase not in commands
    )
    proven_list = ", ".join(f"`{phase}`" for phase in proven) or "none"
    unproven_list = ", ".join(f"`{phase}`" for phase in unproven) or "none"
    missing_list = ", ".join(f"`{phase}`" for phase in missing) or "none"
    component = f"""---
namespace: legacy-adoption
version: 1.0.0
display_name: Legacy Project Pipeline Wrapper
profiles:
  - application
  - adoption-shim
sample: false
provides:
  - name: legacy.pipeline
    version: 1.0.0
requires: []
authoring_inputs:
  - kind: specification-to-source-skill
    uri: skills/specification-to-source/legacy-project-shim/SKILL.md
workflow_definition: workflows/legacy-adoption/workflow.md
routing_policy: routing/legacy-adoption.json
flavor_slots:
  - slot_id: build-system
    axis: build.system
    cardinality: exactly-one
    capability_contract: legacy.pipeline
entrypoints:
  - name: run
    kind: portable-application
    path: litai.harness.mk
acceptance_contracts: []
source_dependencies: []
---
# Legacy Project Pipeline Wrapper

This Component exposes the quarantined project's proven pipeline through the
top-level `litai.harness.mk` boundary. Proven stages (direct baseline ran) are
{proven_list}; recorded but not executed are {unproven_list}; missing stages are
{missing_list} and fail loudly rather than becoming silent no-ops.

### Requirement: Preserve the direct baseline

Every proven wrapper stage SHALL exit successfully and produce the same observed
legacy-tree identity as the corresponding direct Phase 1 baseline stage recorded in
`.literate/legacy-harness-baseline.json`. Recorded stages that were not executed
(host-heavy or missing driver) have no baseline identity to match.

#### Scenario: Wrapper stage matches direct execution

- **WHEN** a proven stage is invoked through `litai.harness.mk`
- **THEN** its exit status and resulting legacy-tree identity match the direct baseline
"""
    skill = """---
name: "legacy-project-shim"
description: >
  Generate and maintain wrappers around a Phase 1-qualified legacy project pipeline.
  Use only during literate-ai project adoption.
metadata:
  author: "Literate AI maintainers <literate-ai-maintainers@users.noreply.github.com>"
schema: "urn:literate-ai:schema:v1:specification-to-source-skill"
skill_id: "legacy-project-shim"
version: "1.0.0"
title: "Legacy project pipeline shim"
stages:
  - "generate"
dependencies: []
limitations:
  - "Invoke only commands recorded in .literate/harness-inventory.json."
  - "Never weaken or skip a baseline stage; missing stages must fail loudly."
  - "Do not delete quarantined source before Phase 1.2 parity is recorded."
trust: "repository-reviewed"
---
# Legacy project pipeline shim

Treat the recorded command, evidence path, exit status, log digests, and resulting
tree identity as the wrapper contract. Generate only delegation glue; do not replace
legacy implementation behavior during Phase 1.1.
"""
    flavor = """---
schema: "literate-ai/flavor-markdown@1"
namespace: "legacy-adoption"
name: "build-legacy-shim"
version: "1.0.0"
display_name: "Qualified legacy build harness"
primary_axis: "build.system"
target: "legacy-shim"
secondary_constraints: []
applicable_capabilities:
  - "legacy.pipeline"
provides:
  - name: "build.policy.legacy-shim"
    version: "1.0.0"
    contract: null
requires: []
specification_roots:
  - "openspec/spec.md"
authoring_inputs:
  - kind: "specification-to-source-skill"
    uri: "../../skills/specification-to-source/legacy-project-shim/SKILL.md"
contributions: []
conflicts: []
co_requisites: []
order_before: []
order_after: []
---
# Qualified legacy build harness

Select this Flavor only for the Phase 1.1 wrapper Component.
"""
    flavor_spec = """# Qualified legacy build harness

### Requirement: Delegate only to qualified commands

The wrapper SHALL invoke only commands recorded by Phase 1 analysis and SHALL compare
their resulting tree identities with the direct baseline before adoption proceeds.

#### Scenario: Wrapper parity is established

- **WHEN** all available wrapper stages complete
- **THEN** every stage has exact successful parity evidence
"""
    workflow = (
        files("literate_ai.project_template")
        .joinpath("workflows", "production", "staging", "dev", "workflow.md")
        .read_text(encoding="utf-8")
        .replace('workflow_id: "dev"', 'workflow_id: "legacy-adoption"')
        .replace("# Dev\n", "# Legacy adoption wrapper\n")
        .replace(
            "The default named workflow selected when a Component does not point at a "
            "different\none. This workflow defines the ordered model and guarded "
            "lifecycle stages.",
            "Plan and generate delegation glue only after the direct legacy baseline "
            "passes.",
        )
    )
    routing = (
        json.dumps(
            {
                "schema": "literate-ai/generation-routing@1",
                "routing_id": "legacy-adoption",
                "version": "1.0.0",
                "group_id": "legacy-adoption",
                "required_locality": None,
                "fallback_allowed": False,
                "data_egress": "source-allowed",
            },
            indent=2,
        )
        + "\n"
    )
    return {
        "components/legacy-project-wrapper/component.md": component,
        "skills/specification-to-source/legacy-project-shim/SKILL.md": skill,
        "flavors/legacy-project-shim/flavor.md": flavor,
        "flavors/legacy-project-shim/openspec/spec.md": flavor_spec,
        "workflows/legacy-adoption/workflow.md": workflow,
        "routing/legacy-adoption.json": routing,
    }


def render_harness_wrapper(
    inventory: dict[str, object],
    *,
    legacy_directory: str,
    legacy_root: Path | None = None,
) -> str:
    """Render the top-level literate-ai wrapper Makefile from inventory evidence."""

    commands: dict[str, object] = inventory.get("commands", {})  # type: ignore[arg-type]
    if not isinstance(commands, dict):
        commands = {}
    stages = _inventory_stages(inventory)
    stages_by_id = {stage["id"]: stage for stage in stages}
    lines = [
        "# Generated by litai init --convert from .literate/harness-inventory.json",
        "# evidence. Framework-owned: litai update reconciles this file.",
        f"LITAI_LEGACY := {legacy_directory}",
        "",
    ]
    workspace_links = inventory.get("workspace_links")
    if isinstance(workspace_links, dict) and workspace_links.get("destinations"):
        lines.extend(
            (
                "ifeq ($(origin LITAI_LEGACY),file)",
                "$(error this retained harness requires external workspace links; "
                "run it through 'litai project test-receipt run-retained "
                "--harness-workspace-link ...' or supply an already projected "
                "LITAI_LEGACY root)",
                "endif",
                "",
            )
        )
    emitted: list[str] = []
    for target in ("build", "test", "run", "package"):
        entry = commands.get(target)
        if not isinstance(entry, dict):
            entry = stages_by_id.get(target)
        lines.append(f"{target}:")
        if isinstance(entry, dict) and entry.get("command"):
            lines.append(_wrapper_recipe(entry, legacy_root=legacy_root))
        else:
            lines.append(
                f"\t$(error no {target} command was detected during conversion; "
                f"record one in .literate/harness-inventory.json and regenerate)"
            )
        emitted.append(target)
    extra = [stage for stage in stages if stage["id"] not in emitted]
    for stage in extra:
        lines.append(f"{stage['id']}:")
        lines.append(_wrapper_recipe(stage, legacy_root=legacy_root))
        emitted.append(stage["id"])
    lines.extend(["", f".PHONY: {' '.join(emitted)}", ""])
    return "\n".join(lines)


def _wrapper_recipe(
    entry: dict[str, object], *, legacy_root: Path | None = None
) -> str:
    command = str(entry["command"])
    relative = _legacy_relative_cwd(entry.get("cwd"), legacy_root=legacy_root)
    if relative is None:
        return (
            "\t$(error harness cwd escapes the legacy tree; "
            "record a project-relative cwd in .literate/harness-inventory.json)"
        )
    if relative == ".":
        return f"\tcd $(LITAI_LEGACY) && {command}"
    return f"\tcd $(LITAI_LEGACY)/{relative} && {command}"


def _move_tree_entry(
    project_root: Path,
    source: Path,
    destination: Path,
    *,
    prefer_git: bool,
) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    moved_with_git = False
    if prefer_git and (project_root / ".git").exists():
        completed = subprocess.run(
            (
                "git",
                "-C",
                str(project_root),
                "mv",
                source.relative_to(project_root).as_posix(),
                destination.relative_to(project_root).as_posix(),
            ),
            capture_output=True,
        )
        moved_with_git = completed.returncode == 0
    if not moved_with_git:
        shutil.move(str(source), str(destination))
    return "git" if moved_with_git else "fs"


def render_lift_shift_adr(
    *,
    implementation_directory: str,
    baseline_identity: str,
    parity_identity: str,
) -> str:
    return f"""# ADR 0001: Retain Legacy Source Authority During Lift-and-Shift

- Status: Accepted for adoption
- Date: {datetime.now(UTC).date().isoformat()}
- Decision owners: project maintainers
- Baseline evidence: `{baseline_identity}`
- Shim parity evidence: `{parity_identity}`

## Context

Phase 1 proved the original project end-to-end. Phase 1.1 proved that Literate AI can
drive the same build, test, package, and CI stages through first-class wrapper
source-to-specification transfer has occurred.

## Decision

Move the complete original tree, preserving its internal taxonomy, from quarantine to
`{implementation_directory}`. The wrapper Component owns this retained implementation
and `litai.harness.mk` invokes it at its new location. Each move uses `git mv` when the
entry is tracked and plain filesystem move otherwise.

The move is admitted only when the post-move wrapper workflow has the same successful
exit and source-authority tree identity as the Phase 1 direct baseline. Only then is
the empty quarantine directory removed. The retained implementation is not generated
source, not a cache, and not a specification; it remains release authority until a
separate Phase 2 ADR qualifies native Components, Flavors, skills, and assets.

## Consequences

- `components/legacy-project-wrapper/implementation/` is intentionally load-bearing
  retained source and may not be cleaned as a build product.
- Build products still belong outside authority and remain fungible.
- Future migration items replace one retained surface at a time, prove parity, then
  remove its pre-adoption implementation with `git rm` or `rm`.
- Phase 2 owns source-to-specification and native rewrite; this ADR makes no semantic
  equivalence claim beyond E2E parity.
"""


def render_native_rewrite_adr(
    *, implementation_directory: str, lift_shift_identity: str
) -> str:
    return f"""# ADR 0002: Rewrite Retained Implementation as Native
# Literate AI Authority

- Status: Proposed
- Date: {datetime.now(UTC).date().isoformat()}
- Decision owners: project maintainers
- Phase 1.2 evidence: `{lift_shift_identity}`

## Context

The original project now lives at `{implementation_directory}` and passes the same
build, test, package, and CI workflow through first-class Literate AI shims. That move
proved project reorganization only. The retained implementation remains release

## Decision

Run a second, independently reviewed migration program that replaces retained
implementation surfaces with native Literate AI Components, Flavors, skills, and
assets. Work proceeds one public boundary at a time:

1. Inventory the boundary from retained source, tests, docs, build metadata, and the
   Phase 1 evidence without treating implementation detail as product intent.
2. Author or refine a native Component specification and its named interface/data
   contracts. Target-specific behavior belongs in Flavors; conversion practice belongs
   in exact skills; immutable non-code inputs become assets.
3. Generate a candidate in the normal Literate AI lifecycle and compare its build,
   tests, package outputs, and observable behavior with the retained baseline.
4. Transfer release authority only after independent acceptance and regenerative
   qualification. Until then the retained source remains authoritative.
5. Remove the replaced retained files with `git rm` when tracked or `rm` otherwise,
   rerun the complete workflow, and record parity before beginning the next boundary.

## Guardrails

- This ADR does not claim source disposability before regenerative qualification.
- Generated source stays in the accepted source cache; objects and fetched binaries
  stay under the object root and never enter Git.
- No target-specific Dockerfile, package-manager, OS, or toolchain instruction is
  copied into Component prose; those remain Flavor authority.
- A failed replacement restores the retained files and keeps their authority state.
- Publication and deletion remain separately authorized operations.

## Completion

The rewrite is complete only when every retained boundary has either transferred to a
qualified native Component or is documented as intentionally retained, the full E2E
workflow matches the Phase 1.2 baseline, and `{implementation_directory}` contains no
unclassified remainder.
"""


def render_native_rewrite_roadmap(*, implementation_directory: str) -> str:
    return f"""# Native rewrite program

- **Status:** active
- **Owning queue item:** [ADOPT-002](active-work.md#adopt-002)
- **Completion / archival evidence:** pending while ADOPT-002 remains open

The retained implementation at `{implementation_directory}` remains source-authority
until each boundary earns transfer under ADR 0002.

## Ordered work

1. [ ] Produce a boundary inventory: deployables, public interfaces, data schemas,
   runtime state, build targets, tests, packages, CI/release jobs, assets, and target
   variance. Every row names source/test/document evidence and an owner.
2. [ ] Classify each row as Component intent, Flavor requirement, conversion skill,
   immutable asset, repository-only harness, or intentionally retained source.
3. [ ] Select the smallest independently buildable/testable boundary and author its
   native authority; leave later boundaries retained.
4. [ ] Generate, build, test, execute, and package the candidate through the ordinary
   lifecycle; compare observable results with the Phase 1.2 parity record.
5. [ ] Independently accept and regeneratively qualify the candidate before authority
   transfer.
6. [ ] Remove only the replaced retained files (`git rm`/`rm`), rerun the full E2E
   workflow, record evidence, then repeat from step 3.
7. [ ] Prove no unclassified retained file remains; archive this program only after
   full parity and current project verification.
"""


def prepare_native_rewrite_program(
    lift_shift: dict[str, object], *, project_root: Path
) -> dict[str, object]:
    """Create Phase 2 decision and roadmap authority without rewriting source."""

    implementation = lift_shift.get("implementation_directory")
    if (
        not isinstance(implementation, str)
        or not (project_root / implementation).is_dir()
    ):
        raise ValueError("native rewrite requires a completed lift-and-shift")
    lift_shift_identity = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(lift_shift, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
    )
    adr_relative = "docs/decisions/0002-native-literate-ai-rewrite.md"
    roadmap_relative = "docs/roadmap/native-rewrite-program.md"
    (project_root / adr_relative).write_text(
        render_native_rewrite_adr(
            implementation_directory=implementation,
            lift_shift_identity=lift_shift_identity,
        ),
        encoding="utf-8",
        newline="\n",
    )
    (project_root / roadmap_relative).write_text(
        render_native_rewrite_roadmap(implementation_directory=implementation),
        encoding="utf-8",
        newline="\n",
    )
    active = project_root / "docs" / "roadmap" / "active-work.md"
    active_text = active.read_text(encoding="utf-8")
    item = f"""

<a id="adopt-002"></a>
### [ ] ADOPT-002 — Native Literate AI rewrite

- **Owner:** project architecture / Components / Flavors / skills / assets
- **Direction:** Execute
  [ADR 0002](../decisions/0002-native-literate-ai-rewrite.md) through the
  [native rewrite program](native-rewrite-program.md), preserving retained source
  authority until each boundary independently qualifies.
- **Next action:** Produce the evidence-linked boundary inventory for
  `{implementation}`.
- **Evidence:** Phase 1.2 lift-and-shift identity `{lift_shift_identity}`.
"""
    if "### [ ] ADOPT-002" not in active_text:
        active.write_text(
            active_text.rstrip("\n") + item, encoding="utf-8", newline="\n"
        )
    docs_readme = project_root / "docs" / "README.md"
    links = (
        "- [Native rewrite decision](decisions/0002-native-literate-ai-rewrite.md)\n"
        "- [Native rewrite program](roadmap/native-rewrite-program.md)"
    )
    if docs_readme.is_file():
        readme = docs_readme.read_text(encoding="utf-8")
    else:
        readme = "# Project guide\n"
    if "decisions/0002-native-literate-ai-rewrite.md" not in readme:
        docs_readme.write_text(
            readme.rstrip("\n") + "\n" + links + "\n",
            encoding="utf-8",
            newline="\n",
        )
    return {
        "schema": "literate-ai/native-rewrite-program@1",
        "state": "planned",
        "retained_authority": implementation,
        "lift_shift_identity": lift_shift_identity,
        "adr": adr_relative,
        "roadmap": roadmap_relative,
        "next_action": "boundary-inventory",
        "source_to_specification_started": False,
    }


def lift_shift_legacy_project(
    inventory: dict[str, object],
    baseline: dict[str, object],
    phase_11_parity: dict[str, object],
    *,
    project_root: Path,
    quarantine: dict[str, object],
    timeout_seconds: int = HARNESS_COMMAND_TIMEOUT_SECONDS,
    diagnostic_limit_chars: int = HARNESS_DIAGNOSTIC_CHARS,
    workspace_links: tuple[HarnessWorkspaceLink, ...] = (),
) -> dict[str, object]:
    """Move retained legacy authority into its wrapper Component and prove parity."""

    directory = quarantine.get("directory")
    moved = quarantine.get("moved")
    if not isinstance(directory, str) or not isinstance(moved, list):
        raise ValueError("lift-and-shift requires an exact quarantine manifest")
    legacy_root = project_root / directory
    implementation = (
        project_root / "components" / "legacy-project-wrapper" / "implementation"
    )
    if implementation.exists() or not legacy_root.is_dir():
        raise ValueError("lift-and-shift source or destination is unavailable")
    implementation.mkdir(parents=True)
    move_records: list[dict[str, str]] = []
    try:
        for item in sorted(moved, key=lambda value: str(value.get("from", ""))):
            if not isinstance(item, dict) or not isinstance(item.get("from"), str):
                raise ValueError("quarantine move record is malformed")
            name = str(item["from"])
            source = legacy_root / name
            destination = implementation / name
            mode = _move_tree_entry(
                project_root,
                source,
                destination,
                prefer_git=item.get("vcs") == "git",
            )
            move_records.append(
                {
                    "from": source.relative_to(project_root).as_posix(),
                    "to": destination.relative_to(project_root).as_posix(),
                    "vcs": mode,
                }
            )
        wrapper = project_root / HARNESS_WRAPPER_FILENAME
        original_wrapper = wrapper.read_text(encoding="utf-8")
        implementation_relative = implementation.relative_to(project_root).as_posix()
        wrapper.write_text(
            original_wrapper.replace(
                f"LITAI_LEGACY := {directory}",
                f"LITAI_LEGACY := {implementation_relative}",
            ),
            encoding="utf-8",
            newline="\n",
        )
        parity = execute_harness_wrapper_parity(
            inventory,
            baseline,
            project_root=project_root,
            legacy_root=implementation,
            timeout_seconds=timeout_seconds,
            diagnostic_limit_chars=diagnostic_limit_chars,
            workspace_links=workspace_links,
        )
    except Exception:
        if "original_wrapper" in locals():
            wrapper.write_text(original_wrapper, encoding="utf-8", newline="\n")
        for record in reversed(move_records):
            source = project_root / record["to"]
            destination = project_root / record["from"]
            if source.exists():
                _move_tree_entry(
                    project_root,
                    source,
                    destination,
                    prefer_git=record["vcs"] == "git",
                )
        if implementation.exists():
            shutil.rmtree(implementation)
        raise
    shutil.rmtree(project_root / Path(directory).parts[0])
    baseline_identity = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(baseline, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    )
    phase_11_identity = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(phase_11_parity, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
    )
    adr_relative = "docs/decisions/0001-legacy-project-lift-and-shift.md"
    adr = project_root / adr_relative
    adr.parent.mkdir(parents=True, exist_ok=True)
    adr.write_text(
        render_lift_shift_adr(
            implementation_directory=implementation.relative_to(
                project_root
            ).as_posix(),
            baseline_identity=baseline_identity,
            parity_identity=phase_11_identity,
        ),
        encoding="utf-8",
        newline="\n",
    )
    return {
        "schema": "literate-ai/legacy-lift-shift@1",
        "state": "passed",
        "implementation_directory": implementation.relative_to(project_root).as_posix(),
        "moves": move_records,
        "phase_11_parity_identity": phase_11_identity,
        "phase_12_parity": parity,
        "adr": adr_relative,
        "quarantine_removed": True,
    }
