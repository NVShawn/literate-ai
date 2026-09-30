// LOW-layer Design-by-Contract example, modeled on samples/critical-path-scheduler.
//
// Mirrors the shape of that sample Component's internal implementation: a forward pass
// computes each task's earliest finish time, a backward pass computes each task's
// latest finish time, and slack = late_finish - early_finish must never be negative for
// a correctly-computed schedule. `slack_for_task` is an internal, dense/algorithmic
// helper -- not the Component's public entry point (that is covered end-to-end by
// source/tests/manifest.json's black-box cases) -- whose violation would silently
// produce a wrong-but-plausible schedule rather than an obvious crash, which is exactly
// the case the LOW-layer Design-by-Contract paragraph added to
// rust-portable-json-application/SKILL.md describes.
//
// Build and run with a direct rustc invocation (matching this skill's "compilable by
// one direct rustc invocation" rule); see the companion NOTES.md for the exact commands
// and captured output, including the debug/release `debug_assert!` split.

use std::collections::HashMap;

/// Internal invariant: for any correctly-computed forward/backward pass over a valid
/// DAG, late_finish[task_id] >= early_finish[task_id] always holds. Violating this
/// means the forward or backward pass has a bug, not that the input schedule is
/// infeasible (infeasible input is rejected earlier, before this helper runs).
fn slack_for_task(task_id: i64, early_finish: &HashMap<i64, i64>, late_finish: &HashMap<i64, i64>) -> i64 {
    let early = *early_finish
        .get(&task_id)
        .unwrap_or_else(|| panic!("slack_for_task: unknown task_id {} in early_finish", task_id));
    let late = *late_finish
        .get(&task_id)
        .unwrap_or_else(|| panic!("slack_for_task: unknown task_id {} in late_finish", task_id));
    let slack = late - early;
    assert!(
        slack >= 0,
        "slack_for_task: negative slack ({}) for task {}; late_finish must never precede early_finish for a correctly-computed pass",
        slack,
        task_id
    );
    slack
}

/// Public-shaped entry point: tasks with zero slack form the critical subgraph.
fn critical_path_task_ids(early_finish: &HashMap<i64, i64>, late_finish: &HashMap<i64, i64>) -> Vec<i64> {
    let mut ids: Vec<i64> = early_finish
        .keys()
        .copied()
        .filter(|task_id| slack_for_task(*task_id, early_finish, late_finish) == 0)
        .collect();
    ids.sort();
    ids
}

fn demo_valid_case() {
    // Correctly-computed forward/backward pass over a 3-task chain A -> B -> C.
    let early_finish: HashMap<i64, i64> = HashMap::from([(1, 3), (2, 7), (3, 10)]);
    let late_finish: HashMap<i64, i64> = HashMap::from([(1, 3), (2, 7), (3, 10)]);
    let result = critical_path_task_ids(&early_finish, &late_finish);
    println!("valid case: critical_path_task_ids = {result:?}");
    assert_eq!(result, vec![1, 2, 3]);
}

fn demo_violation_case() {
    // Deliberately corrupt late_finish for task 2 so late_finish < early_finish --
    // simulates a bug in the backward pass that produced an internally inconsistent
    // schedule. The assert! in slack_for_task exists precisely to catch this class of
    // bug loudly instead of letting critical_path_task_ids silently return a
    // wrong-but-plausible result.
    let early_finish: HashMap<i64, i64> = HashMap::from([(1, 3), (2, 7), (3, 10)]);
    let late_finish: HashMap<i64, i64> = HashMap::from([(1, 3), (2, 5), (3, 10)]); // bug
    critical_path_task_ids(&early_finish, &late_finish);
}

fn main() {
    demo_valid_case();
    println!("now triggering the contract violation on purpose...");
    demo_violation_case();
    println!("UNREACHABLE: the assert! above should have panicked");
}
