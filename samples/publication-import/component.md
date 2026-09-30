---
namespace: samples
version: 1.0.0
display_name: Reproducible Release Manifest
profiles:
  - application
  - portable
  - publication
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
entrypoints:
  - name: run
    kind: portable-application
    path: run
acceptance_contracts: []
source_dependencies: []
---
# Reproducible Release Manifest

The Component turns a portable file set into a canonical content-addressed release
manifest. It is reusable for plugin bundles, static-site releases, configuration packs,
model assets, and any publication workflow that must prove two imports contain the same
bytes independently of filesystem enumeration order.

```mermaid
flowchart LR
    F["Portable file map"] --> H["Hash each file"]
    H --> S["Sort manifest by path"]
    S --> J["Canonical JSON bytes"]
    J --> R["Immutable release digest"]
```

## Application contract

| Concern | Decision |
| --- | --- |
| Application ID | `publication-import` |
| Kind | `publication-manifest` |
| Entrypoint | `run` |
| Digest | SHA-256 (`sha256`) |
| Text encoding | UTF-8 |
| Manifest encoding | Canonical JSON (`canonical-json`) |

The `run` entrypoint SHALL accept exactly one argument object containing exactly one
required field, `files`, whose value is an object mapping portable path strings to UTF-8
content strings. The result SHALL contain exactly `file_count`, `files`, and
`release_digest`; each item in the result's path-sorted `files` array SHALL contain
exactly `digest`, `path`, and `size`.

### Requirement: Verified publication roundtrip

Publication SHALL be explicit and idempotent, and import SHALL reproduce identical
verified bytes.

#### Scenario: Publish twice and import once

- **WHEN** one immutable revision is published repeatedly and imported into an empty cache
- **THEN** publication is reused and imported source bytes match exactly

### Requirement: Executable host outcome

The generated publication application SHALL create a deterministic manifest of the
UTF-8 `files` in its sole argument object and a release identity over the canonical
manifest. The manifest value SHALL be
the path-sorted JSON array of objects containing exactly `digest`, `path`, and `size`.
Its canonical bytes SHALL be UTF-8 JSON with object keys sorted, no insignificant
whitespace, and no wrapper object. `release_digest` SHALL be the lowercase SHA-256
hexadecimal digest of those exact array bytes. Every file and release digest SHALL be
exactly 64 lowercase hexadecimal characters with no algorithm prefix.

### Worked digest vectors

A SHA-256 digest cannot be derived by hand, and generation runs without an execution
tool, so this specification supplies the exact digests rather than expecting them to be
computed during generation. Each vector below was produced by the algorithm above and is
authored specification content: copy these values verbatim into generated
implementation tests instead of guessing, recomputing, or omitting a case. None of these
argument vectors is an acceptance invocation.

| Vector | `files` argument | Exact result |
| --- | --- | --- |
| Two ordinary files | `{"alpha.txt": "alpha\n", "beta.txt": "beta\n"}` | `file_count` 2; `alpha.txt` size 6 digest `b6a98d9ce9a2d9149288fa3df42d377c3e42737afdcdaf714e33c0a100b51060`; `beta.txt` size 5 digest `f2c82decdd7181cf98945929a62598db7e6b477e11f6e0eb0ae97020eff151ad`; `release_digest` `1f7f638808e7fb38d4f0655e61b6ed8b7c60b8d00000f3de3d7e78bac7c771d7` |
| Empty file content | `{"empty.md": ""}` | `file_count` 1; `empty.md` size 0 digest `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`; `release_digest` `0fd1cda5b689ca75cc732a29ce85520e12a81228c825ce0c5e692df39bf04609` |
| No files at all | `{}` | `file_count` 0; `files` `[]`; `release_digest` `4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945` |
| Unsorted input order | `{"zulu.txt": "z", "alfa.txt": "a"}` | `file_count` 2; path-sorted `alfa.txt` size 1 digest `ca978112ca1bbdcafac231b39a23dc4da786eff8147c4e72b9807785afee48bb` then `zulu.txt` size 1 digest `594e519ae499312b29433b7dd8a97ff068defcba9755b6d5d00e84c524d67b06`; `release_digest` `85c44179d725240b909fe67e3322cb552d853826b7c2f98d3e70111a04d3ae1d` |
| Multibyte content | `{"unicode.txt": "h\u00e9llo\n"}` | `file_count` 1; `unicode.txt` size 7 (UTF-8 bytes, not characters) digest `b95becd154aa095f76c4ca47a5aeb8350d6dfcb838404edfc9dae06628de938d`; `release_digest` `9fc9dc5f2f95d24fe6851f2e022ea9f8971efe792799a1a3f2b5d00318ee3cf9` |

The unsorted-input vector fixes path ordering as an invariant independent of argument
order, and the multibyte vector fixes `size` as a UTF-8 byte count.

#### Scenario: Compiled entrypoint runs

- **WHEN** the compiled `run` entrypoint receives a README and JSON configuration file
- **THEN** it returns their exact sizes and SHA-256 identities plus one canonical release digest
