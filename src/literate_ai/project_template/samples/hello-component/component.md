---
namespace: example
version: 1.0.0
display_name: Portable greeting application
profiles: ["application", "portable"]
sample: true
inheritable: true
provides:
  - name: application.portable-json
    version: 1.0.0
requires: []
authoring_inputs:
  - kind: specification-to-source-skill
    uri: skills/specification-to-source/portable-application-implementation/SKILL.md
  - kind: specification-to-source-skill
    uri: skills/specification-to-source/portable-specification-planning/SKILL.md
workflow_definition: workflows/production/staging/dev/workflow.md
routing_policy: routing/production/staging/dev/routing.json
flavor_slots:
  - slot_id: build-system
    axis: build.system
    cardinality: zero-or-one
    capability_contract: application.portable-json
  - slot_id: language
    axis: implementation.language-ecosystem
    cardinality: exactly-one
    capability_contract: application.portable-json
  - slot_id: os
    axis: platform.os
    cardinality: exactly-one
    capability_contract: application.portable-json
  - slot_id: package
    axis: packaging
    cardinality: bounded
    capability_contract: application.portable-json
    minimum: 0
    maximum: 6
  - slot_id: toolchain
    axis: toolchain
    cardinality: zero-or-one
    capability_contract: application.portable-json
entrypoints:
  - name: run
    kind: portable-application
    path: run
acceptance_contracts: []
source_dependencies: []
---
# Portable greeting application

Build a command-line application that accepts one JSON object as its first argument and
emits one JSON object on standard output.

The input has a required non-empty string `name`. The output has exactly `greeting` and
`name`; `name` preserves the input and `greeting` is `Hello, NAME!`. Invalid input exits
nonzero and writes a concise diagnostic to standard error.
