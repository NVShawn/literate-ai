# ADR 0029: A Unified IPC-Surface Contract with MCP, REST, and gRPC as Adapters

- Status: Accepted
- Date: 2026-09-01 (proposed); 2026-09-01 (accepted after planning-cycle review)
- Decision owners: literate-ai maintainers
- Release target: 0.9.0
- Roadmap: [IPC-SURFACE-001](../roadmap/active-work.md) (adapter subtasks recorded
  on acceptance)
- Depends on: [ADR 0005](0005-executable-component-semantics.md) (public
  interfaces, compatibility promise, edge semantics),
  [ADR 0026](0026-multi-entrypoint-deployment-units.md) (deployment units that
  expose a surface), [ADR 0027](0027-durable-split-service-pattern.md) (the
  frontend→API boundary that needs a described contract),
  [ADR 0028](0028-browser-interaction-acceptance.md) (verifier-owned acceptance
  pattern this reuses)

## Context

The toolbox teaches one inter-process protocol well — MCP — via
`skills/specification-to-source/mcp-application`. That skill already encodes the
right invariants for an exposed surface: expose named operations with
**schema-validated input**, return **structured errors** (never a bare string),
give every operation a concise description, **pin identity with a stable id + a
semantic version + exact bytes**, and reject secrets in descriptions/metadata.

But MCP's two most common peers in modern service programming — a **REST/HTTP+JSON
API** and a **gRPC/protobuf service** — have no equivalent. The backend and
split-service skills describe "an HTTP API" only in prose. This leaves three gaps
the recently-shipped work makes concrete:

1. **No described API contract.** ADR 0027's frontend→API→cache composition wires
   boundaries by capability, but the HTTP surface itself is not a schema-described,
   versioned, contract-tested interface. A frontend and API agree by convention,
   not by a served contract.
2. **No self-describing / discoverable endpoints.** When a Component exposes IPC
   there is no guidance to serve its own description (OpenAPI/JSON-Schema at a
   discoverable path, gRPC server reflection, MCP's own tool/resource listing) so a
   consumer — or a verifier — can fetch and check it.
3. **No conformance acceptance for the surface.** We now have browser acceptance
   (ADR 0028) and HTTP-text probes, but nothing that verifies *the served surface
   matches its declared schema and that responses validate against it*, nor an
   API-version compatibility promise analogous to ADR-0005's public-interface
   compatibility.

Rather than author three unrelated skills (REST, gRPC, GraphQL) that each
re-invent schema/description/error/versioning rules and drift apart, this ADR
decides one first-class notion and makes each protocol an adapter of it.

## Decision

### One notion: an "IPC surface"

Introduce a protocol-neutral **IPC surface**: a Component boundary that exposes
named operations to out-of-process callers under four properties, mirroring what
the MCP skill already requires and what ADR-0005 already grants Component public
interfaces:

1. **A declared schema.** Every operation has schema-validated input and typed
   output. Invalid input is rejected with a structured error before execution — no
   partially-valid calls. This is the MCP tool rule, generalized.
2. **A served self-description.** The surface serves its own machine-readable
   description at a discoverable location so a consumer or verifier can fetch it:
   OpenAPI/JSON-Schema for REST, server reflection + the `.proto` descriptor for
   gRPC, the tool/resource listing for MCP. The description is generated from the
   same declared schema, never hand-drifted from it.
3. **A conformance acceptance oracle.** A verifier-owned acceptance (in the shape
   of ADR 0028's browser acceptance and the persistent-service oracle) launches the
   surface via the shipped `--litai-serve` deployment-unit path, fetches the served
   description, asserts it matches the declared schema, drives declared request
   cases, and validates each response against the schema. It fails closed on a
   surface that serves a description not matching its declared schema, or a
   response that does not validate. Deterministic fixtures only; no live
   credentials; the oracle's expected values are verifier-only material and never
   enter a generation prompt (ADR-0005 authority boundary).
4. **A version + compatibility promise.** The surface carries a semantic version
   and a compatibility promise, reusing ADR-0005's `CompatibilityPromise` machinery
   rather than inventing a second one. A breaking change to the surface is a
   version transition a consumer's binding can be checked against — the same way a
   Component public-interface change is today.

### MCP, REST, and gRPC are adapters, not separate features

`mcp-application` becomes the first *adapter* of the IPC-surface notion (its
tools/resources/errors/identity rules are the surface properties specialized to
MCP). Two new adapters follow the same parent:

- **`rest-application`** — REST/HTTP+JSON with an OpenAPI 3.x + JSON-Schema
  contract served at a discoverable path (e.g. `/openapi.json`), responses that
  validate against it, and versioned routes.
- **`grpc-application`** — gRPC with a `.proto` service contract, server reflection
  enabled, and protobuf-typed messages.

GraphQL is a plausible fourth adapter but is deliberately **out of scope for the
first pass**: its schema/introspection/versioning story is different enough to
warrant its own follow-up, and REST + gRPC + MCP cover the dominant cases.

### Schema-first is enabled but not mandated

A protobuf / JSON-Schema / OpenAPI schema source can be a single source of truth a
Component generates typed clients and servers from. This ADR makes that *possible*
(the declared schema is the surface's authority) and anticipates a schema-source
Flavor as a follow-on, but does not require projects to adopt schema-first: an
adapter may declare its schema inline. The `schema-only` Component kind already
exists and is the natural home for a shared schema Component that several surfaces
consume as a capability (ADR-0005 public interface).

### IPC documentation is part of the surface, not a separate deliverable

The served self-description (property 2) *is* the primary, always-current API
documentation, because it is generated from the declared schema and
conformance-checked (property 3) — so it cannot silently drift from behavior. Human
narrative docs remain the deriving project's authority (ADR-0001); the framework
owns the discoverable machine description and its conformance, not product prose.

## Consequences

New work, to be split into an accepted roadmap item per adapter after the planning
cycle: an IPC-surface contract type + served-description + conformance acceptance
oracle (built on ADR 0028's acceptance shape and the `--litai-serve` launch), the
`rest-application` and `grpc-application` skills under a shared parent, and the
re-framing of `mcp-application` as the first adapter (a documentation/linking
change, not a behavioral one, so its pinned children do not cascade). Each adapter
ships with deterministic conformance fixtures that fail closed on a served
description not matching the declared schema.

The cost is a genuinely new cross-cutting subsystem and a gRPC/protobuf and
OpenAPI toolchain as acceptance-time dependencies (gated to Components that declare
those surfaces, like Playwright is gated to browser frontends). The benefit is that
the framework gains a coherent, verifiable answer to the modern-era question it
currently only answers for MCP: *how does a generated Component expose a
documented, discoverable, version-safe, conformance-tested interface to other
processes* — with MCP, REST, and gRPC as three adapters of one contract instead of
three drifting skills, and the ADR-0027 frontend→API boundary finally described by
a served, checked schema.
