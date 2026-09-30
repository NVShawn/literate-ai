---
namespace: samples
version: 1.0.0
display_name: Loan Risk Gate
profiles:
  - application
  - portable
  - sample
sample: true
inheritable: false
specification_provider: dmn
specification_roots:
  - decisions/risk-category.dmn
provides:
  - name: sample.portable-app
    version: 1.0.0
    interface:
      uri: interfaces/loan-risk-gate.md
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
assets:
  - asset_id: score-bands
    source: assets/score-bands.json
    path: source/data/score-bands.json
    role: runtime-data
    media_type: application/json
    pin: sha256:78a2f9d4c0b3104c790b00de737487225a5e24dd5e4d0a1e10c1d7efc50cb4a0
---
# Loan Risk Gate

This Component classifies a personal-loan applicant from two numbers: age in whole
years and annual income in whole currency units. Underwriters, origination desks, and
credit-policy sandboxes can fork it when they need a small, reviewable UNIQUE decision
table rather than prose scenarios. The closed risk intervals live in DMN, the named
income bands live in the pinned JSON asset, and the portable call/result boundary lives
in `interfaces/loan-risk-gate.md`.

```mermaid
flowchart LR
    A["Age"] --> D["UNIQUE DMN table"]
    I["Income"] --> D
    I --> B["Pinned score bands"]
    D --> R["risk_category"]
    B --> S["score_band"]
    R --> O["Loan-risk result"]
    S --> O
```

The generated application exposes the `sample.portable-app` capability. Its exact
input, output, failure, DMN-result, and pinned score-band obligations are owned by the
public interface rather than duplicated in this descriptive authoring body.
