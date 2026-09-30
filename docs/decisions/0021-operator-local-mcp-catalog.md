# ADR 0021: Operator-Local MCP Catalog Under `$HOME/.config/literate-ai`

- Status: Accepted
- Date: 2026-08-28
- Accepted: 2026-08-28
- Decision owners: literate-ai maintainers
- Roadmap: [USER-MCP-001](../roadmap/active-work.md#x-user-mcp-001-operator-local-mcp-catalog-offer-setup-jira-only-when-available)
- Release: `0.8.0`
- GitHub: [#192](https://github.com/NVIDIA-dev/literate-ai/issues/192),
  Jira consumer [#191](https://github.com/NVIDIA-dev/literate-ai/issues/191)

> **0.9.0 amendment:** [ADR 0031](0031-durable-user-configuration-home.md) renames the
> application directory to `literate-ai`, resolves it through the shared platform path
> adapter, and makes MCP setup depend on `mcps.json` itself rather than unrelated files
> in the shared directory. The original 0.8.0 location below is migration input.

## Context

Release association, Slack institutional notify, and Outlook mail all need to know
which MCP servers *this user* wants Literate AI to use. That list is operator-local:
it must not live in project authority, git, or `literate.project.json`. Agents already
attach MCPs in the host; `litai` had no durable catalog and no first-run setup.

An organization MCP service may publish a static URL list and an `mcp_registry`
discovery service. Those
are optional setup sources, not framework dependencies.

## Decision

### 1. Catalog location

The catalog directory is `$HOME/.config/literate-ai`. Honor `XDG_CONFIG_HOME` (then
`XDG_CONFIG_HOME/literate-ai`) and `LITAI_CONFIG_DIR` (must be absolute). The file is
`mcps.json` with schema `urn:literate-ai:schema:v1:user-mcp-catalog`. Tokens, webhooks,
and bot secrets never appear in the catalog or in git.

### 2. First-launch offer

An interactive coding session follows `skills/agent/configure-operator-mcp` when
the catalog is missing or empty, or when the user asks to set it up. That skill
discovers MCP servers already connected in the session, optionally consults a
reachable registry, and writes `mcps.json`. Absence of an organization MCP service or any
registry is not a failure.

If `litai` parses a command with no agent session, and the directory is missing
or contains no regular files, Python offers a thin TTY ids-only setup. Offer
only on an interactive TTY and never in `--json` mode. Non-TTY / CI continue
with an empty in-memory catalog and write nothing. Declining writes an empty
catalog so the offer does not repeat. There is no discovery daemon.

### 3. Availability gating

A named integration (Jira, Slack, Outlook) runs only when that MCP `id` is listed
**and** the session or configured command can reach it. Missing Jira is skip, not a
third `litai` tracker client. GitHub/`gh` and GitLab/`glab` remain the repository
tracker ([TRACKER-HOST-001](../roadmap/active-work.md#x-tracker-host-001-detect-github-vs-gitlab-from-git-remotes-and-use-or)).

### 4. Python vs agent

Superseded for posting and discovery by
[ADR 0025](0025-cli-operator-mcp-client.md): `litai` is the operator-catalog
MCP client. This ADR still owns path, schema, the TTY ids-only offer, and the
empty-catalog write. Session skill `configure-operator-mcp` still authors
`mcps.json` when a human is choosing ids. Coding-session skills wrap `litai`
and do not re-post mutagenic envelopes.

## Rejected alternatives

### Store MCP URLs in `literate.project.json`

Rejected. That would make one maintainer's servers product policy and risk secrets in
git.

### Fail closed when the catalog is empty

Rejected. CI and headless `litai` must keep working. Empty catalog is valid.

## Consequences

- An interactive coding session can create `~/.config/literate-ai/mcps.json` by
  following `configure-operator-mcp`; a TTY `litai` without an agent can still
  offer ids-only setup.
- Tests isolate `LITAI_CONFIG_DIR` and non-TTY stdin so they never prompt or write
  `$HOME`.
- Jira/Slack/Outlook work is conditional on this catalog
  ([ADR 0023](0023-mutagenic-cli-channel-fan-out.md)).
