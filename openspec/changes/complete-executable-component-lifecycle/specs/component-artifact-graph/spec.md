## Purpose

Defines arbitrary-byte resources and typed build artifacts as identity-bound graph inputs and outputs across Component build, link, package, and release stages.

> **Implementation status:** resource/source manifests, typed exports, build manifests/actions, isolated materialization, composite build requests, exact link-plan contracts, target-specific package plans/results, truthful runtime requirements, and release artifact sets are implemented. Production link/package adapters and release qualification remain open.

## ADDED Requirements

### Requirement: Authored resources are separate from generated text
The framework SHALL represent authored arbitrary-byte resources with logical POSIX paths,
roles, media types, owning Components, content identities, sizes, and provenance. A source
manifest SHALL assemble generated text and locked authored resources without allowing a
generator to replace, shadow, reinterpret, or omit an admitted resource.

#### Scenario: Generated output collides with a locked resource
- **WHEN** a generated text path matches an authored resource path
- **THEN** source assembly fails before build and preserves the resource bytes unchanged

### Requirement: Component artifacts have typed exports and imports
Every cross-Component build input SHALL bind an exact artifact export to a compatible
artifact import using logical name, role, media type, target, compatibility or ABI
contract, producer revision, build action, and blob identity.

#### Scenario: Consumer requests wrong target artifact
- **WHEN** an otherwise valid export has a target incompatible with the consumer import
- **THEN** linking fails before the consumer or root artifact is launched

### Requirement: Build and link plans are exact graph projections
The framework SHALL produce per-Component build manifests and build action requests, then
explicit link and package plans for root targets. Plans SHALL derive from the locked
Component DAG, consume only accepted dependency exports, deduplicate shared artifacts,
and retain provenance from every root output to all Component, resource, toolchain, and
action inputs.

#### Scenario: Diamond graph is linked
- **WHEN** B and C consume the same exact export from D
- **THEN** the root link graph references one D artifact and retains both dependency paths

### Requirement: Package kind is target-specific
A package plan SHALL name its exact target and package kind. Standalone executables,
runtime bundles, directories, archives, installers, images, and other launchable artifacts
MAY be supported when selected by compatible target and Flavor policy; the framework SHALL
NOT claim one universal binary form.

#### Scenario: Python runtime bundle is packaged
- **WHEN** target policy selects a runtime bundle instead of a native executable
- **THEN** the release declares its complete external runtime closure rather than labeling it a standalone binary

### Requirement: SBOM evidence spans source through release
Source, resolved, build, link, package, and release evidence SHALL preserve one complete
Component dependency graph while adding exact packages, binaries, resources, toolchains,
and runtime dependencies discovered at each stage. A changed or missing managed edge SHALL
fail before the next executable stage.

#### Scenario: Package SBOM omits a shared resource
- **WHEN** the package manifest references a resource absent from its release SBOM closure
- **THEN** release qualification fails before publication or deployment
