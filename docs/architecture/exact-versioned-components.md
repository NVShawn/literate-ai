# Exact versioned identities

`literate-ai` separates the Python distribution release from versions in the
Component domain. The distribution uses PEP 440 (`0.1.1`); Components, Flavors,
target profiles, model groups and policies, workflows, and authoring skills use strict
Semantic Versioning 2.0.0 (`major.minor.patch[-prerelease][+build]`). A distribution
version is never accepted as a Component version by accident.

## Identity rule

Every versioned object has three independent identity dimensions:

1. a stable logical coordinate or identifier;
2. an explicit semantic version;
3. a content identity for the exact immutable revision.

`VersionedContentRef` carries that generic triple. `ComponentRevisionRef` is the
stronger Component-specific form containing `ComponentCoordinate`, SemVer, and a
SHA-256 `ContentIdentity`. Callers must retain all three. A digest alone proves bytes
but loses the human-facing version contract; a coordinate and version alone can be
rebound to different bytes and is therefore insufficient for generation or linking.

Registries index immutable revisions and permit several versions—and several revisions
of one version—to coexist. Exact references resolve directly. Compatibility lookup by
logical ID or coordinate succeeds only when the result is unique; ambiguity is an
error, never an implicit "latest" selection.

## Propagation

- Component composition accepts and emits exact Component refs while preserving exact
  revision dependency edges.
- Flavor locks bind the exact base Component ref, versioned target profile, and ordered
  exact Flavor refs.
- Model groups may pin exact endpoint refs, policies may pin exact group refs, and route
  decisions record both selected exact refs. Bare-ID routing remains a compatibility
  path only while unique.
- Skill catalogs retain all revisions. `SkillRef` selects exact skill bytes; exact
  dependency refs reject a same-ID but different-version substitute.
- Bundle manifests and every bundle dependency bind a `ComponentRevisionRef`; the ref
  participates in the content-addressed manifest identity.
- Publication manifests and receipts carry the exact Component ref. Import requires an
  expected ref and rejects a different coordinate, version, or revision before copying
  content.

## Compatibility and migration

Compatibility is explicit. `SchemaCompatibilityReader` admits only registered legacy
schemas, applies deterministic one-step migrations, rejects future schemas, and exposes
stable ambiguity errors. Legacy target profiles require a caller-supplied SemVer.
Legacy bundle manifests require a caller-supplied exact Component ref; legacy bundle
dependencies additionally require exact dependency refs. Legacy publication manifests
require an explicit exact request and authorization migration context because older
records lack effective revision, source, provenance, target, and policy facts. The
framework does not invent `0.0.0`, infer a namespace, manufacture publication approval,
or select the newest cached version.

This means old caches remain readable only when their authoritative catalog or lock can
supply the missing identity. Otherwise migration fails closed and the object must be
re-resolved from authoritative source.

## Downstream OVA DTO contract

OVA maps its product schema to these neutral DTOs once, at the adapter boundary:

- `OvaComponentIdentity` -> `ComponentRevisionRef` after revision construction;
- dependency/cache/package/publication records -> the same exact Component ref;
- target profiles and Flavor locks -> strict profile SemVer plus exact base/Flavor refs;
- model endpoints/groups/selectors -> `VersionedContentRef` values;
- skills -> strict `SkillRef` values, including dependency refs.

OVA may retain mutable display aliases such as "latest" for UI convenience, but
generation, build, link, publication, and import APIs must consume exact refs.
Provider-neutral SemVer parsing, coordinates, revision references, coexistence,
resolution, and serialization are framework authority; downstream copies are not part
of the post-cutover architecture.
