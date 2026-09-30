# ADR 0004: Keep Only Executable Port Boundaries

- Status: Accepted for bootstrap
- Date: 2026-08-03
- Decision owners: literate-ai maintainers
- Supersedes: the speculative Python protocol catalog introduced by ADR 0001

## Context

The bootstrap port package declared generic mapping protocols for specifications,
catalogs, source, intelligence, origin verification, resolution, models, validation,
classification, builds, sandboxes, artifacts, publication, events, references,
settings, and workspaces. Most had no application-layer consumer. Several contradicted
the typed APIs already implemented: for example, the generic publication protocol did
not match `PublicationService`, and the generic specification protocol did not match
`OpenSpecProvider`.

An unused interface is not an architecture boundary. It is an untested second design
that can drift independently of both the use case and its adapter.

## Decision

The shared `literate_ai.ports` package contains only protocols injected into an
implemented application service:

- model execution;
- generated-tree validation and classification;
- exact build authorization and building;
- transactional workspace acceptance; and
- lifecycle event persistence.

Readiness remains beside the generation application models because its signature uses
application-specific composition values. `BuildAuthorizer` has one definition in the
shared port package and is re-exported by `literate_ai.application` for import
compatibility. The unused `RouteSelector` protocol is removed; `ModelRouter.select`
already owns that pure decision.

Lifecycle ports continue to exchange canonical JSON because their outputs are durable
event payloads. Those payloads now have explicit typed shapes, and the orchestrator
checks the returned revision, source, classification, request, authorization, tree,
and workspace-reference identities before completing a step. Runtime-checkable
protocol tests prove that the shipped local adapters expose the injected methods.

OpenSpec, source capture, intelligence, storage, settings, and publication keep their
existing typed service APIs. Source-to-specification keeps narrowly typed callable
protocols at its application seam. A capability moves into the shared port package only
when all of the following exist:

1. an application service that receives the capability as a dependency;
2. a signature expressed with real domain or application values, or an explicit durable
   JSON contract;
3. at least one adapter that conforms to it; and
4. a seam test that rejects identity drift or malformed output where authority crosses.

## Compatibility

This is an intentional correction to an unused, less-than-one-day-old `0.1` surface.
The speculative protocol names are removed instead of deprecated because there are no
repository consumers and preserving them would continue to advertise false contracts.
The one used application import, `BuildAuthorizer`, remains compatible through a
re-export of the single canonical protocol.

## Consequences

The code now exposes fewer abstractions, but each shared port has a caller, an
implementation, and executable proof. Future adapters may require a small use-case-local
protocol before becoming shared. That cost is preferable to standardizing a signature
before its data and authority boundary are understood.

The architecture vocabulary can still discuss future provider capabilities. A name in
an architecture diagram is not a Python API or a delivery claim until the criteria
above are met.
