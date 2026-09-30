---
namespace: samples
version: 1.0.0
display_name: Build Pipeline Dependency Planner
profiles:
  - application
  - graph
  - planner
  - portable
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
    uri: skills/specification-to-source/portable-application-implementation/SKILL.md
  - kind: specification-to-source-skill
    uri: skills/specification-to-source/portable-specification-planning/SKILL.md
  - kind: model-selection
    uri: model-selections/dependency-planner.json
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
# Dependency Planner

The planner preserves dependency truth while exposing the parallelism hidden by a
simple list of tasks. A deterministic ready queue gives one reproducible legal order;
dynamic programming over that order gives the earliest possible completion and a
representative longest chain. It is the smaller Rust-first building block for build and
release pipelines; the richer Critical Path Scheduler additionally exposes full
earliest/latest schedules and slack.

```mermaid
flowchart LR
    I["Portable task graph"] --> G["Validate and index graph"]
    G --> Q["ASCII-ordered ready queue"]
    Q --> O["Complete topological order"]
    O --> E["Earliest completion per task"]
    E --> M["Minimum completion with unlimited workers"]
    E --> P["Backtrack deterministic predecessor"]
    P --> C["Representative critical path"]
    O --> R["Stable JSON result"]
    M --> R
    C --> R
```

## Application contract

| Concern | Decision |
| --- | --- |
| Application ID | `dependency-planner` |
| Kind and algorithm | `dependency-planner` using `critical-path-method` |
| Entrypoint | `run` |
| Task count | 1 through 512 |
| Duration field and bounds | `duration_minutes`, 1 through 1,000,000 (`maximum: 1000000`) |
| Dependency field | `depends_on` |
| Maximum total duration | 2,147,483,647 (`maximum_total: 2147483647`) |
| Ordering | ASCII task ID ascending (`ascii-task-id-ascending`) |
| Parallelism | Unlimited |

### Requirement: Validate a portable task graph

The application SHALL accept exactly one entrypoint argument: an object whose `tasks`
array contains between one and 512 objects with a non-empty ASCII string `id`, an
integer `duration_minutes` from 1 through 1,000,000, and a `depends_on` array of task
IDs. The sum of durations SHALL NOT exceed 2,147,483,647. Task IDs SHALL be unique,
dependencies SHALL name tasks in the same request, duplicate dependency edges and self
dependencies SHALL be rejected, and the complete graph SHALL be acyclic.

#### Scenario: Valid task graph

- **WHEN** a task graph has unique IDs, valid dependency edges, and no cycle
- **THEN** every task and dependency participates exactly once in the plan

### Requirement: Produce a deterministic dependency plan

The application SHALL produce a topological order by repeatedly choosing the
lexicographically smallest task ID whose dependencies have all been emitted. It SHALL
report `task_count`, `edge_count`, and that complete `topological_order`.

For unlimited parallel execution, each task's earliest completion time SHALL be its
duration plus the greatest earliest completion time among its dependencies, or just its
duration when it has none. The result SHALL report the greatest task completion time as
`minimum_completion_minutes`. It SHALL also report one `critical_path` by following the
predecessor with the greatest completion time, choosing the lexicographically smallest
ID on a tie; if final tasks tie, it SHALL use the lexicographically smallest final ID.

#### Scenario: Parallel release plan

- **WHEN** schema work feeds API and UI work which both feed a release task
- **THEN** the result exposes the deterministic order and the longest dependency chain rather than summing work that can run in parallel

### Requirement: Emit a stable machine result

The runnable entrypoint SHALL write exactly one JSON object with the fields
`critical_path`, `edge_count`, `minimum_completion_minutes`, `task_count`, and
`topological_order`, and no diagnostic text on standard output.

#### Scenario: Native executable is verified

- **WHEN** a generated Rust implementation is compiled and invoked with the declared JSON argument array
- **THEN** its parsed result equals the expected dependency plan
