---
namespace: samples
version: 1.0.0
display_name: Windows Path Auditor
profiles:
  - application
  - sample
  - windows
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
# Windows Path Auditor

This Windows-only Component classifies and normalizes drive, UNC, rooted, and relative
paths before they enter a build or deployment manifest. It is a reusable boundary for
installers, workspace managers, and artifact-copy tools that cannot treat POSIX path
rules as universal.

```mermaid
flowchart LR
    I["Windows path strings"] --> N["Normalize separators"]
    N --> K["Classify drive, UNC, rooted, relative"]
    K --> R["Stable audit records"]
```

## Application contract

The `run` entrypoint accepts one object containing exactly `paths`, an array of nonempty
strings. Convert every `/` to `\`, collapse repeated separators except the leading UNC
pair, and preserve drive-letter spelling. Emit one result per input in order, with
exactly `original`, `normalized`, `kind`, and `root`.

Kinds are `drive` for `X:\...`, `unc` for `\\server\share\...`, `rooted` for a single
leading backslash, and `relative` otherwise. A drive root is `X:\`; a UNC root is
`\\server\share`; a rooted path root is `\`; a relative path root is the empty string.

### Requirement: Audit Windows-native paths

The application SHALL classify Windows paths without silently applying POSIX semantics.

#### Scenario: The compiled Windows entrypoint runs

- **WHEN** drive, UNC, rooted, and relative paths are supplied
- **THEN** the application returns their exact normalized forms, kinds, and roots in input order
