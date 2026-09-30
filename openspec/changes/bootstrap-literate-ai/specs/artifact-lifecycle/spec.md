## ADDED Requirements

### Requirement: Package manifests and blobs are immutable

The framework SHALL store source, build, and artifact bundles as immutable
content-addressed records with exact typed forward dependencies.

#### Scenario: New consumer uses an existing package

- **WHEN** a Component begins depending on an existing artifact bundle
- **THEN** a separate dependency projection records the reverse edge
- **AND** the existing bundle manifest and identity remain unchanged

### Requirement: Dependency closure is complete and typed

Build and link operations SHALL distinguish direct, transitive, build, runtime, optional,
capability, and provider-generated edges and SHALL fail when a required artifact is absent.

#### Scenario: Required object dependency is not cached

- **WHEN** a build requires an artifact that is not locally available
- **THEN** the materializer faults and verifies it or the build fails explicitly
- **AND** no incomplete artifact closure is recorded as successful

### Requirement: Every SBOM is complete CycloneDX 1.7 evidence

Every generated source tree SHALL contain canonical CycloneDX 1.7 JSON at
`source/.literate/sbom.cdx.json` with lifecycle `pre-build`. Every accepted build SHALL
have a separate CycloneDX 1.7 `post-build` document with exact resolved versions. Both
documents SHALL include the root Component, every Literate-AI-managed Component and
repository-source dependency, and the complete direct and transitive dependency graph.
Both documents SHALL bind the same exact `ComponentComposition` identity; managed
Component membership or relationships cannot grow or change after source generation.
For every managed relationship, namespaced evidence SHALL bind its source, target,
dependency kind, optionality, and exact relationship identity even when standard
CycloneDX `dependsOn` deduplicates the same node pair. The post-build document SHALL bind
the exact pre-build BOM and preserve its managed inventory, relationships, and edges.

#### Scenario: A dependency leaf is omitted from the graph

- **WHEN** an inventory object has no explicit dependency entry, even when it has no
  children
- **THEN** SBOM validation fails before generation output or build acceptance
- **AND** a post-build document cannot claim a complete composition until every resolved
  direct and transitive binary dependency has an exact version and explicit edge

#### Scenario: A cache entry carries dependency evidence

- **WHEN** an accepted generated-source entry is published or materialized
- **THEN** it carries validated source and resolved CycloneDX document identities plus
  their complete graph and managed-graph bindings
- **AND** materialization remains acceptance-untrusted until the current lifecycle
  independently revalidates the candidate

#### Scenario: Resolution tries to erase an intended dependency

- **WHEN** a post-build SBOM omits or reparents a component or edge present in the
  exact pre-build SBOM
- **THEN** source-to-resolved transition validation fails
- **AND** generated tests, independent acceptance, and workspace admission do not run

#### Scenario: A shared Component has different relationship semantics

- **WHEN** two consuming Components depend on one shared Component with different
  dependency kinds or optionality
- **THEN** both CycloneDX documents contain one deduplicated shared inventory node and
  every exact incoming edge
- **AND** namespaced managed-edge evidence retains each edge's own kind, optionality,
  and relationship identity

#### Scenario: Host observation cannot prove the binary closure

- **WHEN** the selected non-executing inspector is missing or changes, a linked image is
  unresolved or ambiguous
- **THEN** post-build dependency resolution fails before generated tests run
- **AND** no adapter executes the generated binary or substitutes a partial graph

#### Scenario: Generated dependency intent is not represented by the source SBOM

- **WHEN** a supported manifest, lock, or external import is absent from the exact source
  SBOM
- **THEN** source admission fails before any generated compiler or build command runs
- **AND** post-build resolution repeats that check against the unchanged generated tree

#### Scenario: A platform chooses a compatible inspector

- **WHEN** macOS uses exact `xcrun`/`dyld_info`, Linux uses exact `readelf`/`ldconfig`
  or a compatible non-executing `objdump`, or Windows uses `dumpbin`, `llvm-readobj`,
  or `objdump`
- **THEN** the resolver binds the exact inspector identity, content-derived version,
  digest, and bounded invocation
- **AND** Linux observation does not invoke `ldd` on untrusted generated binaries

### Requirement: Builders are provider ports with exact inputs

A builder SHALL consume an immutable source bundle, toolchain lock, sandbox profile, and
valid build authorization and SHALL emit logs and outputs as an attestable build bundle.

#### Scenario: Python source is ready but not authorized

- **WHEN** a Python builder receives source without a matching unexpired authorization
- **THEN** it does not invoke `py_compile` or any compiler process

### Requirement: Publication and verified import are explicit

The framework SHALL support explicit resumable publish, fetch, verification, and import
of signed immutable manifests and blobs without coupling publication to local usability.

#### Scenario: Component is usable only locally

- **WHEN** a package completes without a publication request
- **THEN** it remains usable from local cache
- **AND** no remote target is mutated

#### Scenario: Publication is interrupted

- **WHEN** a publish process stops after transferring some blobs
- **THEN** its append-only record supports verified resume without republishing completed
  immutable content

#### Scenario: Import expects a different Component revision

- **WHEN** a publication manifest's coordinate, semantic version, or revision digest
  differs from the exact Component ref requested by the importer
- **THEN** import fails before transferring any published blob

### Requirement: Bundles pin exact versioned dependency refs

Every bundle and typed dependency edge SHALL bind the logical Component coordinate,
semantic version, immutable Component revision, and exact dependency bundle.

#### Scenario: Two dependency versions coexist in cache

- **WHEN** a consumer bundle pins one of two available dependency versions
- **THEN** closure follows only the exact pinned revision and bundle
- **AND** no mutable latest alias participates in build or link identity
