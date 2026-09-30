"""Detect test frameworks and emit a fail-closed CI shard/impact plan.

Language research is recorded from GitHub issues #83 (Go), #84 (Rust), #85 (C++),
and #86 (Swift), plus the pytest and Jest mechanisms already named by CI-SHARD-001.
This module does not install or invoke those runtimes: availability of a maintained
mechanism is the skill contract. A missing or untrusted impact map must select the
full suite, never a truncated one.
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from literate_ai.ci_impact_run import (
    IMPACT_MAP_IDENTITY_NAME,
    IMPACT_MAP_NAME,
    inspect_impact_map,
)

CI_TEST_PLAN_SCHEMA = "literate-ai/ci-test-plan@1"
_MODES = frozenset({"shard", "impact", "compose"})
_IMPACT_MAP_NAMES = (IMPACT_MAP_NAME, ".testmondata-journal", IMPACT_MAP_IDENTITY_NAME)

# Recorded from issues #83–#86 and this repository's Windows pytest-split wiring.
# `unverified` means a tool was cited but not confirmed currently maintained.
_SHARD: dict[str, dict[str, Any]] = {
    "pytest": {
        "status": "available",
        "mechanism": "pytest-split --splits/--group",
        "durations": True,
        "durations_path": ".test_durations",
        "refresh": "pytest --store-durations --durations-path=.test_durations",
        "citations": ("windows-ci",),
    },
    "jest": {
        "status": "available",
        "mechanism": "jest --shard=i/n",
        "durations": False,
        "note": "Jest --shard is a count split, not a durations-weighted split",
        "citations": ("ci-shard-001",),
    },
    "go-test": {
        "status": "available",
        "mechanism": (
            "hashicorp-forge/go-test-split-action with gotestsum JUnit durations"
        ),
        "durations": True,
        "citations": ("#83",),
    },
    "cargo-nextest": {
        "status": "available",
        "mechanism": "cargo nextest run --partition slice:m/n",
        "durations": False,
        "note": "slice partitioning balances without a committed durations seed",
        "citations": ("#84",),
    },
    "googletest": {
        "status": "available",
        "mechanism": "gtest-parallel (duration-sorted) or GTEST_TOTAL_SHARDS (hash)",
        "durations": True,
        "citations": ("#85",),
    },
    "ctest": {
        "status": "unavailable",
        "mechanism": None,
        "note": (
            "CTEST_PARALLEL_LEVEL is within-machine only; cross-runner sharding "
            "needs external test-list partitioning"
        ),
        "citations": ("#85",),
    },
    "swiftpm": {
        "status": "unavailable",
        "mechanism": None,
        "note": (
            "no maintained duration-based CI shard tool; swift test --parallel "
            "is in-process only"
        ),
        "citations": ("#86",),
    },
    "unittest-checkpoint": {
        "status": "unavailable",
        "mechanism": None,
        "note": (
            "checkpointing requires completed == tests[:len(completed)]; "
            "parallel shards finish out of order"
        ),
        "citations": ("ci-shard-001",),
    },
}

_IMPACT: dict[str, dict[str, Any]] = {
    "pytest": {
        "status": "available",
        "mechanism": "pytest-testmon",
        "map_names": list(_IMPACT_MAP_NAMES),
        "identity_name": IMPACT_MAP_IDENTITY_NAME,
        "refresh": "python scripts/run_impact_pytest.py --refresh --",
        "fail_closed": (
            "run the full suite when the testmon map or identity sidecar is "
            "missing, the recorded revision is not an ancestor of HEAD, "
            "pytest-testmon is unavailable, or skip would come from a "
            "durations file"
        ),
        "citations": ("ci-impact-001",),
    },
    "jest": {
        "status": "available",
        "mechanism": "jest --changedSince=<base> or --onlyChanged",
        "fail_closed": (
            "run the full suite when the base revision is missing or not an ancestor"
        ),
        "citations": ("ci-impact-001",),
    },
    "go-test": {
        "status": "unverified",
        "mechanism": "package-graph tools (slim, GTA, affected)",
        "fail_closed": "run go test ./... when affected-package data is untrusted",
        "note": "maintenance of the cited Go package-graph tools was not confirmed",
        "citations": ("#83",),
    },
    "cargo-nextest": {
        "status": "unavailable",
        "mechanism": None,
        "note": (
            "nextest has no impact selection; cargo-test-changed is unverified; "
            "Bazel rules_rust is the credible path for Bazel-built Rust"
        ),
        "fail_closed": "run the full suite",
        "citations": ("#84",),
    },
    "googletest": {
        "status": "unavailable",
        "mechanism": None,
        "note": (
            "no pytest-testmon equivalent; prefer CMake or Bazel graph traversal; "
            "O3DE TIAF is project-specific"
        ),
        "fail_closed": "run the full suite",
        "citations": ("#85",),
    },
    "ctest": {
        "status": "unavailable",
        "mechanism": None,
        "note": "no native CTest impact map; walk the CMake or Bazel graph instead",
        "fail_closed": "run the full suite",
        "citations": ("#85",),
    },
    "swiftpm": {
        "status": "unavailable",
        "mechanism": None,
        "note": (
            "no cross-platform OSS impact tool; XcodeSelectiveTesting is Xcode-shaped"
        ),
        "fail_closed": "run the full suite",
        "citations": ("#86",),
    },
    "unittest-checkpoint": {
        "status": "unavailable",
        "mechanism": None,
        "note": (
            "checkpointing skips already-passed tests of the same local plan, "
            "not tests whose covered code is unchanged"
        ),
        "fail_closed": "run the full suite",
        "citations": ("ci-impact-001",),
    },
}

_SHARD_MARKERS = (
    "pytest-split",
    "--splits",
    "--shard=",
    "--partition slice:",
    "GTEST_TOTAL_SHARDS",
    "go-test-split-action",
    "gtest-parallel",
)
_IMPACT_MARKERS = (
    "pytest-testmon",
    "--changedSince",
    "--onlyChanged",
    "cargo-test-changed",
)


class CiTestPlanError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def plan_ci_tests(path: Path, *, mode: str = "compose") -> dict[str, Any]:
    """Emit a versioned shard/impact plan for one project tree."""

    if mode not in _MODES:
        raise CiTestPlanError(
            "ci.plan_mode_invalid",
            "mode must be shard, impact, or compose",
        )
    try:
        root = path.expanduser().resolve()
    except OSError as exc:
        raise CiTestPlanError(
            "ci.plan_path_invalid", "CI plan path could not be resolved"
        ) from exc
    if not root.is_dir() or root.is_symlink():
        raise CiTestPlanError(
            "ci.plan_path_invalid",
            "CI plan path must be a non-symlink directory",
        )
    detected = _detect_frameworks(root)
    authored = _authored_wiring(root)
    trust = inspect_impact_map(root)
    impact_trusted = trust.trusted
    frameworks = tuple(
        _framework_entry(framework_id, evidence, authored, impact_trusted)
        for framework_id, evidence in detected
    )
    shard_status = _aggregate(frameworks, "shard")
    impact_status = _aggregate(frameworks, "impact")
    if mode == "shard":
        status = shard_status
    elif mode == "impact":
        status = impact_status
    else:
        status = _compose_status(shard_status, impact_status)
    selection = "full_suite"
    if (
        mode in {"impact", "compose"}
        and impact_status == "available"
        and impact_trusted
    ):
        selection = "impact_then_shard" if mode == "compose" else "impact"
    elif mode == "compose" and shard_status in {"available", "partial"}:
        selection = "shard_full_suite"
    elif mode == "shard" and shard_status in {"available", "partial"}:
        selection = "shard"
    return {
        "schema": CI_TEST_PLAN_SCHEMA,
        "project_root": str(root),
        "mode": mode,
        "silent_noop": False,
        "status": status,
        "reason": None if frameworks else "no_recognized_test_framework",
        "composition": (
            "impact then shard remaining; untrusted or missing impact maps "
            "select the full suite, then shard if a shard mechanism exists"
        ),
        "selection": selection,
        "impact_map_trusted": impact_trusted,
        "impact_map_reason": trust.reason,
        "fail_closed_to_full_suite": not impact_trusted,
        "checkpoint_jobs": _checkpoint_decision(detected),
        "frameworks": list(frameworks),
        "already_authored": authored,
    }


def _detect_frameworks(root: Path) -> tuple[tuple[str, tuple[str, ...]], ...]:
    found: list[tuple[str, tuple[str, ...]]] = []
    pytest_hits = _pytest_evidence(root)
    if pytest_hits:
        found.append(("pytest", pytest_hits))
    checkpoint_hits = _checkpoint_evidence(root)
    if checkpoint_hits:
        found.append(("unittest-checkpoint", checkpoint_hits))
    jest_hits = _jest_evidence(root)
    if jest_hits:
        found.append(("jest", jest_hits))
    if (root / "go.mod").is_file():
        found.append(("go-test", ("go.mod",)))
    if (root / "Cargo.toml").is_file():
        found.append(("cargo-nextest", ("Cargo.toml",)))
    cmake = _cmake_text(root)
    if cmake is not None:
        lowered = cmake.lower()
        if "gtest" in lowered or "googletest" in lowered:
            found.append(("googletest", ("CMakeLists.txt",)))
        if "enable_testing" in lowered or "add_test(" in lowered:
            found.append(("ctest", ("CMakeLists.txt",)))
    if (root / "Package.swift").is_file():
        found.append(("swiftpm", ("Package.swift",)))
    return tuple(found)


def _pytest_evidence(root: Path) -> tuple[str, ...]:
    hits: list[str] = []
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            document = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError):
            document = {}
        tool = document.get("tool")
        if isinstance(tool, dict) and "pytest" in tool:
            hits.append("pyproject.toml")
    for name in ("pytest.ini", "conftest.py"):
        if (root / name).is_file():
            hits.append(name)
    if (root / ".test_durations").is_file():
        hits.append(".test_durations")
    return tuple(hits)


def _checkpoint_evidence(root: Path) -> tuple[str, ...]:
    hits: list[str] = []
    runner = root / "scripts" / "run_checkpointed_unittests.py"
    if runner.is_file():
        hits.append("scripts/run_checkpointed_unittests.py")
    module = root / "src" / "literate_ai" / "test_checkpointing.py"
    if module.is_file():
        hits.append("src/literate_ai/test_checkpointing.py")
    return tuple(hits)


def _jest_evidence(root: Path) -> tuple[str, ...]:
    manifest = root / "package.json"
    if not manifest.is_file():
        return ()
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ()
    if not isinstance(payload, dict):
        return ()
    for key in ("dependencies", "devDependencies"):
        block = payload.get(key)
        if isinstance(block, dict) and "jest" in block:
            return ("package.json",)
    if "jest" in payload:
        return ("package.json",)
    scripts = payload.get("scripts")
    if isinstance(scripts, dict) and any(
        isinstance(value, str) and "jest" in value for value in scripts.values()
    ):
        return ("package.json",)
    return ()


def _cmake_text(root: Path) -> str | None:
    path = root / "CMakeLists.txt"
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None


def _authored_wiring(root: Path) -> list[dict[str, str]]:
    authored: list[dict[str, str]] = []
    for path in _ci_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        relative = path.relative_to(root).as_posix()
        for marker in _SHARD_MARKERS:
            if marker in text:
                authored.append({"kind": "shard", "path": relative, "marker": marker})
                break
        for marker in _IMPACT_MARKERS:
            if marker in text:
                authored.append({"kind": "impact", "path": relative, "marker": marker})
                break
    return authored


def _ci_files(root: Path) -> tuple[Path, ...]:
    files: list[Path] = []
    workflows = root / ".github" / "workflows"
    if workflows.is_dir():
        files.extend(sorted(workflows.glob("*.yml")))
        files.extend(sorted(workflows.glob("*.yaml")))
    for name in (".gitlab-ci.yml", "azure-pipelines.yml"):
        candidate = root / name
        if candidate.is_file():
            files.append(candidate)
    script = root / "scripts" / "run_impact_pytest.py"
    if script.is_file():
        files.append(script)
    return tuple(files)


def _framework_entry(
    framework_id: str,
    evidence: tuple[str, ...],
    authored: Sequence[Mapping[str, str]],
    impact_trusted: bool,
) -> dict[str, Any]:
    shard = dict(_SHARD[framework_id])
    impact = dict(_IMPACT[framework_id])
    impact["map_trusted"] = bool(impact_trusted and impact["status"] == "available")
    if impact["status"] == "available" and not impact_trusted:
        impact["selection"] = "full_suite"
    elif impact["status"] == "available":
        impact["selection"] = "impact"
    else:
        impact["selection"] = "full_suite"
    shard["already_authored"] = any(item["kind"] == "shard" for item in authored)
    impact["already_authored"] = any(item["kind"] == "impact" for item in authored)
    return {
        "framework_id": framework_id,
        "detected_by": list(evidence),
        "shard": shard,
        "impact": impact,
    }


def _aggregate(frameworks: Sequence[Mapping[str, Any]], field: str) -> str:
    if not frameworks:
        return "unavailable"
    statuses = {str(item[field]["status"]) for item in frameworks}
    if statuses == {"available"}:
        return "available"
    if "available" in statuses:
        return "partial"
    if "unverified" in statuses:
        return "unverified"
    return "unavailable"


def _compose_status(shard_status: str, impact_status: str) -> str:
    ranks = {
        "available": 3,
        "partial": 2,
        "unverified": 1,
        "unavailable": 0,
    }
    return max((shard_status, impact_status), key=lambda item: ranks[item])


def _checkpoint_decision(
    detected: Sequence[tuple[str, tuple[str, ...]]],
) -> dict[str, str] | None:
    if not any(framework_id == "unittest-checkpoint" for framework_id, _ in detected):
        return None
    return {
        "decision": "keep_unsharded",
        "reason": (
            "load_completed requires completed == tests[:len(completed)]; "
            "do not convert Linux/macOS checkpointed jobs to pytest-split"
        ),
    }


__all__ = [
    "CI_TEST_PLAN_SCHEMA",
    "CiTestPlanError",
    "plan_ci_tests",
]
