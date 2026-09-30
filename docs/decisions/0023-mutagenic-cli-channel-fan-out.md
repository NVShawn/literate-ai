# ADR 0023: Mutagenic CLI Events Fan Out to Jira, Slack, and Outlook

- Status: Accepted
- Date: 2026-08-28
- Accepted: 2026-08-28
- Decision owners: literate-ai maintainers
- Roadmap: [USER-MCP-001](../roadmap/active-work.md#x-user-mcp-001-operator-local-mcp-catalog-offer-setup-jira-only-when-available),
  [SLACK-NOTIFY-001](../roadmap/active-work.md#x-slack-notify-001-optional-slack-channel-for-significant-mutagenic-litai-events),
  [OUTLOOK-MAIL-001](../roadmap/active-work.md#x-outlook-mail-001-outlook-mcp-for-status-mail-and-inbound-project-work)
- Release: `0.8.0`
- GitHub: [#191](https://github.com/NVIDIA-dev/literate-ai/issues/191),
  [#194](https://github.com/NVIDIA-dev/literate-ai/issues/194),
  [#196](https://github.com/NVIDIA-dev/literate-ai/issues/196)

> **0.9.0 amendment:** [ADR 0031](0031-durable-user-configuration-home.md) moves event
> journals from configuration into the platform-resolved user state root. The event and
> fan-out contract below is unchanged.

## Context

A project needs institutional memory of significant `litai` mutations: who ran what,
on which revision, with what outcome. That belongs on the associated Jira ticket, an
optional Slack channel, and optional Outlook status mail — the same event, one
vocabulary ([ADR 0022](0022-channel-author-recipient-vocabulary.md)). Outlook must
also accept inbound `recipient` mail as queued work.

`litai` does not speak MCP. Agents do. The CLI still runs without an agent.

## Decision

### 1. One mutagenic classifier

Mutagenic (notify): `init`, `update`, `reparent`, `catalog.copy`, writing `lock`,
`generate`, writing `rebuild`, `release.prepare`, `release.publish`,
`release.advance-default-branch`.

Not mutagenic: `help`, `version.check`, read-only `release.plan`, `project.validate`,
tracker inspect, probes, documentation-review without `--record`, `lock --check`.

`release.publish` also emits `kind: publish-summary` for completed queue items when a
Jira ticket is associated.

### 2. CLI journal and CLI post

On mutagenic success, Python appends an **author** envelope to
`$HOME/.config/literate-ai/events/` (same directory root as [ADR 0021](0021-operator-local-mcp-catalog.md))
and fan-outs through operator-catalog MCPs
([ADR 0025](0025-cli-operator-mcp-client.md)). Journal or MCP write failure is
fail-open with a stderr skip; it never rolls back the command.

Coding sessions that already ran `litai` do not post the same envelope again.
`associate-release-jira` associates the major/minor ticket and records
`institutional_channels.jira_issue`. There is no persistent Literate AI daemon
or mailbox poller. Channel ids may live in optional project policy; tokens stay
operator-local. Unregistered / unavailable channels skip.

### 3. Jira association

At `release plan` (including a first `0.0.x` cut), ask whether a Jira Epic, Story, or
Task already tracks that major/minor, or should be created. The developer chooses the
type. If it is an Epic, later release Tasks are children via Parent (not Sub-task, not
Epic Link). Story/Task association comments only; it cannot parent other Stories/Tasks.
Patch cuts comment on the same major/minor ticket. Jira Version/Fix Version is distinct
from Epic. No associated ticket means skip comments, not a new issue per command.

### 4. Inbound Outlook

Inbound mail with a valid `recipient` envelope and project id becomes a
`record-user-directed-work` queue draft. Authenticate via the Outlook MCP mailbox.
Unmarked mail is ignored. The body is not specification, lock, or catalog authority.

## Rejected alternatives

### Embed an MCP client in `litai` in 0.8.0

Superseded by [ADR 0025](0025-cli-operator-mcp-client.md). The 0.8.0 CLI is the
operator-catalog client so headless mutagenic events are not missed.

### Fail closed when Slack or Jira is down

Rejected. Institutional notify must not undo a successful mutation.

### New Jira issue per mutagenic command

Rejected. That floods the site and breaks Epic/Story/Task hygiene.

## Consequences

- Tests prove: non-mutagenic commands write no journal event; mutagenic success writes
  an author envelope; journal IO errors do not change the command status.
- Skills wrap MCP posting from an interactive coding session; Python does not call
  Jira REST. There is no drain daemon.
