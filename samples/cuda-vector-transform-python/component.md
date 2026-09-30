---
namespace: samples
version: 1.0.0
display_name: CUDA Matrix Product
profiles:
  - accelerator
  - application
  - python
  - sample
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
  - slot_id: accelerator
    axis: accelerator
    cardinality: exactly-one
    capability_contract: sample.portable-app
  - slot_id: build-system
    axis: build.system
    cardinality: zero-or-one
    capability_contract: sample.portable-app
entrypoints:
  - name: run
    kind: portable-application
    path: run
acceptance_contracts: []
source_dependencies: []
---
# CUDA Matrix Product

This Component performs an exact batched linear-algebra building block with a Python
interface and CUDA execution. Matrix products sit beneath inference, graphics,
recommendation, scientific computing, and signal-processing applications. The integer
domain provides a stable black-box oracle while still exercising a genuine GPU library
stack rather than merely querying device metadata.

```mermaid
flowchart LR
    L["Left matrix"] --> C["CuPy device arrays"]
    R["Right matrix"] --> C
    C --> G["CUDA matrix product"] --> O["Exact matrix + checksum"]
```

## Application contract

The `run` entrypoint accepts exactly one object containing `left` and `right`. Each is a
nonempty rectangular two-dimensional array of signed integers, and the left column count
must equal the right row count. All input values, products, accumulated cells, and the
final checksum SHALL fit in a signed 64-bit integer.

The complete result contains exactly `backend`, `device_executed`, `rows`, `columns`,
`values`, and `checksum`. `backend` is `cupy-cuda`; `device_executed` is true only after
CuPy performed and synchronized the matrix product on the selected NVIDIA device.
`values` is the conventional matrix product and `checksum` is the exact sum of its cells.

### Requirement: Execute matrix multiplication through CuPy CUDA

The implementation SHALL use an exactly resolved CUDA-specific CuPy distribution,
construct both operands as signed 64-bit device arrays, perform the product on the
selected CUDA device, synchronize it, and transfer only the final result to the host.
NumPy, an alternate CPU backend, or implicit fallback does not satisfy this Component.

#### Scenario: The generated Python CUDA entrypoint runs

- **WHEN** two compatible rectangular integer matrices are supplied
- **THEN** CuPy executes their exact product on the NVIDIA device and the application returns its dimensions, cells, checksum, backend, and device-execution evidence
