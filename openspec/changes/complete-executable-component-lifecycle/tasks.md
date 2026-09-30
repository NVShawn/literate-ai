## 1. Phase 1 — Executable Component Framework

- [ ] 1.1 Integration owner checkpoints the current project-v2, cache-v2, source-intelligence, source-translation, compatibility-schema, and documentation working tree; register every schema identity and pass `make validate`, `make wheel-check`, and native project validation from a clean tree.
- [x] 1.2 Contract lane records the `COMP-190` executable-Component ADR and adds conformance tests that keep revision lineage, spec-node refinement, typed Flavor contributions, and capability composition distinct.
- [x] 1.3 Contract lane defines versioned public capability interface contracts with retrievable callable/artifact surfaces, compatibility, re-exports, exact identities, schemas, invalid fixtures, and registry validation.
- [x] 1.4 Contract lane implements authored Component intent separated from exact target/dependency lock state, including schema classification and rejection of resolved or host-specific fields in authored authority.
- [x] 1.5 Contract lane implements deterministic lock creation, check, and semantic diff services/CLI with byte-stability, stale/forged-lock, unselected-catalog, ambiguity, and atomic-write tests.
- [ ] 1.6 Source-authoring lane enriches source-to-specification nodes and promotion with reviewed node kinds, entrypoints, public interfaces, resource ownership, target intent, and acceptance boundaries; emit one canonical `component.md` per normal promoted Component, retain only justified named boundary documents, and add regressions for retired peer authoring files, fabricated application entrypoints, and null contracts.
- [x] 1.7 Planning lane defines versioned Component action/generation plans, project execution plans, named targets, node derivation keys, and deterministic topological layers with schema and canonical-hash tests.
- [x] 1.8 Planning lane replaces flattened dependency-document recipes with one Component's effective local authority plus direct public interfaces and adds diamond, cycle, ambiguity, and catalog-order conformance tests.
- [x] 1.9 Planning lane adds context manifests and byte/token/complexity budgets with private-transitive canaries, wide-fan-in rejection, visibility, route, timing, token, and cost evidence tests.
- [ ] 1.10 Application-service lane extracts catalog resolution, lock resolution, recipe construction, planning, and authority review from CLI-private helpers and adds dependency-direction tests preventing service or sample imports from CLI modules.
- [x] 1.11a Resource lane defines arbitrary-byte authored resource and source-tree manifests, logical-path and ownership rules, generated-text collision rejection, schemas, ports, and CAS adapter behavior.
- [ ] 1.11b Resource lane proves the arbitrary-byte resource path with macOS/Linux/Windows byte-roundtrip tests.
- [x] 1.12a Artifact lane defines typed artifact exports/imports, Component build manifests, build action requests, exact link plans, schemas, and wrong-role/ABI/target/producer/media/digest tests.
- [x] 1.12b Artifact lane defines and implements package plans and release artifact sets.
- [ ] 1.13 Builder lane updates guarded language builders to consume accepted dependency artifact imports and emit typed exports while preserving builder authorization, sandbox, auxiliary-artifact, and SBOM evidence.
- [ ] 1.14 Link/package lane implements provider-neutral linking and package-kind adapters, proving C++/Rust executable and Python/JavaScript runtime-bundle closures without claiming a universal binary format.
- [ ] 1.15 SBOM lane extends managed source, resolved, build, link, package, and release graphs and tests complete Component/resource/toolchain/runtime continuity through a diamond root.
- [x] 1.16a Lifecycle lane implements the public `StandardProjectLifecycleService` core over complete prepared Component nodes: source-only generation/reuse, indexing, current authorization, exact dependency-artifact build order, node tests, execution, acceptance, project admission, and receipt issuance.
- [ ] 1.16b Lifecycle lane integrates lock/cache acquisition, linking, packaging, and integration/release acceptance into the same service.
- [x] 1.17a Lifecycle lane adds in-process source resume, dependent cancellation, parallel-layer failure isolation, current reindex/reauthorization, and no-consumer-before-accepted-dependency tests.
- [ ] 1.17b Lifecycle lane adds durable identity-bound interruption at every stage, retry lineage, and current-receipt preservation across process restart.
- [ ] 1.18 Cache lane converts generation/source/build/artifact membership to per-Component actions and covers cold, leaf-hit/root-miss, root-hit, corrupt, ambiguous, wrong-target, forced bypass, omitted/extra membership, and empty-cache runs.
- [ ] 1.19 Release lane implements exact release variants, manifests, readiness evaluation, package-result and optional publication/deployment receipt membership, truthful standalone status, and tamper/stale/revoked/wrong-target rejection.
- [ ] 1.20 Publication lane extends immutable publish/import to reconstruct and verify a complete release closure across restart while keeping local qualified readiness independent from publication.
- [x] 1.21a Integration owner registers the implemented executable, source-generation, artifact, and Standard post-source lifecycle public contracts and schemas.
- [ ] 1.21b Integration owner registers project-template assets, default Standard lifecycle/receipt bindings, and CLI adapters; prove CLI and direct service callers receive identity-equivalent plans, events, results, and receipts.

