# ADR 0018: Coding Sessions Keep an Explicit Never-Empty Per-Agent Model Stack

- Status: Accepted
- Date: 2026-08-26
- Accepted: 2026-08-26
- Decision owners: literate-ai maintainers
- Roadmap: [GENERATION-010](../roadmap/active-work.md#x-generation-010-keep-an-explicit-never-empty-per-agent-model-stack)
- Related: [ADR 0017](0017-explicit-live-test-coding-cli.md) supplies the live-test
  session model that becomes frame 0 on that path. This ADR is the stack itself.
- Release: `0.7.0`

## Context

Lexical model selection already exists as an immutable parent-linked binding
(`ModelScopeKind`: pipeline → component → flavor-role → skill-invocation). That
binding is the lock and cache identity. It is not a runtime cursor an author can
push, pop, or query from a specification, and it is not isolated per concurrent
agent.

During the `0.7.0` live-qualification work the directing maintainer required a
real stack:

- the initial CLI or user-default model is frame 0 and the stack is never empty
- Component, Flavor, skill, and in-spec sectional changes **push** when the model
  changes and **pop** only when leaving that scope
- generation uses the top of the stack
- push, pop, and depth are part of the specification language, so a Component can
  push a model for a section and the spec-to-code converter uses it
- each thread and each agent has its own stack; a process-global list is forbidden
- popping the only remaining frame is a **warning**, not a pop, and the log names
  the precise caller so the mismatch is debuggable

That is a significant design under
[ADR 0006](0006-significant-feature-request-governance.md): more than one
defensible answer exists (infer silently from the lexical binding; allow an empty
stack; fail closed on a last-frame pop; put sectional ops into
`literate-markdown@2`). Implementation started from chat while finishing
[GENERATION-011](../roadmap/active-work.md#generation-011-pin-live-qualification-to-an-explicit-user-configured-cli-and-model).
This ADR records the decision so `0.7.0` can include it as committed scope rather
than as an undocumented side effect of the live-test pin.

## Decision

### 1. Each coding session owns a `contextvars` model stack

The converter keeps the stack in `contextvars`, not a process-global list. Copying
a context (a thread, an asyncio task, or an agent) copies that agent's stack.
There is no shared mutable stack across agents.

The existing lexical `ModelScopeBinding` remains the immutable lock/cache identity.
The stack is the runtime and specification-facing cursor. Generation passes the
top-of-stack model to the coding CLI.

### 2. Frame 0 is the session model; depth never reaches 0

`begin_session` installs exactly one frame. That frame is the live-test pin from
ADR 0017, a `--model` pipeline value, or the coding CLI's configured default. After
a session starts, depth is at least 1 for the rest of that context.

Querying depth or current without a session fails closed. It does not return 0 and
does not invent an empty stack.

### 3. Nested scopes push when the model changes and pop when leaving

Entering a Component, Flavor role, skill invocation, or authored section **pushes**
when the model (or coding CLI) actually changes. Leaving that scope **pops**.
Siblings therefore see the restored enclosing model. An explicit specification
`push` always pushes, even if the selector matches the current top, because the
author asked to establish a frame they can later pop.

### 4. Push, pop, and depth are specification language

Authors write stack operations in Component or Flavor markdown:

```markdown
<!-- literate-ai:model-stack op="push" model="openai/o3" section="algorithm" -->
...
<!-- literate-ai:model-stack op="pop" section="algorithm" -->
```

`op` may be `push`, `pop`, `depth`, or `current`. `depth` and `current` are
queries: they read the stack and do not mutate it. Additive
`literate-ai/coding-model-selection@2` JSON may carry an ordered `stack` array of
the same operations. `@1` model-selection documents stay valid and do not grow new
required keys.

The spec-to-code converter interprets those operations. They are not prompt
decoration for the coding CLI.

This language is not `literate-markdown@2` ([ADR 0011](0011-component-domain-structure-literate-markdown-v2.md),
still Proposed). Sectional model changes do not wait on that domain-structure track.

### 5. Popping the only frame is a warning with a precise location

A pop that would empty the stack is refused. The session model stays in place,
depth stays 1, and the runtime emits `ModelStackPopRefusedWarning` plus a
`model.stack.pop_refused` log event. That event records:

- the session coding CLI and model
- the Python caller (file, line, function, and a short call chain)
- any specification location the pop named (source path, line, section)

Do not fail the generation as a hard error: a mismatched pop is a defect the author
must see and locate, not a reason to pretend the session has no model.

## Rejected alternatives

### Process-global stack

Rejected. Concurrent threads and agents would clobber each other's model.

### Infer push/pop only from `resolve_model_scope`

Rejected. The lexical binding is the lock identity. Authors must be able to push
inside a Component section and query depth from the specification language.

### Allow depth 0

Rejected. Generation with no current model is an undefined cursor. Frame 0 is the
session default, including the coding CLI's configured default when nothing else is
pinned.

### Fail closed on a last-frame pop

Rejected by the directing maintainer: warn, keep the session model, and log the
precise location.

### Fold sectional model ops into `literate-markdown@2`

Rejected for `0.7.0`. ADR 0011 is a different domain-structure track and is still
Proposed. HTML-comment directives and additive `@2` model-selection JSON are enough
for this cursor.

## Consequences

- `0.7.0` ships the stack as product behavior: live and
  attended generation share one cursor model, isolated per agent.
- A Component can change model for a section without rewriting Flavor JSON or the
  pipeline `--model`.
- A pop that does not match a push is visible in logs with a file:line the author
  can open.
- Live-test session start still logs the user-default tuple and
  `model.session.cli_override` as required by ADR 0017; those events are not this
  stack.

## Acceptance Criteria

- The directing maintainer explicitly accepted this ADR on 2026-08-26.
- Unit tests prove the stack never empties after `begin_session`, threads and
  copied contexts isolate, and a last-frame pop warns with caller file, line,
  function, and specification location.
- Specification-language `push` / `pop` / `depth` / `current` parse from markdown
  directives (and `@2` JSON when present) and drive the converter cursor.
- The `0.7.0` changelog names the never-empty stack and the last-frame warning.
