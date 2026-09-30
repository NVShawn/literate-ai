# Contributing

## Test observable behavior first

Prefer contract and component/integration tests through supported public CLI or Python
APIs. Cover request and response schemas, exit/status codes, documented errors,
backward compatibility, and applicable authentication or authorization behavior. Use
real internal components and realistic persistence; mock only true external boundaries
such as networks, host tools, credentials, clocks, and model providers.

Exercise important stateful behavior as complete scenarios, including relevant
transitions and recovery. Give distinct coverage to malformed input, missing data,
duplicate and idempotent requests, authorization failures, timeouts, retries, partial
failures, boundaries, and concurrency hazards. Use parameterized or property-based
tests when they express related inputs more economically.

Retain method-level unit tests only when isolated logic is complex, safety-critical,
algorithmically subtle, or substantially easier to diagnose independently. Do not add
tests merely for count or line coverage, duplicate the same behavior at multiple
layers, assert internal call sequences, test trivial wrappers/getters, or build
mock-heavy replicas of the implementation. Every test should protect a distinct
contract, failure mode, boundary, or regression.

## Before you open a pull request

Every pull request must contain exactly one release-classification line in its body:

```text
Literate-AI-Release: major.minor
```

Use the active Pre-release target, such as `Literate-AI-Release: 0.8`, when the pull
request belongs on that release line; use `Literate-AI-Release: none` for ordinary work.
Missing, duplicate, malformed, or non-current release values are unknown rather than
silently inferred. Ordinary new work always targets writable `main`. After a release
line is cut, only release-critical fixes belong there, and the fix must land on `main`
first before an authorized backport.

Only identities listed under [`README.md`'s Release Engineers](README.md#release-engineers)
may merge release-line pull requests or create/publish a major or minor release. A
project configured with loose patch authority may additionally permit a listed writer
to perform a break-glass patch operation with an explicit reason; this does not grant
major/minor authority and does not waive the trunk-first rule. LitAI enforces this in its
release commands. Forge branch protection is a separate control and should mirror the
same restrictions; do not assume it is active without verifying the forge settings.

This repository is self-hosting: `litai`, the tool this repository builds, is used
to validate this repository itself. Two of its checks are easy to miss if you don't
already know they exist, and both will fail CI even when your actual change is
correct:

### 1. The lifecycle-driver TCB pin

`literate.project.json` pins a content hash (`driver.pinned_identity`) over every
file under the declared `implementation_paths` — the framework's trusted computing
base. If your change touches anything under `src/literate_ai/` (or another declared
implementation path), that pin goes stale and `make driver-review` (part of
`make python-check`) fails with a message naming the drifted files.

Fix it by re-pinning after you've reviewed what changed:

```
make driver-review-record
```

This updates `literate.project.json`'s pinned identity to match your change. Commit
that file alongside your code change — re-pinning is not optional or automatic in
CI; it has to already be current in your commit.

### 2. The documentation-authority marker

`docs/architecture/design-traceability.md` carries a marker comment:

```
<!-- literate-ai:authority-reviewed sha256:... -->
```

This is a content hash over the project's documentation authority (skill/Component/Flavor
definitions and declared docs other than the tactical `docs/roadmap/` queue). If your
change touches `docs/` outside that queue, `literate.project.json`, or anything else that
feeds this computation, the marker goes stale and `make documentation-review` fails.

Fix it by recording the current reviewed identity after you have reviewed the
changed authority:

```
make documentation-review-record
```

That target runs `litai project documentation-review . --record`, replaces exactly
one placeholder or stale marker, and verifies the result is current. Do not
transcribe a digest by hand. If recording fails because a marker is missing or
duplicated, fix that document first; the command does not write in those cases.
Then re-run `make documentation-review` (read-only) to confirm `"state": "current"`.

## Before every commit

Run the full gate locally so CI doesn't surprise you:

```
make python-check
```

This runs the project's Python test suite plus both checks above (`driver-review`,
`documentation-review`), along with lint and formatting checks. If it passes locally,
CI should pass too.

At cycle boundaries, inspect peer work with `litai project peer-work survey --when
start|end`. Before deleting a local branch, record an exact `merged` or reason-bearing
`dead` lifecycle marker with `litai project peer-work mark`; then run `peer-work gc`
without `--apply` first. Collection rejects stale markers, open pull requests, release or
default branches, unmerged heads, and any dirty worktree or repository state. Actual
deletion additionally requires `--apply --authorize-delete`.

## Why this exists

Literate AI treats its own repository as a project managed by itself — the same
provenance and authority mechanisms it provides to projects built with it apply here
first. See `docs/architecture/design-traceability.md` and
`docs/user/troubleshooting.md` for more on how these checks work and how to recover
from a stale pin or marker mid-development, not just before a commit.
