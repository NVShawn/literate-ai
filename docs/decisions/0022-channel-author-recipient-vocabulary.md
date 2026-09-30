# ADR 0022: Shared Author or Recipient Vocabulary for Jira, Slack, and Outlook

- Status: Accepted
- Date: 2026-08-28
- Accepted: 2026-08-28
- Decision owners: literate-ai maintainers
- Roadmap: [CHANNEL-VOCAB-001](../roadmap/active-work.md#x-channel-vocab-001-shared-literate-ai-author-or-recipient-vocabulary-for-jira-slack-and-outlook)
- Release: `0.8.0`
- GitHub: [#195](https://github.com/NVIDIA-dev/literate-ai/issues/195)

## Context

The same mutagenic `litai` events will appear as Jira comments, Slack posts, and
Outlook mail. Inbound Outlook (later Slack/Jira) will ask Literate AI to do work on a
named project. Without one envelope, each channel would grow an ad-hoc prefix and
agents could not tell whether Literate AI wrote the message or should act on it.

## Decision

### 1. One envelope

Schema `urn:literate-ai:schema:v1:channel-event`:

- `marker` is always `literate-ai`
- `role` is `author` (we wrote this) or `recipient` (act on this)
- `kind` is `mutagenic-cli`, `inbound-task`, or `publish-summary`
- `project_id` names the Literate AI project
- optional `command`, `revision`, `outcome`, `user`

Human first line plus a parseable `literate-ai-event:1` trailer. No per-channel
prefix. No secrets, tokens, private prompts, or worker hostnames.

### 2. Authority boundary

`author` events are status. `recipient` text is **not** project authority until
`record-user-directed-work` records a queue item. Unmarked or spoofed messages are
ignored. Inbound never mutates locks, catalogs, or specifications by itself.

### 3. Python owns parse and format

Formatters for Jira comment, Slack post, and Outlook subject/body share this contract.
The parser is the admission gate for inbound Outlook ([ADR 0023](0023-mutagenic-cli-channel-fan-out.md)).

## Rejected alternatives

### Channel-specific subject lines only

Rejected. Slack and Jira comments would drift from Outlook subjects.

### Treat inbound mail as specification authority

Rejected. Email is an untrusted inbox. Queue it, then execute from the queue.

## Consequences

- Jira, Slack, and Outlook skills must emit this envelope; tests round-trip all three
  shapes.
- [ADR 0006](0006-significant-feature-request-governance.md) still applies before a
  later schema version adds fields.
