# ADR 0025: `litai` Speaks Operator-Catalog MCP Directly

- Status: Accepted
- Date: 2026-08-28
- Accepted: 2026-08-28
- Decision owners: literate-ai maintainers
- Roadmap: [MCP-CLI-001](../roadmap/active-work.md#x-mcp-cli-001-litai-posts-to-operator-catalog-mcps-discover-flag-off-by-default)
- Release: `0.8.0`
- GitHub: [#202](https://github.com/NVIDIA-dev/literate-ai/issues/202),
  [#203](https://github.com/NVIDIA-dev/literate-ai/issues/203)
- Supersedes: [ADR 0021](0021-operator-local-mcp-catalog.md) §4 (no Python MCP
  client) and [ADR 0023](0023-mutagenic-cli-channel-fan-out.md) §2 (agent-only
  post) plus the rejected alternative "Embed an MCP client in `litai` in 0.8.0"

## Context

Mutagenic `litai` work often runs **without** an interactive coding session.
Journaling under `~/.config/literate-ai/events/` is not enough: Jira, Slack, and
Outlook never see the envelope unless something speaks MCP. Directing
maintainer: the CLI must talk to operator-catalog servers itself, optionally
**discover** servers behind a flag that is off by default, and coding-session
skills must not re-post the same events when they already wrap `litai`.

The official Python module is [`mcp`](https://pypi.org/project/mcp/)
([modelcontextprotocol/python-sdk](https://github.com/modelcontextprotocol/python-sdk),
MIT). Generated application MCP servers use per-language official SDKs when
those Flavors are selected ([MCP-SDK-001](../roadmap/active-work.md#x-mcp-sdk-001-liberal-license-mcp-sdks-in-the-generation-skill-by-selected-flavor)).
Those SDKs are not product dependencies of the `litai` wheel.

## Decision

### 1. CLI is the operator-MCP client

On mutagenic success, Python journals the author envelope **and** attempts
fan-out through MCPs listed in `mcps.json` whose `uses` matches Jira, Slack, or
Outlook, when project `institutional_channels` names the destination. Outage or
missing tool is fail-open with a stderr skip. Tokens stay out of git.

The preferred library for this process is the official `mcp` package when it is
importable. Otherwise a bounded stdio JSON-RPC client uses optional `command`
argv from the catalog. Empty `command` cannot spawn; skip that server.

### 2. Discovery is opt-in

Global `--discover-mcps` defaults off. When on, `litai` may probe catalog
`command` hints and a listed `uses: registry` server. Discovery lists
reachability; it does not write secrets or replace `configure-operator-mcp`
catalog authoring. There is still no discovery daemon.

### 3. Skills wrap `litai`

Coding sessions that perform work through `litai` do **not** post duplicate
Jira/Slack/Outlook envelopes. `associate-release-jira` still **associates** a
major/minor ticket (interactive) and records `institutional_channels.jira_issue`
so the CLI can comment. Inbound `recipient` ingest remains an agent skill
(mail already in context; no poller).

### 4. Four MCP seams stay distinct

Operator catalog (`~/.config/literate-ai`), project `mcps/<id>/mcp.md`, embedded
application MCP (generation skill), and WebMCP (in-page tools) do not share
ids, secrets, or generation-prompt requirements.

## Rejected alternatives

### Keep agent-only posting

Rejected. Headless and scripted `litai` would miss institutional memory.

### Discovery on by default

Rejected. Probing MCP servers is unexpected network/process spawn on every
command, including CI.

## Consequences

- ADR 0021 path/schema/TTY offer and empty catalog remain.
- Tests inject a fake transport; they never require a live Jira/Slack/Outlook
  MCP.
- Generated Components learn language SDKs from the MCP skill gated on selected
  Flavors, not from this CLI dependency.
