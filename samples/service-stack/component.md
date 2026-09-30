---
namespace: samples
version: 1.0.0
display_name: Composable Invoice Service
profiles:
  - application
  - portable
  - sample
  - stack
sample: true
inheritable: false
provides:
  - name: sample.portable-app
    version: 1.0.0
    interface: null
requires:
  - requirement_id: invoice-service-interface
    capability: literate-ai.invoice-service
    version_range: ">=1,<2"
    dependency_kind: generation
    optional: false
    constraints: []
  - requirement_id: invoice-service
    capability: literate-ai.invoice-service
    version_range: ">=1,<2"
    dependency_kind: runtime
    optional: false
    constraints: []
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
  - slot_id: build-system
    axis: build.system
    cardinality: exactly-one
    capability_contract: sample.portable-app
  - slot_id: language
    axis: implementation.language-ecosystem
    cardinality: exactly-one
    capability_contract: sample.portable-app
  - slot_id: os
    axis: platform.os
    cardinality: exactly-one
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
# Composable Invoice Service

This is the clearest Component-composition example in the catalog: an application owns
the executable boundary, an invoice service owns line aggregation, and an exact-money
library owns integer discount arithmetic. The same shape adapts naturally to carts,
quotes, subscriptions, usage billing, and procurement tools.

```mermaid
flowchart LR
    R["Invoice request"] --> A["Application entrypoint"]
    A --> S["Invoice service<br/>aggregate lines"]
    S --> M["Money capability<br/>basis-point discount"]
    M --> O["Exact cent totals"]
```

## Application contract

| Concern | Decision |
| --- | --- |
| Application ID | `service-stack` |
| Kind | `invoice` |
| Entrypoint | `run` |
| Currency storage | Integer cents (`integer-cents`) |
| Discount unit | Basis points (`basis-points`) |
| Rounding | Half up (`half-up`) |

### Requirement: Exact transitive composition

The `component://samples/service-stack` application SHALL require
`literate-ai.invoice-service` from `component://literate-ai/invoice-service`. That Component SHALL
in turn require `literate-ai.money-calculation` from
`component://literate-ai/money-calculation`. Generation SHALL preserve all three independently
specified logical modules or build targets. Each logical dependency SHALL have a
generation edge carrying only the provider's public interface and a runtime edge
carrying its accepted provider artifact.

#### Scenario: Stack resolution

- **WHEN** the application stack is composed
- **THEN** the service and library each occur once with four exact phase-specific edges

### Requirement: Executable host outcome

The generated invoice application SHALL compose an entrypoint, service layer, and exact
integer-money library to price line items without floating-point currency arithmetic.

#### Scenario: Compiled entrypoint runs

- **WHEN** the compiled `run` entrypoint prices five units across two lines with a ten-percent discount
- **THEN** it returns a 4095-cent subtotal, 410-cent discount, and 3685-cent total
