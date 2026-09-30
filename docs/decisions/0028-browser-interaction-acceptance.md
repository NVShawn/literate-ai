# ADR 0028: Verifier-Owned Browser Interaction Acceptance for Generated Frontends

- Status: Accepted
- Date: 2026-09-01 (proposed); 2026-09-01 (accepted after planning-cycle review)
- Decision owners: literate-ai maintainers
- Roadmap: [BROWSER-ACCEPT-001](../roadmap/active-work.md) (#218)
- Depends on: [ADR 0026](0026-multi-entrypoint-deployment-units.md) (deployment
  units to health-probe), the shipped `--litai-serve` launch mode (#214); extends
  #64 and #134; follows the operator-side FRONTEND-002 `verify-frontend-browser`
  skill
- Related: validates the frontend boundary of
  [ADR 0027](0027-durable-split-service-pattern.md) (#216)

## Context

A generated frontend repeatedly passed generated tests and text-only
persistent-service acceptance while shipping observable failures: omitted required
sections, `NaN` rendered, inline scripts referencing undefined constants or with
syntax errors, a serialized browser helper referencing an undeclared constant, a
virtualized table that computed the right window in unit tests but bound no working
scroll handler, export/column controls that were labels or no-op handlers, and
persistent mobile document overflow.

Independent acceptance today checks HTTP **text**, so none of these fail closed —
a silent false-pass in the acceptance oracle, not merely a generation-quality gap.
The FRONTEND-002 `verify-frontend-browser` operator skill helps manual review but
does not gate the lifecycle. This ADR decides the verifier-owned, lifecycle-gating
browser acceptance that closes the enforcement gap.

## Decision

### A verifier-owned browser acceptance contract, separate from generated tests

Add a browser acceptance contract that is **owned by the verifier**, distinct from
the Component's own generated tests (which a generator could satisfy while shipping
a broken page). It is a new independent-acceptance mode for a served frontend
deployment unit (ADR-0026), launched via the shipped `--litai-serve` mode. The
contract can declare:

- service startup plus desktop and mobile viewports;
- accessible role/name assertions and required landmarks;
- rejection on any browser console error, page error, failed request, or
  HTTP-error response;
- document-overflow checks (e.g. no horizontal overflow at a 390px viewport);
- interaction steps: click, select, fill, scroll, keyboard;
- observable postconditions: text, state, ARIA, row identity, count, download;
- request-count assertions for local-only interactions (a local sort/toggle that
  fetches unexpectedly fails);
- screenshot and accessibility-snapshot evidence as diagnostics.

### Browser-tool-neutral contract; Playwright is the first adapter

The contract is browser-tool-neutral. Playwright is the initial adapter, but the
declared contract (viewports, interactions, postconditions, rejection classes) is
not Playwright-shaped, so a second adapter can be added without rewriting
acceptance specs. The adapter is an infrastructure port behind the contract, in
keeping with ADR-0004 executable-port boundaries.

**Rejected alternative:** asserting on rendered HTML text or a DOM snapshot
without a real browser engine. That is what text-only acceptance already does and
is exactly what let `NaN`, unbound scroll handlers, and no-op buttons pass. A real
browser engine that executes scripts, fires events, and surfaces console/page
errors is required to make these fail closed.

### Deterministic fixtures; no secrets in generation prompts

Acceptance uses a deterministic fixture/stub transport. It never uses live
credentials, and never places private oracle values (expected counts, fixture
data, the acceptance spec itself) into any generation prompt — the generator must
not be able to hardcode to the oracle. This preserves the ADR-0005 authority
boundary: the browser acceptance oracle is verifier-only material.

### Evidence receipt binds the full interaction context

The acceptance evidence receipt binds viewport, the interaction contract identity,
the browser/tool identity, screenshots, captured console failures, and the
outcome. A pass is only evidence for the exact viewport + interaction contract +
browser identity recorded, so a reviewer can see precisely what was driven.

### Text probes remain for non-browser services

Existing persistent-service text/HTTP probes remain supported and are the default
for non-browser services; browser acceptance is additive and applies to frontend
deployment units that declare a browser contract.

## Consequences

The independent-acceptance path gains a browser mode and a Playwright adapter, a
new browser-acceptance contract schema, and an evidence-receipt extension. The
acceptance test suite gains the issue's fail-closed fixtures: a text-only fixture
that throws on init fails; a virtual-grid fixture with an unbound scroll handler
fails after a scroll/postcondition step; a no-op Export button fails a download
assertion; a page wider than a 390px viewport fails overflow; a local sort/toggle
that fetches unexpectedly fails a request-count assertion.

The cost is a browser engine (Playwright) as an acceptance-time dependency and the
CI capacity to run it; this is gated to frontend deployment units that declare a
browser contract, so non-frontend projects pay nothing. The benefit is that the
class of observable frontend failures that currently ship silently now fails the
lifecycle — closing the verifier-enforcement gap #218 identifies and giving the
ADR-0027 frontend boundary a real acceptance oracle.
