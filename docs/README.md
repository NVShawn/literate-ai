# Documentation

- [Coding CLI release plugins](user/coding-cli-plugins.md)
- [1.1 SDLC audit and graded concept assessment](roadmap/1.1-sdlc-audit.md)

## User documentation

- [Rebuildable instructional videos](user/instructional-videos.md)
- [Watch the introductory courses](courses/README.md)
- [Native gRPC oracle declarations and qualification boundary](user/native-grpc-oracles.md)
- [Independent child repository lifecycle commands](user/repository-lifecycle.md)

Start with [Getting started](user/getting-started.md) for the shortest runnable path,
then use the [Literate AI user guide](user/README.md) as the task map.

For a presentation-length introduction, use the editable
[Literate AI overview deck](presentations/literate-ai-overview.pptx). It explains the
thesis, lifecycle, evidence model, source-promotion path, cost controls, and enterprise
agent-system boundary for a technically sophisticated new audience.

For the current manager and engineering narrative, use the reproducible
[manager and engineering overview package](presentations/literate-ai-manager-overview/README.md).
Maintained presentations and documents are modeled inside the Component system; read
[documentation artifacts as Components](architecture/documentation-artifacts.md) for the
document-pair capability, its ecosystem Flavors, and the terminal-Component rule.

For the complete test story, read [the three test boundaries](user/framework-flow.md#three-test-artifacts-three-jobs),
[clean major rebuilds](user/models-and-generation.md#clean-major-rebuilds), and
[project test receipts](user/configuration-and-cli.md#project-test-receipts) together.

For the literate-documentation contract, start with
[the reviewed project graph](user/project-layout.md#documentation-is-a-reviewed-project-graph)
and [documentation authority review](user/configuration-and-cli.md#documentation-authority-review).

For dependency and environment reconstruction, read the
[CycloneDX SBOM and dependency graph](architecture/sbom-and-dependency-graph.md) together
with [cache/publication boundaries](user/caches-packages-publication.md) and
[retained Cargo package provisioning](user/retained-cargo.md).

For the current per-Component implementation boundary, read
[Per-Component execution plans](architecture/component-execution-plans.md). It separates
the implemented Standard-bound CLI, source scheduler, cache publication, and
admission/receipt core from deferred link/package and root-integration coverage plus the
remaining sample and qualification migration. Current
writers use the [`schemas/v2`](../schemas/v2/) catalog; [`schemas/v1`](../schemas/v1/) is
the frozen compatibility catalog.

For work that must survive an agent session, start with the
[active work queue](roadmap/active-work.md) and its
[user-directed work loop](architecture/user-directed-work-loop.md). This is the durable
bridge from user direction to implementation, evidence, changelog, and reusable project
learning.

For repository governance, release-line lockdown, patch authority, pull-request
classification, and safe branch collection, read the
[Release Policy](architecture/project-releases.md#release-policy). The
[configuration reference](user/configuration-and-cli.md#repository-policy-configuration)
lists the per-project fields and their defaults.

- [Installation and first run](user/installation.md)
- [Public repository export](user/public-export.md)
- [Test-suite audit (October 2026)](testing/test-suite-audit.md)
- [Moving from NVIDIA-dev/literate-ai](user/repository-migration.md)
- [Getting started](user/getting-started.md)
- [The framework flow](user/framework-flow.md)
- [Canonical project layout](user/project-layout.md)
- [Core concepts and layered Components](user/concepts.md)
- [Writing readable specifications](user/specifications.md)
- [Components, identities, and exact versioning](user/components-and-versioning.md)
- [Flavors and target profiles](user/flavors-and-targets.md)
- [Concurrent target matrices](user/target-matrices.md)
- [Private cross-platform test matrix](user/test-matrix.md)
- [Source to specification](user/source-to-specification.md)
- [Model groups, selectors, and generation](user/models-and-generation.md)
- [Caches, packages, and publication](user/caches-packages-publication.md)
- [Source trust and build security](user/security.md)
- [Configuration and CLI reference](user/configuration-and-cli.md)
- [Samples and tutorials](user/samples.md)
- [OVA, migration, and rollback](user/ova-and-migration.md)
- [Troubleshooting](user/troubleshooting.md)
- [Glossary](user/glossary.md)

## Architecture and policy

- [NVIDIA library discovery and integration](architecture/nvidia-library-discovery.md)

- [Repository layout](architecture/repository-layout.md)
- [Repository inheritance and update DAG](architecture/repository-inheritance.md)
- [Neutral domain model](architecture/domain-model.md)
- [Exact versioned Components](architecture/exact-versioned-components.md)
- [Component authoring and lock boundary](architecture/component-authoring-lock-boundary.md)
- [Capability-based provider resolution](architecture/provider-resolution.md)
- [Per-Component execution plans](architecture/component-execution-plans.md)
- [Composable Component Flavors](architecture/component-flavors.md)
- [Repository source dependencies](architecture/repository-source-dependencies.md)
- [CycloneDX SBOM and dependency graph](architecture/sbom-and-dependency-graph.md)
- [Source promotion and regenerative qualification](architecture/source-promotion.md)
- [Literate AI and agent-ledger boundary](architecture/agent-ledger-boundary.md)
- [Authority learning loop](architecture/authority-learning-loop.md)
- [HTML observability boundary and contracts](architecture/html-observability.md)
- [Accepted 1.1 capability boundaries](decisions/0039-1.1-capability-boundaries.md)
- [Proposed retained Cargo library bridge](decisions/0040-retained-cargo-library-bridge.md)
- [Accepted action-DAG scheduling and shared verified caches](decisions/0044-action-dag-and-shared-cache.md)
- [Accepted Debian package construction on compatible workers](decisions/0045-debian-package-worker-custody.md)
- [Accepted project-scoped worktree lifecycle](decisions/0046-project-scoped-worktree-lifecycle.md)
- [Accepted native CLI Component acceptance](decisions/0047-native-cli-component-acceptance.md)
- [Accepted retained-project Standard lifecycle](decisions/0048-retained-project-standard-lifecycle.md)
- [Accepted repository succession](decisions/0049-repository-succession.md)
- [User-directed work loop](architecture/user-directed-work-loop.md)
- [Project release protocol](architecture/project-releases.md)
- [Skill architecture](architecture/skills.md)
- [Superpowers skills assessment](architecture/superpowers-skills-assessment.md)
- [Authoring and machine-record formats](architecture/authoring-and-record-formats.md)
- [Mission specifications, hierarchy, and readable authoring](architecture/mission-specification-composition.md)
- [Optional BEAM/ERTS live-coding layer investigation](architecture/beam-live-coding-layer-investigation.md)
- [Sample portfolio review](architecture/sample-portfolio-review.md)
- [Design traceability](architecture/design-traceability.md)
- [Framework premise and implementation assessment](architecture/framework-premise-assessment.md)
- [OVA model evaluation](architecture/ova-model-evaluation.md)
- [Framework boundary decision](decisions/0001-framework-boundary.md)
- [Reference ecosystem decision](decisions/0002-reference-implementation-ecosystem.md)
- [Invariant, policy, and delivery-claim classification](decisions/0003-constraint-classification.md)
- [Executable application-port boundaries](decisions/0004-executable-port-boundaries.md)
- [Executable Component semantics](decisions/0005-executable-component-semantics.md)
- [Significant feature request governance](decisions/0006-significant-feature-request-governance.md)
- [Update classification and semantic merge](decisions/0007-update-classification-and-semantic-merge.md)
- [Samples gate live-generation resilience](decisions/0008-samples-gate-live-generation-resilience.md)
- [Explicit lifecycle cache-root binding](decisions/0009-explicit-cache-root-binding.md)
- [Canonical Flavor naming migration](decisions/0010-canonical-flavor-naming-migration.md)
- [Typed Component domain structure](decisions/0011-component-domain-structure-literate-markdown-v2.md)
- [Sample portfolio matrix and committed source cache](decisions/0012-sample-portfolio-matrix-and-committed-source-cache.md)
- [Intent refinement and specification sufficiency](decisions/0013-intent-refinement-and-specification-sufficiency.md)
- [Zig language and C/C++ cross-compilation](decisions/0014-zig-language-and-c-cpp-cross-compilation.md)
- [Bounded CodeGraph evidence and exact-custody reuse](decisions/0015-bounded-codegraph-evidence-and-reuse.md)
- [CodeGraph is not a product dependency](decisions/0016-codegraph-not-a-product-dependency.md)
- [Live qualification pins one coding CLI and model](decisions/0017-explicit-live-test-coding-cli.md)
- [Never-empty per-agent model stack](decisions/0018-never-empty-per-agent-model-stack.md)
- [Opt-in external source intelligence](decisions/0019-opt-in-external-source-intelligence.md)
- [Paginated large Component-lock review](decisions/0020-paginated-component-lock-review.md)
- [Operator-local MCP catalog](decisions/0021-operator-local-mcp-catalog.md)
- [Shared author and recipient vocabulary](decisions/0022-channel-author-recipient-vocabulary.md)
- [Mutagenic CLI channel fan-out](decisions/0023-mutagenic-cli-channel-fan-out.md)
- [Project MCP catalog hygiene](decisions/0024-project-mcp-catalog-hygiene.md)
- [Operator-catalog MCP client](decisions/0025-cli-operator-mcp-client.md)
- [Multi-entrypoint deployment units](decisions/0026-multi-entrypoint-deployment-units.md)
- [Durable split-service pattern](decisions/0027-durable-split-service-pattern.md)
- [Browser interaction acceptance](decisions/0028-browser-interaction-acceptance.md)
- [Unified IPC-surface contract](decisions/0029-ipc-surface-contract.md)
- [Executable cross-layer authority](decisions/0030-executable-cross-layer-authority.md)
- [Durable user configuration home](decisions/0031-durable-user-configuration-home.md)
- [Tuple-specific native-install SBOMs](decisions/0032-tuple-specific-native-install-sboms.md)
- [Universal `os-base` toolchain](decisions/0033-os-base-universal-toolchain.md)
- [Continuous evidence-bound release closure](decisions/0034-continuous-evidence-bound-release-closure.md)
- [Explicit evidence-bound operator adoption](decisions/0035-explicit-operator-adoption-state.md)
- [Selection-bound npm dependency lifecycle](decisions/0036-selection-bound-npm-dependency-lifecycle.md)
- [Prefix-installed CLI self-update](decisions/0037-prefix-installed-cli-self-update.md)
- [Source trust and build-security plan](security/source-trust-and-build-policy.md)

## Migration and roadmap

- [Active work resumption queue](roadmap/active-work.md)
- [Prefix-installed CLI self-update](roadmap/prefix-cli-self-update.md)
- [Operator adoption program](roadmap/operator-adoption.md)
- [0.9.0 executable-authority program](roadmap/0.9.0-executable-authority.md)
- [Framework score-improvement program](roadmap/framework-score-improvement-program.md)
- [OVA two-phase rebase](migration/ova-two-phase-rebase.md)
- [Phase 1 release and rollback](migration/phase-1-release-and-rollback.md)
- [Phase 2 lifecycle bridge](migration/phase-2-lifecycle-bridge.md)
- [Phase 2 compatibility prerelease and rollback](migration/phase-2-compatibility-prerelease-and-rollback.md)
- [Phase 2 final cutover](migration/phase-2-final-cutover.md)
- [Phase 8 source-to-specification roadmap](roadmap/phase-8-source-to-specification.md)
- [Component authoring and lock separation roadmap](roadmap/component-authoring-and-locks.md)
- [Original-source native SDK consumption](roadmap/native-sdk-admission.md)

Completed programs live under `docs/history/roadmap/` with `historical`
lifecycle headers. Their owning queue items remain in
[active work](roadmap/active-work.md).

- [Release branching model](history/roadmap/release-branching-model.md)
- [Coverage-gap detection](history/roadmap/coverage-gap-detection.md)
- [0.7.0 release evidence closure](history/roadmap/0.7.0-release-evidence-closure.md)
- [SCXML specification provider design](history/roadmap/0.6.0-scxml-provider-design.md)
- [DMN specification provider design](history/roadmap/0.6.0-dmn-provider-design.md)
- [LOW-layer Design-by-Contract examples](history/roadmap/0.5.0-design-by-contract-examples/NOTES.md)
- [SCXML declared-trace spike results](history/roadmap/spikes/scxml-trace-prototype/RESULTS.md)
