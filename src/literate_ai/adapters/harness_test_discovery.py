"""Evidence-backed retained test-command discovery and result observation."""

from __future__ import annotations

import ast
import configparser
import fnmatch
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

_REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_SAFE_GROUP_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SAFE_TEST_PATH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_PYTEST_OUTCOME = re.compile(
    r"\b(\d+)\s+(passed|failed|skipped|xfailed|xpassed|errors?)\b",
    re.IGNORECASE,
)
_CARGO_OUTCOME = re.compile(
    r"\btest result:\s+(?:ok|FAILED)\.\s+"
    r"(\d+) passed;\s+(\d+) failed;\s+(\d+) ignored;",
    re.IGNORECASE,
)
_CTEST_OUTCOME = re.compile(
    r"\b(\d+)% tests passed,\s+(\d+) tests failed out of\s+(\d+)\b",
    re.IGNORECASE,
)
_CTEST_ALL_PASSED = re.compile(
    r"^\s*100% tests passed out of\s+(\d+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_UNITTEST_TOTAL = re.compile(r"\bRan\s+(\d+)\s+tests?\b")
_UNITTEST_FAILED = re.compile(r"\b(?:failures|errors)=(\d+)\b", re.IGNORECASE)
_UNITTEST_SKIPPED = re.compile(r"\bskipped=(\d+)\b", re.IGNORECASE)
_COLLECTED_TOTAL = re.compile(r"\bcollected\s+(\d+)\s+items?\b", re.IGNORECASE)
_REPO_TEST_OUTCOME = re.compile(
    r"\[OK\]\s+All\s+(\d+)\s+tests?\s+process(?:es)?\s+returned\s+0\b",
    re.IGNORECASE,
)
_ZERO_PATTERNS = (
    re.compile(r"\bno tests ran\b", re.IGNORECASE),
    re.compile(r"\bcollected\s+0\s+items?\b", re.IGNORECASE),
    re.compile(r"\bRan\s+0\s+tests?\b"),
)


@dataclass(frozen=True)
class RetainedTestCommand:
    """One repository-owned test command and the evidence that selected it."""

    command: str
    evidence: str
    cwd: str
    cost: str
    detector_id: str
    detail: str


def discover_retained_test_command(root: Path) -> RetainedTestCommand | None:
    """Select a test command only when repository evidence proves its authority."""

    pyproject_path = root / "pyproject.toml"
    pyproject = _load_pyproject(pyproject_path)
    poe = _poe_test_command(pyproject)
    if poe is not None:
        return RetainedTestCommand(
            command=poe,
            evidence="pyproject.toml",
            cwd=".",
            cost="local-cheap",
            detector_id="test-runner.poe",
            detail="pyproject Poe test task",
        )
    script = _aggregate_test_script(root)
    if script is not None:
        relative = script.relative_to(root)
        cwd = relative.parent.as_posix()
        return RetainedTestCommand(
            command=(
                script.name
                if script.suffix.casefold() == ".bat"
                else f"./{script.name}"
            ),
            evidence=relative.as_posix(),
            cwd="." if cwd == "." else cwd,
            cost="host-heavy",
            detector_id="test-runner.aggregate-script",
            detail="repository-owned aggregate test script",
        )
    pytest = _pytest_test_command(root, pyproject)
    if pytest is not None:
        return pytest
    return None


def observe_test_collection(phase: str, stdout: bytes, stderr: bytes) -> dict[str, Any]:
    """Normalize an externally meaningful test-count signal without trusting exit 0."""

    if phase.split(".", 1)[0] != "test":
        return _test_observation("not-applicable")
    text = (stdout + b"\n" + stderr).decode("utf-8", errors="replace")
    repo_test = tuple(_REPO_TEST_OUTCOME.finditer(text))
    if repo_test:
        passed = max(int(match.group(1)) for match in repo_test)
        return _counted_observation(passed, 0, 0, 0)
    if any(pattern.search(text) for pattern in _ZERO_PATTERNS):
        return _test_observation("empty", total=0, passed=0, failed=0, skipped=0)

    cargo = tuple(_CARGO_OUTCOME.finditer(text))
    if cargo:
        passed = sum(int(match.group(1)) for match in cargo)
        failed = sum(int(match.group(2)) for match in cargo)
        skipped = sum(int(match.group(3)) for match in cargo)
        return _counted_observation(passed, failed, skipped, 0)

    ctest = _CTEST_OUTCOME.search(text)
    if ctest is not None:
        failed = int(ctest.group(2))
        total = int(ctest.group(3))
        return _counted_observation(total - failed, failed, 0, 0)

    # CTest 4.4 omits the redundant zero-failures clause on an all-pass run.
    # Only 100% proves an exact passed count; never infer failures by rounding
    # a partial percentage, and keep a zero-test summary empty.
    ctest_passed = _CTEST_ALL_PASSED.search(text)
    if ctest_passed is not None:
        return _counted_observation(int(ctest_passed.group(1)), 0, 0, 0)

    pytest_counts: dict[str, int] = {}
    for match in _PYTEST_OUTCOME.finditer(text):
        outcome = match.group(2).casefold()
        pytest_counts[outcome] = max(pytest_counts.get(outcome, 0), int(match.group(1)))
    if pytest_counts:
        passed = pytest_counts.get("passed", 0) + pytest_counts.get("xpassed", 0)
        failed = pytest_counts.get("failed", 0) + pytest_counts.get("error", 0)
        failed += pytest_counts.get("errors", 0)
        skipped = pytest_counts.get("skipped", 0)
        known_failed = pytest_counts.get("xfailed", 0)
        return _counted_observation(passed, failed, skipped, known_failed)

    unittest_total = _UNITTEST_TOTAL.search(text)
    if unittest_total is not None:
        total = int(unittest_total.group(1))
        failed = sum(int(match.group(1)) for match in _UNITTEST_FAILED.finditer(text))
        skipped = sum(int(match.group(1)) for match in _UNITTEST_SKIPPED.finditer(text))
        return _counted_observation(total - failed - skipped, failed, skipped, 0)

    collected = tuple(_COLLECTED_TOTAL.finditer(text))
    if collected:
        total = max(int(match.group(1)) for match in collected)
        return _test_observation("nonempty", total=total)
    return _test_observation("unreported")


def _test_observation(
    state: str,
    *,
    total: int | None = None,
    passed: int | None = None,
    failed: int | None = None,
    skipped: int | None = None,
    known_failed: int | None = None,
) -> dict[str, Any]:
    return {
        "state": state,
        "total": total,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "known_failed": known_failed,
    }


def _counted_observation(
    passed: int, failed: int, skipped: int, known_failed: int
) -> dict[str, Any]:
    total = passed + failed + skipped + known_failed
    return _test_observation(
        "empty" if total == 0 else "nonempty",
        total=total,
        passed=passed,
        failed=failed,
        skipped=skipped,
        known_failed=known_failed,
    )


def _load_pyproject(path: Path) -> dict[str, Any]:
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _poe_test_command(pyproject: dict[str, Any]) -> str | None:
    tool = pyproject.get("tool")
    poe = tool.get("poe") if isinstance(tool, dict) else None
    tasks = poe.get("tasks") if isinstance(poe, dict) else None
    if not isinstance(tasks, dict) or "test" not in tasks:
        return None
    groups = pyproject.get("dependency-groups")
    if isinstance(groups, dict):
        for group, requirements in sorted(groups.items()):
            if (
                isinstance(group, str)
                and _SAFE_GROUP_NAME.fullmatch(group)
                and _requirements_include(requirements, "poethepoet")
            ):
                return f"uv run --group {group} poe test"
    if _project_requirements_include(pyproject, "poethepoet"):
        if isinstance(tool, dict) and "uv" in tool:
            return "uv run poe test"
        return "poe test"
    return None


def _project_requirements_include(pyproject: dict[str, Any], name: str) -> bool:
    project = pyproject.get("project")
    if not isinstance(project, dict):
        return False
    if _requirements_include(project.get("dependencies"), name):
        return True
    optional = project.get("optional-dependencies")
    return isinstance(optional, dict) and any(
        _requirements_include(requirements, name) for requirements in optional.values()
    )


def _requirements_include(value: object, name: str) -> bool:
    if not isinstance(value, list):
        return False
    for item in value:
        if not isinstance(item, str):
            continue
        match = _REQUIREMENT_NAME.match(item)
        if match and match.group(1).casefold().replace("_", "-") == name:
            return True
    return False


def _aggregate_test_script(root: Path) -> Path | None:
    suffix = ".bat" if os.name == "nt" else ".sh"
    for relative in (
        f"run_tests{suffix}",
        f"tests/run_tests{suffix}",
        f"tests/docs/run_tests{suffix}",
        f"scripts/run_tests{suffix}",
    ):
        candidate = root / relative
        if not candidate.is_file() or candidate.is_symlink():
            continue
        if suffix == ".sh" and not os.access(candidate, os.X_OK):
            continue
        return candidate
    return None


def _pytest_test_command(
    root: Path, pyproject: dict[str, Any]
) -> RetainedTestCommand | None:
    patterns = ("test_*.py", "*_test.py")
    testpaths = ("tests",)
    configured = False
    evidence: str | None = None
    tool = pyproject.get("tool")
    pytest = tool.get("pytest") if isinstance(tool, dict) else None
    ini = pytest.get("ini_options") if isinstance(pytest, dict) else None
    if isinstance(ini, dict):
        configured = True
        evidence = "pyproject.toml"
        patterns = _configured_values(ini.get("python_files"), patterns)
        testpaths = _configured_testpaths(ini.get("testpaths"), testpaths)
    for name, section in (
        ("pytest.ini", "pytest"),
        ("tox.ini", "pytest"),
        ("setup.cfg", "tool:pytest"),
    ):
        path = root / name
        if not path.is_file():
            continue
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(path, encoding="utf-8")
        except (configparser.Error, OSError):
            continue
        if not parser.has_section(section):
            continue
        configured = True
        if evidence is None:
            evidence = name
        patterns = _configured_values(
            parser.get(section, "python_files", fallback=None), patterns
        )
        testpaths = _configured_testpaths(
            parser.get(section, "testpaths", fallback=None), testpaths
        )
    declared = _project_requirements_include(pyproject, "pytest")
    if declared and evidence is None:
        evidence = "pyproject.toml"
    if not (configured or declared) or not _has_collectable_pytest_node(
        root, testpaths, patterns
    ):
        return None
    explicit_paths = " ".join(testpaths) if testpaths else ""
    command = f"python -m pytest {explicit_paths}".rstrip()
    assert evidence is not None
    return RetainedTestCommand(
        command=command,
        evidence=evidence,
        cwd=".",
        cost="local-cheap",
        detector_id="test-runner.pytest",
        detail="pytest configuration/dependency plus statically collectable tests",
    )


def _configured_values(value: object, default: tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(value, str):
        values = tuple(value.split())
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        values = tuple(value)
    else:
        return default
    return tuple(item for item in values if _safe_relative_pattern(item)) or default


def _configured_testpaths(value: object, default: tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(value, str):
        values = tuple(value.split())
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        values = tuple(value)
    else:
        return default
    if not values or any(not _safe_test_path(item) for item in values):
        return default
    return values


def _safe_test_path(value: str) -> bool:
    if not _SAFE_TEST_PATH.fullmatch(value) or "\\" in value:
        return False
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and path.as_posix() == value
        and all(part not in {"", ".", ".."} for part in path.parts)
    )


def _safe_relative_pattern(value: str) -> bool:
    path = Path(value)
    return bool(value) and not path.is_absolute() and ".." not in path.parts


def _has_collectable_pytest_node(
    root: Path, testpaths: tuple[str, ...], patterns: tuple[str, ...]
) -> bool:
    for relative in testpaths:
        candidate = root / relative
        if candidate.is_file():
            paths = (candidate,)
        elif candidate.is_dir():
            paths = candidate.rglob("*.py")
        else:
            paths = ()
        for path in paths:
            if not any(fnmatch.fnmatchcase(path.name, pattern) for pattern in patterns):
                continue
            try:
                module = ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, SyntaxError, UnicodeError):
                continue
            if any(_is_pytest_node(node) for node in module.body):
                return True
    return False


def _is_pytest_node(node: ast.stmt) -> bool:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return node.name.startswith("test")
    return (
        isinstance(node, ast.ClassDef)
        and node.name.startswith("Test")
        and any(
            isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            and child.name.startswith("test")
            for child in node.body
        )
    )
