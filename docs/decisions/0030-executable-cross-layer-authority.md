# ADR 0030: Make Cross-Layer Authority Executable Once

- Status: Accepted
- Date: 2026-09-01 (accepted after the repository-wide investigation and
  operator planning approval)
- Decision owners: literate-ai maintainers
- Release target: 0.9.0
- Roadmap: [AUTHORITY-FACTORING-001](../roadmap/active-work.md#x-authority-factoring-001-make-cross-layer-authority-executable-for-090)
- Detailed plan: [0.9.0 executable-authority program](../roadmap/0.9.0-executable-authority.md)

## Context

Literate AI documents three important boundaries clearly but does not enforce them
through one implementation at every consumer:

1. Directory sentinels form a skill taxonomy. A sentinel owns resources until a
   nested sentinel begins a child scope, and a child inherits its ancestor chain.
   Repository catalog loading implements that rule, while changed-skill evaluation,
   initialization, validation, authority-graph construction, and authoring
   normalization each discover or project skills differently.
2. Python is the deterministic connector between an agent and the host. Several
   agent skills still ask the model to allocate identifiers, parse records, resolve
   paths, edit structured state, or reproduce command algorithms because the
   corresponding application service is internal or absent from the CLI/MCP surface.
3. Portable logical paths are distinct from host-native configuration, data, cache,
   state, temporary, and installation locations. Logical-path validation, safe
   filesystem traversal, and project cache custody are centralized, but host paths
   are independently reconstructed across adapters. That produces Linux-style
   defaults on macOS, inconsistent Windows fallbacks, relative XDG acceptance in one
   adapter, and duplicated install/object-directory rules.

Two current gate failures expose the same broader issue. An ordinary conformance test
crosses a live coding-model boundary, and self-host source admission loses declared
optional dependencies between project metadata and the generated source BOM. Both are
contracts represented in one layer but not carried through the next.

## Decision

### Restore the deterministic and dependency contracts first

The standard Python gate must not invoke a live coding CLI, model, network endpoint, or
authentication flow. Tests of model-selection plumbing inject the live verifier;
explicit live qualification keeps the real preflight.

Self-host dependency authority records core requirements and named optional groups.
An optional import is admissible when its distribution is declared in an optional
group, without installing that group for core-only verification. An undeclared import
continues to fail closed.

### Use one skill-catalog service

Introduce one `AgentSkillCatalog` application service that discovers sentinel skills,
computes owned-resource closure and ancestor inheritance, validates identity and
references, and derives impact. Changed-skill evaluation, initialization, project
validation, authority graphs, and authoring tools consume that service rather than
performing independent glob or prefix scans.

An initialized catalog is either the complete canonical catalog or an explicit,
validated subset. It may not contain dangling skill routes. Evaluation projections
contain an item's owned closure plus the ancestor context required to interpret it;
they do not recursively absorb descendant sentinels.

### Add a narrow host-platform path policy

Keep the existing canonical relative-path contract, safe filesystem helpers, project
cache custody, and `importlib.resources` package-resource loading. Add a separate,
immutable and injectable host-path policy for semantic operator locations: config,
data, cache, state, managed tools, installation, temporary files, and short staging.
Keep OS-owned files and conventional runtime roots in the smaller `HostSystemPaths`
surface so AppArmor, `os-release`, `ldconfig`, and sandbox mount policy never consults
user configuration or the home directory.

The policy uses Python standard-library facilities (`os`, `pathlib`, `tempfile`,
`shutil`, `sysconfig`, `importlib.resources`, and Windows Known Folder access behind
the adapter). Durable user configuration follows ADR 0031's explicit POSIX
`$HOME/.config/literate-ai` and Windows Known Folder contract; cache, data, state,
temporary, and installation locations retain their distinct platform semantics.
Target-machine tool constants embedded in remote transport programs and deliberately
standalone stage-zero bootstraps remain narrow exceptions; they do not import
coordinator-host defaults. The static gate rejects ambient `Path.home()`, host-root
environment reads, and absolute `Path(...)` literals outside the central owner.

### Put deterministic work behind application services

CLI and MCP handlers are adapters over shared Python application services. Add public
operations for user-directed work records, operator MCP catalog writes/discovery,
channel trailer ingestion, deterministic stack compatibility resolution, and document
artifact inspection where the corresponding skill currently reproduces mechanics.
Expose skill/document/static-resource lookup through MCP resources. Agent skills retain
only routing, judgment, and fail-closed boundaries and name the exact command or
resource that performs deterministic work.

### Preserve compatibility while centralizing

Explicit overrides retain precedence. A native-path migration detects a single legacy
location and reports or migrates it deliberately; it never silently merges conflicting
state. New static checks prevent additional host-default and skill-discovery logic from
appearing outside the owning services and documented bootstrap/target exceptions.

## Consequences

The first changes are small release-blocking contract repairs. Catalog and host-path
centralization then require staged migrations because they touch multiple consumers,
but each stage can be proven with external behavior before the next begins.

The framework gains fewer places where taxonomy, dependency, or platform behavior can
drift. Skills become shorter because deterministic work becomes callable instead of
prompt text. The cost is a stronger internal service boundary and migration tests for
legacy operator paths. This ADR does not turn every filesystem path into a member of
one god object, make optional dependencies mandatory, or move target-specific tool
knowledge out of its adapter.
