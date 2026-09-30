# ADR 0026: First-Class Multi-Entrypoint Components as Independent Deployment Units

- Status: Accepted
- Date: 2026-09-01 (proposed); 2026-09-01 (accepted after planning-cycle review)
- Decision owners: literate-ai maintainers
- Roadmap: [MULTI-ENTRYPOINT-001](../roadmap/active-work.md) (#33); enabling
  mechanism for [SPLIT-SERVICE-001](../roadmap/active-work.md) (#216)

## Context

The Standard lifecycle requires exactly one entrypoint per executable Component.
`_command_contract` in `src/literate_ai/adapters/standard_project.py` fails closed
with `standard_command.entrypoint_cardinality` for any Component declaring more
than one entrypoint, and the whole command contract downstream of that check
assumes `entrypoints[0]`: one artifact export shape, one TEST command, one
EXECUTE command, one runtime tool binding.

The documented workaround (ENTRYPOINT-001) is to express a multi-surface
application as several single-entrypoint Components joined by the ADR-0005
`provides`/`requires` capability edges. That workaround is correct and remains
supported. But two independent derived services (`portfolio-dashboard`,
`telemetry-dashboard`) show the gap it leaves: a real application is a small set of
**cooperating surfaces that must be built, tested, accepted, deployed, restarted,
and evidenced independently** — a browser frontend, a read-only HTTP API, a
scheduled single-writer collector, and a durable cache. Modeling these purely as
separate Components works for the dependency graph but leaves two things
uncatalogued: (a) whether one Component may legitimately own several cooperating
entrypoints, and (b) how independent *deployment units* — not just build units —
are declared, so that restarting the frontend does not restart the collector.

This ADR decides the lifecycle model. It does **not** decide the reusable
split-service pattern (ADR 0027) or browser acceptance (ADR 0028); those build on
this mechanism.

## Decision

### A Component may declare more than one entrypoint, with one deployment unit per entrypoint

We keep ADR-0005's rule that **one immutable Component revision is the unit of
generation, source caching, build, test, and publication**. We do not merge
private authority. What this ADR adds is a distinction ADR-0005 left implicit:

- an **entrypoint** is one invocable surface of a Component (a CLI, a served
  HTTP/MCP process, a scheduled worker loop);
- a **deployment unit** is the independent operational axis — the thing that is
  started, stopped, restarted, and health-evidenced on its own.

The single-entrypoint wall is replaced by an explicit cardinality contract: a
Component declares one or more entrypoints, and every entrypoint resolves to one
unique deployment unit. The common case (one entrypoint, one implicit deployment
unit named after that entrypoint) is unchanged and stays the default. An undeclared
unit on a multi-entrypoint Component likewise defaults to that entrypoint's name;
an explicit `deployment_unit` gives the operational unit a stable domain name.
Duplicate resolved deployment-unit names fail closed because receipt, package,
restart, and acceptance selectors must identify exactly one invocable surface.

**Rejected alternative:** allowing arbitrary many entrypoints in one Component
with no deployment-unit concept. That reintroduces exactly the coupling #216
reports — restarting one surface restarts all — and gives the acceptance oracle
no independent unit to health-probe. The deployment unit, not the entrypoint
count, is the axis that matters.

**Preferred composition remains multiple Components.** For surfaces that must not
see each other's private authority (the #216 case: the frontend must not hold
collector credentials), separate Components joined by public capabilities stay
the recommended model. First-class multi-entrypoint is for cooperating surfaces
that legitimately share one authority boundary and one source tree (e.g. an API
and its admin CLI over the same domain types). ADR 0027 chooses per boundary
which of the four surfaces are separate Components vs. entrypoints.

### Per-entrypoint lifecycle stages

Each entrypoint carries its own TEST, EXECUTE (and, per ADR-0028, ACCEPTANCE)
command projection and its own artifact export shape, keyed by entrypoint
identity rather than a single `entrypoints[0]`. The command contract becomes a
map from entrypoint identity to its per-phase commands and export shape. BUILD
remains per-Component-revision (one source tree, one build), producing the set of
entrypoint artifacts; TEST/EXECUTE/ACCEPTANCE fan out per entrypoint.

`--litai-serve` (ADR/issue #214) already lets one artifact be launched as a
listening server; multi-entrypoint generalizes the dispatcher so the driver
selects which entrypoint of a multi-entrypoint artifact to run.

The durable dispatcher exposes that choice as `litai run COMPONENT --entrypoint
NAME`. Omitting the option selects the first declared entrypoint for backward
compatibility. Local exports retain the shared artifact root plus a closed,
identity-bound command set; SSH worker-CAS locators bind both the artifact bytes
and execution manifest so equal payload bytes with different commands cannot
reuse stale execution state.

### Receipt and lock bind every entrypoint and deployment unit

The Component lock and the evidence receipt bind each entrypoint's export shape,
per-phase command identities, and its deployment-unit membership. A change to one
entrypoint's authority invalidates that entrypoint's generation/build/test per
the ADR-0005 invalidation table; the deployment-unit binding is part of the
locked identity so a reviewer sees the operational topology, not just the build
graph. CycloneDX evidence enumerates deployment units so the four-boundary
composition in #216 is auditable.

### Fail-closed cardinality and kind rules

`resolve_component_kind` / `validate_component_kind` extend to multi-entrypoint:
a Component's kind must be consistent with the *set* of its entrypoint kinds, and
contradictions (e.g. a library Component with any entrypoint) still fail closed.
A multi-entrypoint Component with no deployment-unit declarations defaults every
entrypoint to its own implicit unit named after the entrypoint. Existing
single-entrypoint Components remain unchanged, while multi-entrypoint Components
receive independently selectable operational units without additional syntax.

## Consequences

`_command_contract` stops rejecting multiple entrypoints and instead projects a
per-entrypoint command/export map; this is the largest single implementation
change and touches the Standard command projection, the lock/receipt schemas, and
the acceptance dispatch. New version-2 wire contracts are additive
(`multi-entrypoint-component-semantics@1`, plus the optional Component entrypoint
`deployment_unit` field); single-entrypoint Components that omit the field project
byte-identically to today.

The cost is a more elaborate command contract and lock. The benefit is that #216
can declare independent deployment units directly, #218 can health-probe and
browser-accept one deployment unit without starting the others, and #217's
collector can be restarted without touching the API/frontend. This ADR is the
mechanism; ADRs 0027 and 0028 are its first two consumers and must be accepted
together as the 0.9.0 durable-service cluster.
