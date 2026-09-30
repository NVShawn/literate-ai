---
namespace: samples
version: 1.0.0
display_name: Back-end base
profiles:
  - application
  - service
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
    uri: skills/specification-to-source/mcp-application/SKILL.md
  - kind: specification-to-source-skill
    uri: skills/specification-to-source/backend-application/SKILL.md
  - kind: specification-to-source-skill
    uri: skills/specification-to-source/backend-application/python-service-application/SKILL.md
  - kind: specification-to-source-skill
    uri: skills/specification-to-source/backend-application/rust-service-application/SKILL.md
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
  - name: service
    kind: persistent-service
    path: service
acceptance_contracts: []
source_dependencies: []
---
# Back-end base

Unpinned peer of hello-component for the back-end parent. Empty language lists still
default to Python plus Make plus the host OS. Both language-chapter skills are pinned
here so `+lang-rust` keeps the Rust delta and omits the Python one.

```mermaid
flowchart LR
    S["HTTP API"] --> D[("Local store")]
    M["Embedded MCP"] --> D
    W["Scheduled worker"] --> D
```

## Application contract

| Concern | Decision |
| --- | --- |
| Kind | `persistent-service` |
| Store | Local; request paths do not call the expensive upstream |

### Requirement: Local store answers requests

The HTTP API and embedded MCP server SHALL answer from the local store. Only a
scheduled worker MAY call an expensive upstream source.

#### Scenario: Missing resource is a structured error

- **WHEN** a caller requests a resource absent from the local store
- **THEN** the API returns a typed error and does not contact the upstream source
