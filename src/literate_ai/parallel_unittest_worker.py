"""Internal isolated unittest worker. Only its parent writes repair checkpoints."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from literate_ai.parallel_unittests import PROTOCOL
from literate_ai.test_checkpointing import (
    _failure_identity,
    _flatten,
    _unexpected_success_identity,
)


def main() -> int:
    request_path = Path(sys.argv[1])
    if request_path.stat().st_size > 1024 * 1024:
        raise ValueError("worker request exceeds its bound")
    request = json.loads(request_path.read_text(encoding="utf-8"))
    if request.get("schema") != PROTOCOL:
        raise ValueError("invalid worker request schema")
    root = Path(request["root"]).resolve(strict=True)
    cases = request["cases"]
    if not isinstance(cases, list) or not cases:
        raise ValueError("worker requires selected cases")
    indices = [case["index"] for case in cases]
    names = [case["id"] for case in cases]
    if any(type(i) is not int or i < 0 for i in indices) or len(set(indices)) != len(
        indices
    ):
        raise ValueError("invalid selected indices")
    if any(not isinstance(name, str) or len(name) > 4096 for name in names):
        raise ValueError("invalid selected test IDs")
    sys.path.insert(0, str(root))
    tempfile.tempdir = None
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromNames(names)
    tests = tuple(_flatten(suite))
    if loader.errors or [test.id() for test in tests] != names:
        raise ValueError("selected tests did not reload exactly")
    index_by_object = {
        id(test): index for test, index in zip(tests, indices, strict=True)
    }
    completed: set[int] = set()
    skipped_subtests: set[int] = set()
    sequence = 0
    with (request_path.parent / "events.jsonl").open(
        "w", encoding="utf-8", buffering=1
    ) as events:

        def emit(
            kind: str, test: unittest.TestCase | None = None, **values: object
        ) -> None:
            nonlocal sequence
            record = {"schema": PROTOCOL, "sequence": sequence, "kind": kind, **values}
            if test is not None:
                record["description"] = test.id()
            events.write(json.dumps(record, ensure_ascii=True) + "\n")
            events.flush()
            sequence += 1

        class Result(unittest.TestResult):
            failfast = True

            def startTest(self, test):  # noqa: N802
                super().startTest(test)
                emit("start", test, indices=[index_by_object[id(test)]])

            def stopTest(self, test):  # noqa: N802
                index = index_by_object[id(test)]
                if index in skipped_subtests and index not in completed:
                    self.outcome("subtest_end", test, "skipped")
                super().stopTest(test)

            def outcome(self, kind, test, status, **values):
                index = index_by_object.get(id(test))
                if index is None:
                    # unittest represents module/class setup and teardown failures
                    # with _ErrorHolder, outside the selected case object set.
                    scope = test.id().partition(" (")[2].removesuffix(")")
                    selected = [
                        i
                        for i, name in zip(indices, names, strict=True)
                        if name.startswith(scope + ".")
                    ]
                    if not selected:
                        raise ValueError(
                            "fixture outcome does not match selected cases"
                        )
                    if kind == "skip":
                        selected = [i for i in selected if i not in completed]
                        if not selected:
                            raise ValueError("fixture skip followed completed cases")
                        kind = "fixture_skip"
                    else:
                        values["invalidate"] = list(selected)
                        selected = [
                            next(
                                (i for i in selected if i not in completed),
                                selected[-1],
                            )
                        ]
                        kind = "fixture_error"
                else:
                    selected = [index]
                if status is not None:
                    completed.update(selected)
                emit(kind, test, indices=selected, status=status, **values)

            def addSuccess(self, test):  # noqa: N802
                super().addSuccess(test)
                self.outcome("success", test, "passed")

            def addSkip(self, test, reason):  # noqa: N802
                super().addSkip(test, reason)
                parent = getattr(test, "test_case", None)
                if parent is not None:
                    skipped_subtests.add(index_by_object[id(parent)])
                self.outcome(
                    "subtest_skip" if parent else "skip",
                    parent or test,
                    None if parent else "skipped",
                    reason=str(reason)[:4096],
                )

            def addExpectedFailure(self, test, err):  # noqa: N802
                super().addExpectedFailure(test, err)
                self.outcome(
                    "expected_failure",
                    test,
                    "skipped",
                    detail=self._exc_info_to_string(err, test)[:32768],
                )

            def failure(self, kind, test, err):
                self.outcome(
                    kind,
                    test,
                    "failed",
                    failure_identity=_failure_identity(err).uri,
                    detail=self._exc_info_to_string(err, test)[:32768],
                )

            def addFailure(self, test, err):  # noqa: N802
                super().addFailure(test, err)
                self.failure("failure", test, err)

            def addError(self, test, err):  # noqa: N802
                super().addError(test, err)
                self.failure("error", test, err)

            def addSubTest(self, test, subtest, err):  # noqa: N802
                super().addSubTest(test, subtest, err)
                if err is not None:
                    kind = (
                        "subtest_failure"
                        if issubclass(err[0], test.failureException)
                        else "subtest_error"
                    )
                    self.failure(kind, test, err)

            def addUnexpectedSuccess(self, test):  # noqa: N802
                super().addUnexpectedSuccess(test)
                self.outcome(
                    "unexpected_success",
                    test,
                    "failed",
                    failure_identity=_unexpected_success_identity().uri,
                )

        result = Result()
        result.failfast = True
        suite.run(result)
        emit("done", successful=result.wasSuccessful())
        return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
