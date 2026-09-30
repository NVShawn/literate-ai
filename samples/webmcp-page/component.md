---
namespace: samples
version: 1.0.0
display_name: WebMCP page
profiles:
  - application
  - frontend
  - mcp
  - sample
sample: true
inheritable: false
provides:
  - name: application.web-frontend
    version: 1.0.0
    interface: null
requires: []
authoring_inputs:
  - kind: specification-to-source-skill
    uri: skills/specification-to-source/mcp-application/SKILL.md
  - kind: specification-to-source-skill
    uri: skills/specification-to-source/frontend-application/SKILL.md
  - kind: specification-to-source-skill
    uri: skills/specification-to-source/mcp-application/webmcp/SKILL.md
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
    capability_contract: application.web-frontend
  - slot_id: ui
    axis: implementation.ui-framework
    cardinality: zero-or-one
    capability_contract: application.web-frontend
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
  - name: page
    kind: web-application
    path: page
acceptance_contracts: []
source_dependencies: []
---
# WebMCP page

In-page MCP tools on a JavaScript front-end. Pins JavaScript explicitly. WebMCP is
MCP in the page, not a backend integration.

```mermaid
flowchart LR
    H["Human UI"] --> T["WebMCP tools"]
    T --> H
```

## Application contract

| Concern | Decision |
| --- | --- |
| Kind | `web-application` |
| Protocol | WebMCP |
| Language | JavaScript |

### Requirement: Tools update visible page state

The page SHALL register at least one WebMCP tool whose execute path updates visible
state. Invalid input SHALL return a structured error.

#### Scenario: Missing required field is structured

- **WHEN** a browser agent invokes the tool without a required field
- **THEN** the tool returns a structured error and the page remains usable
