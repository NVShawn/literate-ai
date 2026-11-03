"""Strict observed libtest outcomes with reviewed ignored cases, never admission."""

from __future__ import annotations

import re
from dataclasses import dataclass

from literate_ai.adapters.retained_cargo_test_observation import _successful_text
from literate_ai.contracts.retained_cargo_test_protocol import (
    RetainedCargoIgnoredCase,
    RetainedCargoLibtestProtocol,
)


@dataclass(frozen=True, slots=True)
class RetainedCargoLibtestObservationV2:
    passed: tuple[str, ...]
    ignored: tuple[RetainedCargoIgnoredCase, ...]


def verify_libtest_observation_v2(
    listed,
    executed,
    expected: RetainedCargoLibtestProtocol,
    *,
    maximum_bytes: int = 4 * 1024 * 1024,
) -> RetainedCargoLibtestObservationV2:
    """Verify exact discovery, named outcomes/reasons and all summary counts.

    The surrounding native owner must attribute the harness declaration, guard
    its binary and all input bytes, and retain the bounded command results. An
    ignored result never appears in the returned passed set. Existing v1
    observation and its zero-ignored rule remain unchanged.
    """
    if type(expected) is not RetainedCargoLibtestProtocol:
        raise ValueError("retained.tests.v2-protocol-required")
    names = tuple(sorted((*expected.must_run, *(x.name for x in expected.ignored))))
    discovery = _successful_text(listed, maximum_bytes).splitlines()
    if (
        len(discovery) != len(names)
        or any(not line.endswith(": test") for line in discovery)
        or tuple(sorted(line[:-6] for line in discovery)) != names
    ):
        raise ValueError("retained.tests.discovery-mismatch")
    lines = [
        line for line in _successful_text(executed, maximum_bytes).splitlines() if line
    ]
    count = len(names)
    if len(lines) < 2 or lines[0] != f"running {count} test" + (
        "" if count == 1 else "s"
    ):
        raise ValueError("retained.tests.run-framing-invalid")
    summary = (
        rf"test result: ok\. {len(expected.must_run)} passed; 0 failed; "
        rf"{len(expected.ignored)} ignored; 0 measured; 0 filtered out; "
        r"finished in [0-9]+\.[0-9]+s"
    )
    if re.fullmatch(summary, lines[-1]) is None:
        raise ValueError("retained.tests.summary-mismatch")
    passed, ignored = [], []
    known = set(names)
    for line in lines[1:-1]:
        match = re.fullmatch(r"test (\S+)(?: - should panic)? \.\.\. ok", line)
        if match is not None:
            passed.append(match.group(1))
            continue
        match = re.fullmatch(
            r"test (\S+)(?: - should panic)? \.\.\. ignored(?:, (.+))?", line
        )
        if match is not None:
            ignored.append(RetainedCargoIgnoredCase(match.group(1), match.group(2)))
            continue
        slow = re.fullmatch(
            r"test (\S+) has been running for over [0-9]+ seconds", line
        )
        if slow is None or slow.group(1) not in known:
            raise ValueError("retained.tests.case-result-invalid")
    if (
        tuple(sorted(passed)) != expected.must_run
        or tuple(sorted(ignored, key=lambda x: x.name)) != expected.ignored
    ):
        raise ValueError("retained.tests.case-result-mismatch")
    return RetainedCargoLibtestObservationV2(expected.must_run, expected.ignored)
