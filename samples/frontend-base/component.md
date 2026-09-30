---
namespace: samples
version: 1.0.0
display_name: Front-end base
profiles:
  - application
  - frontend
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
    uri: skills/specification-to-source/frontend-application/SKILL.md
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
  - slot_id: packages
    axis: packaging
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
# Front-end base

JavaScript page UI peer. Pass `+lang-javascript` explicitly; add `+ui-react` (or the
`lang-javascript-react` alias) when the run should contribute the React delta. Add
`+package-npm` when the page needs a lockfile-pinned npm closure. Do not pin
`react-application` on this Component: the `ui-react` Flavor already contributes
that skill. The language slot requires `application.web-frontend`, so an unpinned
Python default cannot fill it.

```mermaid
flowchart LR
    P["Page specification"] --> U["Front-end parent"]
    U -.-> R["React delta via ui-react"]
```

## Application contract

| Concern | Decision |
| --- | --- |
| Kind | `web-application` |
| Language | JavaScript |
| UI | Optional React via `ui-react` |
| Packages | Optional lockfile-pinned npm via `package-npm` |

### Requirement: JavaScript Flavor is required

The Component SHALL select `lang-javascript`. A Python-only selection SHALL fail closed.

#### Scenario: Conflicting language is rejected

- **WHEN** only a non-JavaScript language Flavor is selected
- **THEN** the Component cannot lock
