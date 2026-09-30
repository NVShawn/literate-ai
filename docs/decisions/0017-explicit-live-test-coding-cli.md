# ADR 0017: Live Qualification Pins One Coding CLI and Model from User Config

- Status: Accepted
- Date: 2026-08-26
- Accepted: 2026-08-26
- Decision owners: literate-ai maintainers
- Roadmap: [GENERATION-011](../roadmap/active-work.md#generation-011-pin-live-qualification-to-an-explicit-user-configured-cli-and-model)

> **0.9.0 amendment:** [ADR 0031](0031-durable-user-configuration-home.md) moves this
> file from the ignored project root to project-scoped durable user configuration. The
> selection and precedence decision below is unchanged.

## Context

`--target github` for `0.7.0` is green at `a924ef51`. `--target local` cannot close:
POSIX SSH workers fail `installed-project-e2e --live` (and would then fail `samples`)
with `coding_cli.authentication_required`. Cursor, Claude, and Codex on those hosts
depend on interactive login sessions that do not survive the SSH login environment used
by release fan-out. OpenCode already accepts `OPENAI_API_KEY` in process environment and
an explicit `--model` argument, which is the transport that actually works on those
workers.

Today `select_coding_cli` still walks a PATH priority list (`codex`, `claude`,
`cursor-agent`, `opencode`, or `LITERATE_AI_CODING_CLI_PRIORITY`) and
`GENERATION-008` can fall through to the next CLI after a quota or authentication
denial. `CODING_CLI=<name>` pins a launcher, and `--model` already exists on sample
and planning commands, but live qualification does not *require* either. Two hosts with
the same revision can therefore generate with different launchers and models, and a
release gate can change provider mid-run. That is too failure-prone for tests that must
pass reliably.

The repository must not become opinionated about *which* vendor a maintainer prefers.
It must become opinionated that live qualification names one launcher and one model
explicitly, because the run is expensive and the result is otherwise not comparable.

Manual one-off runs still need a way to point at any supported provider and model
without editing the durable default.

## Decision

### 1. Live qualification requires an explicit launcher and model

`installed-project-e2e --live`, `make samples`, and any other live generation that is
part of `make release-check` fail closed unless both a coding CLI and a model selector
are bound before the first model egress. Absence is
`coding_cli.test_selection_unconfigured` (or the existing typed equivalent). The first
PATH-available CLI is not a test default. `GENERATION-008` fallback is disabled for
this path: a pinned live run does not silently change provider.

Non-live gates, unit tests, and GitHub `make validate` are unchanged. They do not
invoke a coding CLI.

### 2. The durable default lives in ignored user configuration, not the repository

The ignored `literate.test.json` (already the operator-owned test matrix, never
committed) grows required live-generation fields: coding CLI name and model selector.
`literate.test.example.json` documents the keys with inert placeholders. Project
authority (`literate.project.json`, committed samples, Flavors, skills) does not name a
vendor CLI or a spend-bearing model.

An incomplete or unknown CLI name fails closed. Secrets never enter this file;
`OPENAI_API_KEY` and sibling credentials stay in the process environment, as they do
today.

### 3. Command-line flags override that file for one invocation

A maintainer may still run any supported provider and any model by overriding the
file's defaults on the command that starts the test:

1. `--coding-cli` and `--model` on that invocation
2. then `CODING_CLI` / the existing model environment, when those flags are omitted
3. then `literate.test.json`
4. never PATH search and never first-available fallback

The override applies only to that process tree. It does not rewrite the JSON file and
does not become repository authority. Interactive `litai generate` / `litai rebuild` on
a developer workstation (the "local worker" of this session) may keep today's
user-chosen harnesses; that path is not the pinned fan-out test.

### 4. Remote pinned fan-out hard-wires OpenCode plus `OPENAI_API_KEY`

SSH workers that run the live release gates treat this as a prerequisite, not a guess:

- `opencode` is on `PATH`
- `OPENAI_API_KEY` is present in the worker login environment that
  `BoundedSshProcessRunner` already forwards through the OpenCode allowlist
- the bound model is passed as OpenCode's `--model` argument (already constructed by
  `_coding_cli_command`)

Cursor/Claude/Codex interactive login is not a remote-worker transport. A remote live
gate whose bound CLI is not `opencode`, or whose environment lacks `OPENAI_API_KEY`,
fails with a typed prerequisite error before model egress. A command-line override from
decision 3 remains valid for a *manual* remote experiment; it is not the release
fan-out default.

Do not copy a credential cache between machines. Do not put keys in
`literate.workers.json`.

### 5. Interactive local use is not this contract

Selecting Cursor, Claude, Codex, or OpenCode in an attended session stays the
operator's choice. `select_coding_cli`'s PATH order and `GENERATION-008` fallback remain
for that attended path until a later ADR changes them. This ADR only binds live
qualification and release fan-out.

### 6. Session start logs the user default and a distinct CLI override

Live qualification logs the user-default provider/model tuple from
`literate.test.json` at session start. When `--coding-cli` / `--model` override that
tuple, a distinct `model.session.cli_override` event records what the CLI args
dictated.

The runtime cursor those logs describe is the never-empty per-agent model stack in
[ADR 0018](0018-never-empty-per-agent-model-stack.md) (Accepted for `0.7.0`). This
ADR only binds how live tests choose frame 0.

## Consequences

- `0.7.0` `--target local` can close once POSIX workers have `opencode`,
  `OPENAI_API_KEY` in the SSH login environment, and `literate.test.json` names
  `opencode` plus an explicit model.
- Two live runs of the same revision are comparable: same launcher, same model, unless
  the operator overrode both on the command line and recorded that in the invocation.
- Maintainers keep a one-shot escape hatch (`--coding-cli` / `--model`) for experiments
  without making the repository vendor-specific.
- Generating a passing live result now costs an explicit, durable choice. That is
  intended.

Rejected alternatives:

- Keep first-available PATH selection for tests (variable results; this session's
  failure mode).
- Commit a CLI/model pin in `literate.project.json` (makes the repository
  vendor-opinionated and spend-bearing).
- Probe every coding CLI until one authenticates (expensive, non-deterministic, and
  still session-fragile on SSH workers).
- Copy Codex/Claude/Cursor credential caches onto workers (forbidden by existing
  installation guidance; those sessions still time out).
