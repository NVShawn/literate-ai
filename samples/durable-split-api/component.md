---
namespace: samples
version: 1.0.0
display_name: Durable Snapshot Read API
profiles:
  - application
  - service
  - split-service
sample: true
inheritable: false
provides:
  - name: sample.portable-app
    version: 1.0.0
    interface: null
  - name: literate-ai.durable-snapshot-api
    version: 1.0.0
    interface:
      uri: interfaces/snapshot-api.md
      pin: null
requires:
  - requirement_id: snapshot-read-interface
    capability: literate-ai.durable-snapshot-read
    version_range: ">=1,<2"
    dependency_kind: generation
    optional: false
    constraints: []
authoring_inputs:
  - kind: specification-to-source-skill
    uri: skills/specification-to-source/portable-application-implementation/SKILL.md
  - kind: specification-to-source-skill
    uri: skills/specification-to-source/backend-application/SKILL.md
  - kind: specification-to-source-skill
    uri: skills/specification-to-source/backend-application/durable-split-service/SKILL.md
  - kind: specification-to-source-skill
    uri: skills/specification-to-source/portable-specification-planning/SKILL.md
workflow_definition:
  uri: workflows/sample-host.md
routing_policy:
  uri: routing/sample-host.json
flavor_slots:
  - slot_id: language
    axis: implementation.language-ecosystem
    cardinality: exactly-one
    capability_contract: sample.portable-app
  - slot_id: os
    axis: platform.os
    cardinality: exactly-one
    capability_contract: sample.portable-app
  - slot_id: build-system
    axis: build.system
    cardinality: zero-or-one
    capability_contract: sample.portable-app
  - slot_id: toolchain
    axis: toolchain
    cardinality: zero-or-one
    capability_contract: sample.portable-app
entrypoints:
  - name: api
    kind: persistent-service
    path: api
acceptance_contracts: []
source_dependencies: []
---
# Durable Snapshot Read API

This Component is the read-only HTTP boundary of the durable split-service portfolio.
It receives only the cache's public read interface. It has no upstream client or
credential and owns no write path.

```mermaid
flowchart LR
    F["Frontend"] -->|GET /snapshot| A["Read-only API"]
    A -->|SQLite mode=ro| C["Current snapshot"]
```

## Application contract

| Concern | Decision |
| --- | --- |
| Application ID | `durable-split-api` |
| Role | Read-only HTTP API |
| Entrypoint | `api` |
| Upstream access | Forbidden |
| Cache writes | Forbidden |

### Requirement: Serve only the current complete snapshot

The `api` entrypoint SHALL support Standard `--litai-test`, `--litai-smoke`, and
`--litai-serve PORT DATABASE` modes. Serve mode SHALL bind loopback, answer
`GET /health`, and answer `GET /snapshot` with one canonical JSON object containing
the current published snapshot and shared freshness/progress state. The database
connection SHALL use SQLite URI `mode=ro` and `PRAGMA query_only=ON`. Absence of a
published snapshot SHALL return an explicit empty/stale result and SHALL NOT trigger
collection.

#### Scenario: API reads a complete snapshot

- **WHEN** the current pointer names a published snapshot
- **THEN** `GET /snapshot` returns exactly that snapshot's ordered metrics and metadata

### Requirement: API cannot collect or write

Generated API source SHALL contain no upstream request client, upstream URL input,
credential input, migration DDL, or cache mutation statement. Its process receives only
the database path. Restarting the process SHALL reopen the same file read-only and serve
the prior snapshot without coordinating with a collector.

#### Scenario: API restart while upstream is unavailable

- **WHEN** a new API process opens the durable database after collection has stopped
- **THEN** it serves the same current snapshot and makes no upstream request

### Requirement: Deterministic boundary description

Outside serve mode, the entrypoint SHALL accept either `[{"action":"describe"}]` or
`[{"action":"capabilities"}]` and return exactly
`{"cache_mode":"read-only","request":ACTION,"role":"api",
"upstream_access":false}`, where `ACTION` is the supplied action. Standard smoke mode
SHALL use request `describe` and return the same shape without opening a database.

#### Scenario: API identifies its least-privilege role

- **WHEN** the entrypoint receives either self-description request
- **THEN** it reports read-only cache access and no upstream access
