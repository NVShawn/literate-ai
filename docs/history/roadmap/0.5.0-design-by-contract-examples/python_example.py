"""LOW-layer Design-by-Contract example, modeled on samples/critical-path-scheduler.

This mirrors the shape of that sample Component's internal implementation (a forward
pass computes each task's earliest finish time, a backward pass computes each task's
latest finish time, and slack = late_finish - early_finish must never be negative for a
correctly-computed schedule). ``_slack_for_task`` is exactly the kind of internal,
dense/algorithmic helper the LOW-layer Design-by-Contract paragraph added to
``python-portable-application/SKILL.md`` describes: it is not the Component's public
entry point (that is covered end-to-end by ``source/tests/manifest.json``'s black-box
cases), a violation of its invariant would silently produce a wrong-but-plausible
schedule rather than an obvious crash, and the check is O(1) relative to the function's
own cost.

Run directly to see both the passing case and the assertion firing on a corrupted
internal array (see the companion NOTES.md for the exact command and captured output).
"""

from __future__ import annotations


def _slack_for_task(task_id: int, early_finish: dict[int, int], late_finish: dict[int, int]) -> int:
    """Return the slack for ``task_id`` given already-computed forward/backward passes.

    Internal invariant: for any correctly-computed forward/backward pass over a valid
    DAG, late_finish[task_id] >= early_finish[task_id] always holds -- a task's latest
    allowable finish can never be earlier than its earliest possible finish. Violating
    this would mean the forward or backward pass has a bug, not that the input schedule
    is infeasible (infeasible input is rejected earlier, before this helper runs).
    """
    assert task_id in early_finish, f"_slack_for_task: unknown task_id {task_id!r} in early_finish"
    assert task_id in late_finish, f"_slack_for_task: unknown task_id {task_id!r} in late_finish"
    slack = late_finish[task_id] - early_finish[task_id]
    assert slack >= 0, (
        f"_slack_for_task: negative slack ({slack}) for task {task_id!r}; "
        "late_finish must never precede early_finish for a correctly-computed pass"
    )
    return slack


def critical_path_task_ids(early_finish: dict[int, int], late_finish: dict[int, int]) -> list[int]:
    """Public-shaped entry point: tasks with zero slack form the critical subgraph."""
    return [task_id for task_id in early_finish if _slack_for_task(task_id, early_finish, late_finish) == 0]


def _demo_valid_case() -> None:
    # Correctly-computed forward/backward pass over a 3-task chain A -> B -> C.
    early_finish = {1: 3, 2: 7, 3: 10}
    late_finish = {1: 3, 2: 7, 3: 10}
    result = critical_path_task_ids(early_finish, late_finish)
    print(f"valid case: critical_path_task_ids = {result}")
    assert result == [1, 2, 3]


def _demo_violation_case() -> None:
    # Deliberately corrupt late_finish for task 2 so late_finish < early_finish --
    # simulates a bug in the backward pass that produced a schedule that is
    # internally inconsistent. This must never happen for correct code; the assertion
    # in _slack_for_task exists precisely to catch this class of bug loudly instead of
    # letting critical_path_task_ids silently return a wrong-but-plausible result.
    early_finish = {1: 3, 2: 7, 3: 10}
    late_finish = {1: 3, 2: 5, 3: 10}  # bug: late_finish[2] < early_finish[2]
    critical_path_task_ids(early_finish, late_finish)


if __name__ == "__main__":
    _demo_valid_case()
    print("now triggering the contract violation on purpose...")
    _demo_violation_case()
    print("UNREACHABLE: the assertion above should have raised AssertionError")
