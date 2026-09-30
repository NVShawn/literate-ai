---
namespace: samples
version: 1.0.0
display_name: macOS LaunchAgent Catalog
profiles:
  - application
  - macos
  - sample
  - swift
sample: true
inheritable: false
provides:
  - name: sample.portable-app
    version: 1.0.0
requires: []
authoring_inputs:
  - kind: specification-to-source-skill
    uri: skills/specification-to-source/portable-application-implementation/SKILL.md
  - kind: specification-to-source-skill
    uri: skills/specification-to-source/portable-specification-planning/SKILL.md
workflow_definition: workflows/sample-host.md
routing_policy: routing/sample-host.json
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
  - slot_id: package
    axis: packaging
    cardinality: zero-or-one
    capability_contract: sample.portable-app
entrypoints:
  - name: run
    kind: portable-application
    path: run
acceptance_contracts: []
source_dependencies: []
---
# macOS LaunchAgent Catalog

This macOS-only Swift Component turns a LaunchAgent declaration into a compact,
reviewable catalog record. It is a useful starting point for desktop installers,
developer-environment managers, and managed-service launch tooling, while the Homebrew
Flavor proves that packaging policy composes with language and operating-system pins.

```mermaid
flowchart LR
    D["LaunchAgent declaration"] --> V["Validate label and arguments"]
    V --> F["Derive plist filename"]
    V --> C["Catalog record"]
    F --> C
```

## Application contract

The `run` entrypoint accepts one object with exactly `bundle_id`, `label`, `arguments`,
and `run_at_load`. The first two fields are nonempty strings, `arguments` is a nonempty
array of strings whose first element is the program, and `run_at_load` is boolean.

Return exactly `bundle_id`, `label`, `plist_file`, `program`, `argument_count`, and
`run_at_load`. `plist_file` is the label followed by `.plist`; `program` is the first
argument; and `argument_count` counts the complete arguments array.

### Requirement: Catalog a macOS LaunchAgent

The application SHALL compile with the selected Apple Swift toolchain and derive one
deterministic LaunchAgent catalog record.

#### Scenario: The compiled macOS Swift entrypoint runs

- **WHEN** a valid LaunchAgent declaration is supplied
- **THEN** the application returns its exact plist filename, program, argument count, and run-at-load policy