## 2. Phase 2 — Application Services, Proof, and Downstream Checkpoint

- [ ] 2.1 Application-service lane implements structured create/open, canonical `component.md` authoring, specification derive/review/promote, validate, lock, plan, rebuild, release, status/events, cancellation, and artifact/release retrieval APIs without OVA, UI, CLI-private dependencies, or duplicate peer specification authority.
- [ ] 2.2 Application-service lane represents applications as root Components and release-significant atomic suites as distribution Components while leaving cosmetic organization outside framework authority.
- [ ] 2.3 Source-authoring lane connects promotion to the public project services and proves reviewed multi-language source creates canonical multi-Component projects with one `component.md` per normal Component whose source authority changes only after qualification.
- [ ] 2.4 Sample lane replaces sample-specific lifecycle orchestration with a standard-lifecycle diamond `root -> {left,right} -> shared` and independent oracles; verify the shared node generates/builds once.
- [ ] 2.5 Sample lane adds a multi-root project sharing one Component and proves exact reuse plus isolated invalidation when only one root changes.
- [ ] 2.6 Sample lane adds binary-resource and package-kind fixtures proving locked asset collision rejection, release-relative reconstruction, and exact runtime closure.
- [ ] 2.7 Sample lane makes a freshly initialized project lock, plan, rebuild, test, package, and report status using the installed standard lifecycle without an external sample driver.
- [ ] 2.8 Qualification lane runs the existing source-promotion qualification through the same standard lifecycle and rejects historical, command-counted, provider-asserted, or differently scoped evidence as current release authority.
- [ ] 2.9 Compatibility lane adds explicit characterization/migration tests for welded pre-release manifests, flattened cache records, historical receipts, fabricated source-promotion entrypoints, and frozen schema URIs without silent aliasing.
- [ ] 2.10 Security/test lane adds property, fuzz, mutation, path/resource, interface-confusion, cache-poisoning, receipt-membership, and hostile-artifact cases before enabling the standard lifecycle by default.
- [ ] 2.11 Portability lane runs schema, unit, conformance, wheel, initialized-project, diamond, multi-root, binary-resource, package, and publication/import matrices on supported macOS, Linux, and Windows targets.
- [ ] 2.12 Documentation lane updates `SKILL.md`, project templates, architecture, concepts, single-file Component authoring, lock/context/resource/build/cache/release/source-intake guides, traceability, and the five-command golden path to match implemented contracts.
- [ ] 2.13 Integration owner removes ordinary-project dependence on project-specific external drivers and duplicate sample orchestration only after service, compatibility, and rollback gates pass.
- [ ] 2.14 Integration owner produces a clean tagged/checkpointed upstream tree identity with all gates passing and publishes the exact source/status evidence required for OVA embedding.
- [ ] 2.15 Integration owner coordinates three-worker review lanes for authority/security, API/adapter boundaries, and documentation/conformance, resolves findings, and records a concise downstream OVA handoff.
