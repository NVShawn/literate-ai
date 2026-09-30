# ADR 0016: Literate AI Does Not Include a Source-Graph Indexer

- Status: Accepted
- Date: 2026-08-24
- Decision owners: literate-ai maintainers
- Roadmap: [CODEGRAPH-DEPENDENCY-001](../roadmap/active-work.md)
- Supersedes: [ADR 0015](0015-bounded-codegraph-evidence-and-reuse.md) in full.
- Partially superseded by:
  [ADR 0019](0019-opt-in-external-source-intelligence.md), only for an explicitly
  configured downstream project adapter and CLI.
  The directing maintainer later narrowed this ADR from "stop requiring the
  indexer" to "delete it from the product": no adapter, no optional CLI, no
  worker capability, and no documentation or tests that name it.

## Context

`main` already has a prepared 0.6.0 commit. Mid-release, the directing maintainer
asked to hold publication and remove a host source-graph indexer from the
framework's dependency list. After that work started, the same maintainer
directed a complete purge: the product must not invoke, ship, document, or test
that indexer in any form.

This is significant under
[ADR 0006](0006-significant-feature-request-governance.md). An earlier draft of
this ADR kept an optional adapter. That alternative is rejected.

## Decision

Literate AI does not include a source-graph indexer.

1. **Framework prerequisites are Python 3.11+ and, for generation only, one
   authenticated coding CLI.** No source-graph binary, package, sidecar
   directory, or capability belongs on any required or optional product list.
2. **Lifecycle never invokes a source-graph tool.** `litai init`, `validate`,
   `build`, `run`, `rebuild`, generated-source admission, snapshot-replication,
   worker bootstrap, and release complete without that class of tool. Generation
   indexing binds the exact source-tree identity only.
3. **Project policy has no indexer provider.** `source_intelligence` remains the
   explicit off/none declaration so existing manifests stay valid; no other
   `provider_id` is accepted.
4. **Do not keep an optional adapter, CLI, worker capability, Makefile target,
   npm pin, or agent-navigation instruction** for that indexer. Agents that want
   a third-party navigation aid install and use it themselves; Literate AI does
   not mention, bootstrap, or fail closed on it.
5. **Model-backed source-to-specification that required graph evidence is
   unavailable.** The static translator remains. Do not invent a replacement
   indexer in this change.
6. **Hold 0.6.0 until this lands, then re-prepare.** Publication of the already
   prepared `0.6.0` commit is not authorized.

## Rejected alternatives

### Keep the adapter as an optional CLI/agent path

Rejected by the directing maintainer: remove it entirely, including tests,
workflows, and documentation, as if it were never part of the product.

### Keep CodeGraph required, only delete it from user-facing lists

Rejected. Users would still fail `init`/`validate`/`rebuild` without the binary.

### Ship 0.6.0 first, remove the indexer later

Rejected by the directing maintainer: do not cut 0.6.0 yet.

## Consequences

- Users do not install, bootstrap, or debug a source-graph indexer to use
  Literate AI. Generated Component behavior is unchanged.
- Canonical validation, admission, worker bootstrap, and release receipts never
  claim graph-derived facts.
- 0.6.0 must be re-prepared after this change.

## Acceptance Criteria

- The directing maintainer explicitly accepts this complete-removal scope.
- Product source, tests, workflows, skills, templates, and user documentation
  do not name CodeGraph, `codegraph`, `.codegraph/`, or `codegraph-cli`.
- A host without that binary can `litai init`, `validate`, plan, and run the
  non-generation lifecycle gates.
- `@colbymchenry/codegraph` is absent from product and contributor manifests.
- 0.6.0 is not tagged until this implementation is on the revision that
  `litai release prepare` records.
