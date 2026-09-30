---
namespace: samples
version: 1.0.0
display_name: Playback Controller
profiles:
  - application
  - portable
  - sample
sample: true
inheritable: false
specification_provider: scxml
specification_roots:
  - playback.scxml
  - playback.trace.json
provides:
  - name: sample.portable-app
    version: 1.0.0
    interface:
      uri: interfaces/playback-controller.md
      pin: null
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
# Playback Controller

This Component is a small media-transport controller: play, mute, power, and resume
with deep history across two parallel regions. Device firmware, embedded players, and
session restorers can fork it when the product behavior is a state chart rather than a
decision table or a prose scenario list. The chart and replay traces own state-machine
behavior; `interfaces/playback-controller.md` owns the portable call/result boundary.

```mermaid
flowchart LR
    E["Event list"] --> C["SCXML chart"]
    C --> H["Deep history last"]
    C --> A["Active leaves"]
    H --> A
    A --> R["Playback configuration"]
```

The generated application exposes the `sample.portable-app` capability. Its exact
input, output, ordering, and state-replay obligations are owned by the public interface
rather than duplicated in this descriptive authoring body.
