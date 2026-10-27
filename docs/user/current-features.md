# Current features and release posture

This page is the version-aware map of Literate AI's operator-visible features.
The source tree on `main` is the **1.4.0 development line**. The latest published
release is **1.3.0**, and **1.3.1** is a separately governed patch program. A feature
described as *current development* below may therefore be newer than the latest wheel.
Use the [release page](https://github.com/jordanhubbard/literate-ai/releases/latest)
for published artifacts and the [active queue](../roadmap/active-work.md) for unfinished
qualification.

## Available in the current development tree

| Capability | What it provides | Start here |
| --- | --- | --- |
| Reviewed onboarding | Read-only plans and acknowledged application for new and existing repositories through `litai onboard create` and `litai onboard adopt`. | [Getting started](getting-started.md) |
| Readable authority | Markdown Component specifications, selected Flavors, pinned skills, workflows, routing, and lifecycle drivers form the exact generation recipe. | [Core concepts](concepts.md) |
| Exact Component DAGs | Projects inherit and compose versioned Components, Flavors, skills, assets, and repository sources without flattening their ownership. | [Components and versioning](components-and-versioning.md) |
| Evidence-gated lifecycle | `verify`, `lock`, `plan`, `build`, `test`, `run`, `package`, `rebuild`, and `release` retain independently checkable identities and receipts. | [Framework flow](framework-flow.md) |
| Multi-language targets | Cataloged language Flavors cover C++, Elixir, Go, JavaScript, Python, Rust, Swift, TypeScript, and Zig. Bazel and Make are the primary build-system pair; Cargo, CMake, Mix, and repo-man are also cataloged for bounded profiles. | [Flavors and targets](flavors-and-targets.md) |
| Native and remote execution | Local, SSH, and private command workers share typed lifecycle requests, capability checks, and immutable artifact boundaries across Linux, macOS, and Windows. | [Test matrix](test-matrix.md) |
| Retained-source adoption | Existing source remains release authority while a reviewed specification is drafted and repeatedly qualified; wrapping a repository never claims automatic conversion. | [Source to specification](source-to-specification.md) |
| CycloneDX and package custody | Source and resolved dependency graphs, cache identities, package plans, constructed bytes, and independent package verification remain distinct. | [Caches, packages, and publication](caches-packages-publication.md) |
| HTML repository explorer | A generated, single-file, provenance-bound view of effective repositories, Components, Flavors, skills, identities, and selected source excerpts. | [HTML repository explorer](html-repository-explorer.md) |
| Explicit release goals | Release publication checks a machine-readable goal ledger; incomplete work must finish or move only with recorded approval and a named future release. | [Project releases](../architecture/project-releases.md) |

## Current development emphasis

The 1.4.0 line is extending the same authority and evidence model rather than
introducing a second orchestration system. Active work includes complete retained
native-project lifecycle integration, bounded action-DAG scheduling and cache reuse,
native package and platform qualification, repository succession, and exact installed
or public-path acceptance. These are real implementation programs, but their unchecked
acceptance items are not release claims.

The 1.3.1 patch line remains separate. Its release-goal manifest owns the patch's
documentation, performance, retained-source, Windows, packaging, and release-evidence
closure. Work on `main` does not silently satisfy those patch gates.

## Deliberate boundaries

- Generated source is not trusted merely because a model produced it or its own tests
  pass. Independent acceptance and current receipts remain required.
- The HTML explorer is an authority and provenance browser, not a general source-code
  IDE with symbol navigation.
- FreeBSD support is active work, not a fourth qualified first-class platform yet.
- Source-to-specification translation does not retire original-source authority until
  repeated clean regeneration and parity evidence satisfy the promotion contract.
- Catalog presence is not universal qualification. A Flavor can be available for a
  bounded profile while broader native, installed, worker, or publication evidence
  remains open.

## Learn by making strange things

The [visual labs](../../labs/) are six progressively composed, free-standing examples.
They begin with a grumpy counter and end with a component cathedral combining math,
physics, 3D projection, music, audio, and video capture. Each lab exposes its readable
Component specification and interactive DAG beside the running artifact.
