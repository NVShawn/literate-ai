---
namespace: samples
version: 1.0.0
display_name: Multi-Role Text Job Pipeline
profiles:
  - aggregate
  - application
  - portable
  - sample
sample: true
inheritable: false
provides:
  - name: sample.portable-app
    version: 1.0.0
    interface: null
requires: []
authoring_inputs:
  - kind: specification-to-source-skill
    uri: skills/specification-to-source/portable-application-implementation/SKILL.md
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
  - name: run
    kind: portable-application
    path: run
acceptance_contracts: []
source_dependencies: []
---
# Multi-Role Text Job Pipeline

This pipeline keeps request validation and worker processing in separately generated
source roles, then composes them behind one runnable entrypoint. The shape is a useful
starting point for ingestion services, queue workers, document processors, and other
systems whose public API and asynchronous work evolve independently.

```mermaid
flowchart LR
    J["Text jobs"] --> A["API role: validate IDs and payloads"]
    A --> W["Worker role: count + hash content"]
    W --> I["Per-job content identity"]
    W --> S["Aggregate completion summary"]
```

## Application contract

| Concern | Decision |
| --- | --- |
| Application ID | `multi-repository-component` |
| Kind | `text-jobs` |
| Entrypoint | `run` |
| Generated members | `api`, `worker` |
| Digest | SHA-256 (`sha256`), first 12 lowercase hexadecimal characters |
| Text encoding | UTF-8 |

### Requirement: Aggregate identity

The aggregate SHALL retain exact member identities and remain independent of declaration
order.

#### Scenario: Two repository aggregate

- **WHEN** API and worker source snapshots are mounted in either input order
- **THEN** the aggregate identity and mounted-member set are identical

### Requirement: Executable host outcome

The generated job application SHALL compose API validation and worker processing from
separate source roots and return deterministic UTF-8 content identities. Every selected
job digest SHALL be the first 12 lowercase hexadecimal characters of SHA-256 with no
algorithm prefix. Every selected language Flavor SHALL generate non-empty `source/api/`
and `source/worker/` trees. The `run` entrypoint SHALL call validation implemented under
`source/api/` and processing implemented under `source/worker/`; it SHALL NOT inline
either member into the entrypoint.

#### Scenario: Compiled entrypoint runs

- **WHEN** the compiled `run` entrypoint receives two uniquely identified text jobs
- **THEN** it uses both generated member roots to validate the jobs, compute their word counts and SHA-256 prefixes, and report both as completed
