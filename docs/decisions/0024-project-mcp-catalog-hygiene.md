# ADR 0024: Project MCP Create, Modify, and Hygiene as a Catalog Kind

- Status: Accepted
- Date: 2026-08-28
- Accepted: 2026-08-28
- Decision owners: literate-ai maintainers
- Roadmap: [MCP-CATALOG-001](../roadmap/active-work.md#x-mcp-catalog-001-mcp-create-modify-and-hygiene-as-a-first-class-catalog-kind)
- Release: `0.8.0`
- GitHub: [#193](https://github.com/NVIDIA-dev/literate-ai/issues/193)

## Context

Operator opt-in ([ADR 0021](0021-operator-local-mcp-catalog.md)) lists servers a *user*
wants to call. A *project* may also create and evolve MCP servers it owns. That
lifecycle needs the same durability class as Components, Flavors, skills, workflows,
and routing: identity pins, no drifting second copy, no secrets in git, no hidden
generation-prompt requirements.

PLUGIN-001's later CLI-as-MCP adapter and an embedded MCP inside a generated
application (`python-service-application`) are different: one is a distribution
surface, the other is product behavior. Neither is project MCP hygiene.

## Decision

### 1. Catalog kind

Project-owned MCP servers live under a declared catalog root with directory-owned
sentinel `mcp.md` (same nesting rule as `SKILL.md` / `workflow.md`). Each item has a
stable id, semantic version, and content identity. `litai catalog copy` and repository
inheritance compose them like skills once the filesystem layout lands.

### 2. Hygiene rejects

Fail closed on: secrets or token-shaped bytes in the sentinel or sibling tracked
files; a second copy of the same id; an MCP instruction in a specification-to-source
skill (generation prompt); treating a maintainer's `~/.config/literate-ai` id as project
policy; unpinned bytes after modify.

### 3. 0.8.0 slice

This ADR is accepted so 0.8.0 can land the layout, schema, and reject rules. Full
`litai catalog` composition and an in-tree example server may follow in the same
minor if evidence checkboxes close; they must not block the operator catalog or
channel fan-out ([ADR 0021](0021-operator-local-mcp-catalog.md),
[ADR 0023](0023-mutagenic-cli-channel-fan-out.md)).

## Rejected alternatives

### Reuse `skills/agent/` for project MCP servers

Rejected. Agent skills wrap Python and must not become the MCP process.

### Operator catalog as the only MCP record

Rejected. That cannot version a server the project ships.

## Consequences

- A new catalog kind is a documentation and init-template change; do not flatten MCP
  files into the parent agent skill.
- Generation recipes stay MCP-free ([DOC-PUB-001](../roadmap/active-work.md)).
