"""Fail-closed pytest-testmon invocation for CI impact selection.

A durations file is shard-balancing evidence, never skip authority. Release
gates must keep the full suite: this module only emits ``--testmon`` when a
SQLite map plus an identity sidecar name an ancestor of HEAD and the plugin
is actually importable.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from literate_ai.adapters._processes import run_with_tree_kill

IMPACT_MAP_IDENTITY_SCHEMA = "literate-ai/ci-impact-map-identity@1"
IMPACT_MAP_NAME = ".testmondata"
IMPACT_MAP_IDENTITY_NAME = ".testmondata.identity.json"
IMPACT_MECHANISM = "pytest-testmon"
_SQLITE_HEADER = b"SQLite format 3"
_GIT_TIMEOUT_SECONDS = 30
_TESTMON_PROBE_TIMEOUT_SECONDS = 30
_STRIPPED_FLAGS = frozenset(
    {
        "--testmon",
        "--testmon-noselect",
        "--testmon-nocollect",
        "--testmon-forceselect",
        "--no-testmon",
    }
)


class CiImpactRunError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ImpactMapTrust:
    trusted: bool
    reason: str
    base_revision: str | None = None
    head_revision: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "trusted": self.trusted,
            "reason": self.reason,
            "base_revision": self.base_revision,
            "head_revision": self.head_revision,
        }


def inspect_impact_map(root: Path) -> ImpactMapTrust:
    """Return whether ``root`` may skip tests with pytest-testmon."""

    map_path = root / IMPACT_MAP_NAME
    if not _is_regular_file(map_path):
        return ImpactMapTrust(False, "missing_map")
    if not _looks_like_sqlite(map_path):
        return ImpactMapTrust(False, "map_invalid")
    identity_path = root / IMPACT_MAP_IDENTITY_NAME
    if not _is_regular_file(identity_path):
        return ImpactMapTrust(False, "missing_identity")
    payload = _load_identity(identity_path)
    if payload is None:
        return ImpactMapTrust(False, "identity_invalid")
    if "durations_path" in payload or "durations" in payload:
        return ImpactMapTrust(False, "durations_are_not_skip_authority")
    if payload.get("schema") != IMPACT_MAP_IDENTITY_SCHEMA:
        return ImpactMapTrust(False, "identity_invalid")
    if payload.get("mechanism") != IMPACT_MECHANISM:
        return ImpactMapTrust(False, "identity_invalid")
    base_revision = payload.get("base_revision")
    if not isinstance(base_revision, str) or not base_revision.strip():
        return ImpactMapTrust(False, "identity_invalid")
    head = _git_head(root)
    if head is None:
        return ImpactMapTrust(
            False,
            "git_unavailable",
            base_revision=base_revision.strip(),
        )
    if not _is_ancestor(root, base_revision.strip(), head):
        return ImpactMapTrust(
            False,
            "stale_revision",
            base_revision=base_revision.strip(),
            head_revision=head,
        )
    return ImpactMapTrust(
        True,
        "trusted",
        base_revision=base_revision.strip(),
        head_revision=head,
    )


def write_impact_map_identity(root: Path, *, revision: str | None = None) -> Path:
    """Record the Git revision at which a testmon map was collected."""

    recorded = revision.strip() if revision is not None else _git_head(root)
    if not recorded:
        raise CiImpactRunError(
            "ci.impact_identity_revision_missing",
            "impact map identity requires git HEAD or an explicit revision",
        )
    payload = {
        "schema": IMPACT_MAP_IDENTITY_SCHEMA,
        "mechanism": IMPACT_MECHANISM,
        "base_revision": recorded,
    }
    path = root / IMPACT_MAP_IDENTITY_NAME
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def pytest_testmon_available(python: str) -> bool:
    try:
        completed = run_with_tree_kill(
            [python, "-c", "import testmon"],
            timeout=_TESTMON_PROBE_TIMEOUT_SECONDS,
            text=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def resolve_pytest_impact_run(
    root: Path,
    pytest_args: Sequence[str] = (),
    *,
    python: str | None = None,
    refresh: bool = False,
    testmon_available: bool | None = None,
) -> dict[str, Any]:
    """Choose pytest argv: ``--testmon`` only when the map is trusted."""

    interpreter = python or os.environ.get("PYTHON", "python")
    available = (
        pytest_testmon_available(interpreter)
        if testmon_available is None
        else testmon_available
    )
    trust = inspect_impact_map(root)
    cleaned = _without_testmon_flags(pytest_args)
    use_testmon = False
    selection = "full_suite"
    reason = trust.reason
    if refresh:
        use_testmon = available
        if not available:
            reason = "testmon_unavailable"
    elif trust.trusted and available:
        use_testmon = True
        selection = "impact"
    elif trust.trusted and not available:
        reason = "testmon_unavailable"
    map_present = _looks_like_sqlite(root / IMPACT_MAP_NAME)
    argv = [
        interpreter,
        "-m",
        "pytest",
        *_testmon_flags(
            use_testmon,
            refresh=refresh,
            plugin_available=available,
            map_present=map_present,
        ),
        *cleaned,
    ]
    return {
        "selection": selection,
        "use_testmon": use_testmon,
        "refresh": refresh,
        "argv": argv,
        "trust": trust.to_dict(),
        "testmon_available": available,
        "reason": reason,
    }


def run_pytest_impact(
    root: Path,
    pytest_args: Sequence[str] = (),
    *,
    python: str | None = None,
    refresh: bool = False,
    timeout: float = 3600,
    testmon_available: bool | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    """Run the fail-closed pytest argv and stamp identity after a green refresh."""

    decision = resolve_pytest_impact_run(
        root,
        pytest_args,
        python=python,
        refresh=refresh,
        testmon_available=testmon_available,
    )
    completed = run_with_tree_kill(
        decision["argv"],
        cwd=root,
        env=_pytest_environment(),
        timeout=timeout,
        text=True,
        check=False,
    )
    if (
        refresh
        and completed.returncode == 0
        and decision["use_testmon"]
        and _looks_like_sqlite(root / IMPACT_MAP_NAME)
    ):
        write_impact_map_identity(root)
        trust = inspect_impact_map(root)
        decision = {**decision, "trust": trust.to_dict(), "reason": trust.reason}
    return completed, decision


def _is_regular_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def _looks_like_sqlite(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(len(_SQLITE_HEADER)) == _SQLITE_HEADER
    except OSError:
        return False


def _load_identity(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _git_environment() -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"}
    }
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GCM_INTERACTIVE"] = "Never"
    return environment


def _git_text(root: Path, *arguments: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return run_with_tree_kill(
            ["git", "-C", str(root), *arguments],
            env=_git_environment(),
            timeout=_GIT_TIMEOUT_SECONDS,
            text=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _git_head(root: Path) -> str | None:
    completed = _git_text(root, "rev-parse", "HEAD")
    if completed is None or completed.returncode != 0:
        return None
    head = completed.stdout.strip()
    return head or None


def _is_ancestor(root: Path, base_revision: str, head: str) -> bool:
    verify = _git_text(
        root,
        "rev-parse",
        "--verify",
        "--end-of-options",
        f"{base_revision}^{{commit}}",
    )
    if verify is None or verify.returncode != 0:
        return False
    completed = _git_text(root, "merge-base", "--is-ancestor", base_revision, head)
    return completed is not None and completed.returncode == 0


def _without_testmon_flags(arguments: Sequence[str]) -> tuple[str, ...]:
    return tuple(
        argument
        for argument in arguments
        if argument not in _STRIPPED_FLAGS and not argument.startswith("--testmon")
    )


def _testmon_flags(
    use_testmon: bool,
    *,
    refresh: bool,
    plugin_available: bool,
    map_present: bool,
) -> tuple[str, ...]:
    if not use_testmon:
        if plugin_available and not refresh:
            return ("--no-testmon",)
        return ()
    if refresh:
        return ("--testmon-noselect",) if map_present else ("--testmon",)
    return ("--testmon",)


def _pytest_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment.pop("PYTEST_ADDOPTS", None)
    return environment


__all__ = [
    "IMPACT_MAP_IDENTITY_NAME",
    "IMPACT_MAP_IDENTITY_SCHEMA",
    "IMPACT_MAP_NAME",
    "IMPACT_MECHANISM",
    "CiImpactRunError",
    "ImpactMapTrust",
    "inspect_impact_map",
    "pytest_testmon_available",
    "resolve_pytest_impact_run",
    "run_pytest_impact",
    "write_impact_map_identity",
]
