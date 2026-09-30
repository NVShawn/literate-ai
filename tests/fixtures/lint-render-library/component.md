---
namespace: fixtures
name: greeting-surface
version: 1.0.0
display_name: Greeting Surface
kind: library
profiles:
  - library
  - sample
sample: true
inheritable: false
provides: []
requires: []
authoring_inputs: []
workflow_definition: workflows/host.md
routing_policy: routing/default.json
flavor_slots: []
entrypoints: []
acceptance_contracts: []
source_dependencies: []
---
# Greeting Surface

A hand-authored library Component. Independent acceptance is a lint command plus an
exact HTML render snapshot, not a CLI JSON probe and not visual inspection of an
editor.
