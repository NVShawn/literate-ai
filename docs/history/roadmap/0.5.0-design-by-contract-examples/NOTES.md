# LOW-DBC-001 implementation evidence

- **Status:** historical
- **Owning queue item:** [LOW-DBC-001](../../../roadmap/active-work.md#x-low-dbc-001-scope-the-low-layer-design-by-contract-skill-convention)
- **Completion / archival evidence:** [python_example.py](python_example.py)
  and the byte-equivalence regression cited by the owning queue item.

These three files are hand-authored, executed validation for the LOW-layer
Design-by-Contract skill paragraphs added to `python-portable-application/SKILL.md`,
`rust-portable-json-application/SKILL.md`, and `go-portable-application/SKILL.md` (both
the `skills/` catalog copy and the `src/literate_ai/project_template/skills/` copy — the
two are kept byte-identical). This directory is the retained execution evidence for the
completed queue item.

## Why hand-authored rather than real Component regeneration

The design doc's recommended validation path is "run one real Component through
generation with and without the new paragraphs to produce before/after evidence." That
requires an authenticated coding CLI driving `litai rebuild`. Checked availability
before choosing a path:

```
$ which codex claude cursor-agent opencode
/opt/homebrew/bin/codex
/Users/jordanh/.local/bin/claude
/Users/jordanh/.local/bin/cursor-agent
/opt/homebrew/bin/opencode

$ timeout 20 codex exec "print hi"
...
ERROR: failed to refresh OAuth tokens for server maas_ngc
ERROR: You hit your spend cap set by the owner of your workspace. Ask an owner to
increase your spend cap to continue.

$ timeout 20 cursor-agent -p "print hi"
Workspace Trust Required — Cursor Agent can execute code and access files in this
directory. Pass --trust/--yolo/-f to proceed non-interactively.
```

`codex` is installed and has valid-looking credentials but is blocked by an
organization-level spend cap in this environment (not a missing-auth error — a policy
block that a `--allow-host-execution` `litai rebuild` run would hit the same way). This
is a real, checked "not feasible here" result, not an assumption. Per the task's
documented fallback, the examples below are hand-authored and — critically — actually
executed, with the invariant violation actually firing, rather than left as
plausible-looking but unexecuted snippets.

## What each example demonstrates

All three examples share one scenario, deliberately modeled on the actual algorithm in
`samples/critical-path-scheduler/component.md` (a forward pass computes each task's
earliest finish time, a backward pass computes each task's latest finish time, and
`slack = late_finish - early_finish` must never be negative for a correctly-computed
schedule). Each example contains:

- `slack_for_task` / `slackForTask`: an internal, dense/algorithmic helper — not the
  Component's public entry point — whose invariant (`slack >= 0`) is exactly the shape
  the design doc's §4 "reach for a contract when" criteria describe: cheap to check
  (O(1)), a caller-bug/broken-precondition signal (a bug in the forward/backward pass,
  not an ordinary domain-validation failure), and silently-wrong-not-obviously-crashing
  if left unchecked (an unchecked negative slack would still produce a schedule that
  *looks* like valid output).
- A valid-case demo that computes the correct result and asserts it matches expectation.
- A violation-case demo that deliberately corrupts one internal value the way a bug in
  the backward pass would, then calls into the guarded helper — the contract mechanism
  fires exactly as the corresponding skill paragraph specifies.

## Python — [`python_example.py`](python_example.py)

Command and full output:

```
$ python3 python_example.py
valid case: critical_path_task_ids = [1, 2, 3]
now triggering the contract violation on purpose...
Traceback (most recent call last):
  File ".../python_example.py", line 68, in <module>
    _demo_violation_case()
  File ".../python_example.py", line 62, in _demo_violation_case
    critical_path_task_ids(early_finish, late_finish)
  File ".../python_example.py", line 42, in critical_path_task_ids
    return [task_id for task_id in early_finish if _slack_for_task(task_id, early_finish, late_finish) == 0]
  File ".../python_example.py", line 33, in _slack_for_task
    assert slack >= 0, (
           ^^^^^^^^^^
AssertionError: _slack_for_task: negative slack (-2) for task 2; late_finish must never precede early_finish for a correctly-computed pass
```
Exit code: `1`.

The skill paragraph also states Python's `assert` is stripped under `-O`, and that
external-input validation must not rely on `assert` for that reason. Confirmed directly:

```
$ python3 -O python_example.py
valid case: critical_path_task_ids = [1, 2, 3]
now triggering the contract violation on purpose...
UNREACHABLE: the assertion above should have raised AssertionError
```
Exit code: `0` — the assertion silently does not fire under `-O`, exactly as the skill
paragraph warns, which is why the paragraph tells authors never to use `assert` for
externally-sourced input validation.

## Rust — [`rust_example.rs`](rust_example.rs)

Compiled with `--edition 2021` per this skill's "Rust 2021 source" requirement, once in
release mode (`-O`) and once in debug mode, to demonstrate the skill paragraph's claim
that `assert!` (unlike `debug_assert!`) compiles unconditionally into both:

```
$ rustc --edition 2021 -O rust_example.rs -o /tmp/rust_example_release
$ rustc --edition 2021 rust_example.rs -o /tmp/rust_example_debug
$ /tmp/rust_example_release
valid case: critical_path_task_ids = [1, 2, 3]
now triggering the contract violation on purpose...

thread 'main' (...) panicked at rust_example.rs:31:5:
slack_for_task: negative slack (-2) for task 2; late_finish must never precede early_finish for a correctly-computed pass
note: run with `RUST_BACKTRACE=1` environment variable to display a backtrace
$ echo $?
101
$ /tmp/rust_example_debug
valid case: critical_path_task_ids = [1, 2, 3]
now triggering the contract violation on purpose...

thread 'main' (...) panicked at rust_example.rs:31:5:
slack_for_task: negative slack (-2) for task 2; late_finish must never precede early_finish for a correctly-computed pass
note: run with `RUST_BACKTRACE=1` environment variable to display a backtrace
$ echo $?
101
```

Both the release and debug builds panic identically, confirming the paragraph's claim
that `assert!` (as opposed to `debug_assert!`) is not compiled out in release builds.

## Go — [`go_example.go`](go_example.go)

Run both via `go run` and via a separately built binary (`go build`), matching this
skill's "compilable ... with no `go.mod`" shape:

```
$ go run go_example.go
valid case: criticalPathTaskIDs = [1 2 3]
now triggering the contract violation on purpose...
panic: slackForTask: negative slack (-2) for task 2; lateFinish must never precede earlyFinish for a correctly-computed pass

goroutine 1 [running]:
main.slackForTask(...)
	.../go_example.go:37 +0xd0
main.criticalPathTaskIDs(...)
	.../go_example.go:49 +0xd4
main.demoViolationCase()
	.../go_example.go:76 +0x16c
main.main()
	.../go_example.go:82 +0x54
exit status 2
```

`go build -o /tmp/go_example_bin go_example.go && /tmp/go_example_bin` reproduces the
same panic and stack trace (exit status `2`), confirming the behavior is identical
whether run via `go run` or as a standalone compiled binary.

## Toolchain versions used

```
$ python3 --version
Python 3.14.6
$ rustc --version
rustc 1.94.1 (e408947bf 2026-03-25)
$ go version
go version go1.26.4 darwin/arm64
```
