"""Attribute Cargo test candidates and strict named libtest results.

Parsing grants no execution or receipt authority. The runner must retain measured
compiler/input custody, guard exact binary bytes and obtain reviewed case inventory.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from literate_ai.adapters.builders._process import BoundedProcessResult
from literate_ai.adapters.cargo_workspace_graph import (
    _set,
    verify_cargo_workspace_graph,
)
from literate_ai.adapters.retained_cargo_current import _unique_object
from literate_ai.contracts.cargo_workspace import CargoWorkspaceExpectation
from literate_ai.contracts.retained_cargo_tests import (
    retained_cargo_test_targets,
    validate_retained_test_cases,
)


@dataclass(frozen=True, slots=True)
class CargoTestExecutable:
    package_root: str
    target_name: str
    target_kinds: tuple[str, ...]
    source: str
    executable: Path


def _successful_text(result, maximum_bytes):
    if (
        not isinstance(result, BoundedProcessResult)
        or type(result.returncode) is not int
        or result.returncode != 0
        or not isinstance(result.stdout, bytes)
        or not isinstance(result.stderr, bytes)
        or type(maximum_bytes) is not int
        or maximum_bytes < 1
        or len(result.stdout) + len(result.stderr) > maximum_bytes
    ):
        raise ValueError("retained.tests.process-refused")
    try:
        return result.stdout.decode("utf-8")
    except UnicodeError:
        raise ValueError("retained.tests.output-invalid") from None


def select_cargo_test_executables(
    result: BoundedProcessResult,
    metadata: dict,
    *,
    workspace_root: Path,
    output_root: Path,
    expected: CargoWorkspaceExpectation,
    maximum_bytes: int = 8 * 1024 * 1024,
    maximum_records: int = 10000,
) -> tuple[CargoTestExecutable, ...]:
    """Match a workspace/all-targets/no-run JSON result, including test=false.

    Output-root ownership and binary byte custody belong to the actual runner.
    Custom harness candidates are included; their protocol cannot be assumed.
    """
    verify_cargo_workspace_graph(
        metadata, workspace_root=workspace_root, expected=expected
    )
    declared_output = workspace_root / expected.output_directory
    if (
        not isinstance(output_root, Path)
        or not output_root.is_absolute()
        or ".." in output_root.parts
        or not output_root.is_relative_to(declared_output)
        or type(maximum_records) is not int
        or maximum_records < 1
    ):
        raise ValueError("retained.tests.configuration-invalid")
    text = _successful_text(result, maximum_bytes)
    lines = text.splitlines()
    if not lines or len(lines) > maximum_records:
        raise ValueError("retained.tests.record-limit")
    ids = {
        p["manifest_path"]: p["id"] for p in metadata["packages"] if p["source"] is None
    }
    selected = {}
    for package, target in retained_cargo_test_targets(expected):
        identity = ids[str(workspace_root / package.root / "Cargo.toml")]
        key = (identity, target.name, tuple(sorted(target.kinds)))
        if key in selected:
            raise ValueError("retained.tests.target-ambiguous")
        selected[key] = (package, target)
    if not selected:
        raise ValueError("retained.tests.targets-empty")
    binaries = {}
    finished = False
    for line in lines:
        try:
            record = json.loads(line, object_pairs_hook=_unique_object)
        except (ValueError, TypeError, RecursionError):
            raise ValueError("retained.tests.record-invalid") from None
        if not isinstance(record, dict) or finished:
            raise ValueError("retained.tests.record-invalid")
        reason = record.get("reason")
        if reason == "build-finished":
            if record.get("success") is not True:
                raise ValueError("retained.tests.compilation-failed")
            finished = True
            continue
        if reason in {"compiler-message", "build-script-executed"}:
            continue
        if reason != "compiler-artifact" or not isinstance(record.get("profile"), dict):
            raise ValueError("retained.tests.record-invalid")
        test = record["profile"].get("test")
        if type(test) is not bool:
            raise ValueError("retained.tests.record-invalid")
        if not test:
            continue
        target = record.get("target")
        if (
            not isinstance(target, dict)
            or not isinstance(target.get("kind"), list)
            or any(not isinstance(k, str) for k in target["kind"])
        ):
            raise ValueError("retained.tests.target-invalid")
        key = (
            record.get("package_id"),
            target.get("name"),
            tuple(sorted(target["kind"])),
        )
        if (
            not all(isinstance(k, str) for k in key[:2])
            or key not in selected
            or key in binaries
        ):
            raise ValueError("retained.tests.target-set-drift")
        package, wanted = selected[key]
        if (
            target.get("src_path") != str(workspace_root / wanted.source)
            or _set(target.get("crate_types")) != set(wanted.crate_types)
            or target.get("edition") != wanted.edition
            or target.get("test") is not wanted.test
            or target.get("doctest") is not wanted.doctest
            or _set(record.get("features")) != set(package.features)
        ):
            raise ValueError("retained.tests.target-drift")
        value = record.get("executable")
        if not isinstance(value, str):
            raise ValueError("retained.tests.executable-required")
        path = Path(value)
        if (
            not path.is_absolute()
            or ".." in path.parts
            or not path.is_relative_to(output_root)
            or path == output_root
        ):
            raise ValueError("retained.tests.executable-path-refused")
        binaries[key] = CargoTestExecutable(
            package.root, wanted.name, wanted.kinds, wanted.source, path
        )
    if not finished or set(binaries) != set(selected):
        raise ValueError("retained.tests.target-set-drift")
    if len({binary.executable for binary in binaries.values()}) != len(binaries):
        raise ValueError("retained.tests.executable-alias")
    return tuple(binaries[key] for key in sorted(binaries))


def verify_libtest_observation(
    listed: BoundedProcessResult,
    executed: BoundedProcessResult,
    expected_cases: tuple[str, ...],
    *,
    maximum_bytes: int = 4 * 1024 * 1024,
    maximum_cases: int = 100000,
) -> tuple[str, ...]:
    """Require exact named discovery and passing results for one guarded binary.

    The runner uses --list --format=terse, then --format=pretty --color=never.
    Empty targets are represented, but a receipt must require positive tests
    overall. Unrecognized output refuses instead of becoming zero tests.
    """
    validate_retained_test_cases(expected_cases, maximum_cases)
    discovery = _successful_text(listed, maximum_bytes).splitlines()
    if len(discovery) != len(expected_cases) or any(
        not line.endswith(": test") for line in discovery
    ):
        raise ValueError("retained.tests.discovery-mismatch")
    names = tuple(sorted(line[:-6] for line in discovery))
    if names != expected_cases:
        raise ValueError("retained.tests.discovery-mismatch")
    lines = [
        line for line in _successful_text(executed, maximum_bytes).splitlines() if line
    ]
    count = len(expected_cases)
    if len(lines) < 2 or lines[0] != f"running {count} test" + (
        "" if count == 1 else "s"
    ):
        raise ValueError("retained.tests.run-framing-invalid")
    summary = (
        rf"test result: ok\. {count} passed; 0 failed; 0 ignored; "
        r"0 measured; 0 filtered out; finished in [0-9]+\.[0-9]+s"
    )
    if re.fullmatch(summary, lines[-1]) is None:
        raise ValueError("retained.tests.summary-mismatch")
    passed = []
    expected_names = set(expected_cases)
    for line in lines[1:-1]:
        match = re.fullmatch(r"test (\S+)(?: - should panic)? \.\.\. ok", line)
        if match is None:
            slow = re.fullmatch(
                r"test (\S+) has been running for over [0-9]+ seconds", line
            )
            if slow is not None and slow.group(1) in expected_names:
                continue
            raise ValueError("retained.tests.case-result-invalid")
        passed.append(match.group(1))
    if tuple(sorted(passed)) != expected_cases:
        raise ValueError("retained.tests.case-result-mismatch")
    return expected_cases
