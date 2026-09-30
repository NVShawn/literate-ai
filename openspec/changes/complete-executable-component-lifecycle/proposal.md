## Why

Literate AI has strong specification, identity, evidence, and guarded-execution contracts,
but its standard generation path still flattens a Component dependency graph into one
model context and one source tree. Higher-level application builders need every Component
to remain an independently planned, generated, built, tested, cached, linked, packaged,
and releasable unit without inventing another lifecycle outside the framework.

## What Changes

- **BREAKING**: separate authored Component intent from exact target/dependency locks and
  replace the pre-release single-tree execution shape with a versioned, topological
  per-Component execution plan. No implicit compatibility reader or dual authority is
  introduced for the superseded pre-release contracts.
- Define executable Component semantics around public capability interfaces, exact
  revisions, typed Flavor mixins, local specification refinement, and explicit lineage;
  generic behavioral inheritance remains outside the framework taxonomy.
- Generate each Component from its own bounded specifications, skills, Flavors, resources,
  and directly consumed public interfaces. Private transitive implementation context does
  not enter another Component's model request.
- Keep a normal Component's authored metadata and default behavioral specification in one
  canonical `component.md`. Additional specification or interface documents exist only
  for named domain, module, protocol, or independently consumed public boundaries; the
  lifecycle never recreates `component.json` or an `openspec/` peer tree as duplicate
  authoring authority.
- Add content-locked authored resources, arbitrary-byte source manifests, typed artifact
  exports/imports, build actions, link plans, package plans, and release manifests.
- Add public application services and one standard lifecycle that resolves, generates,
  builds, tests, accepts, links, packages, qualifies, and publishes a Component DAG in
  topological order with safe interruption and per-Component cache reuse.
- Represent applications and atomic application suites as root Components over shared
  exact Component dependencies; expose programmatic services suitable for graphical IDEs
  without depending on OVA, a UI framework, a model provider, or a build system.
- Make target packaging truthful: a standalone executable is one target/Flavor outcome,
  while runtime bundles, archives, installers, images, and other qualified artifacts remain
  valid explicit outcomes.
- Retain source-to-specification, stage-specific model routing, generated tests,
  independent acceptance, SBOM continuity, publication separation, and exact receipts as
  framework-owned lifecycle inputs and evidence.

## Implementation Status

Implemented core slices:

- authored Component/lock separation, public capability interfaces, exact per-Component
  execution plans, bounded direct-interface contexts, and complexity budgets;
- arbitrary-byte source/resource contracts, typed artifact exports, build manifests,
  materialization, composite build requests, and exact link-plan contracts;
- the source-only generation runner/scheduler boundary with complete prepared nodes,
  tree/bundle-distinct candidates, provenance, bounded parallel layers, and accepted-source
  reuse; and
- the reusable Standard lifecycle core from source generation/reuse through current
  indexing and authorization, plan finalization, dependency-safe build, node test,
  execution, acceptance, project admission, and receipt issuance.

Still open in this change:

- `CACHE-250` per-Component cache membership and publication;
- package/release adapters, release qualification, publication/import, and deployment;
- durable stage-boundary interruption, process-restart resume, and retry lineage;
- public application-service completion plus ordinary CLI/default-driver integration; and
- migration of ordinary samples and source-promotion qualification onto the Standard
  lifecycle using the canonical `component.md`-first authoring form. The in-repository
  diamond is lifecycle conformance evidence, not completion of those adoption tasks.

## Capabilities

### New Capabilities

- `component-authoring-locks`: Separates portable authored Component intent from exact
  dependency, target, Flavor, toolchain, and resource resolution state.
- `executable-component-composition`: Defines Components as independent executable
  lifecycle nodes connected only through versioned public capability interfaces.
- `bounded-component-generation`: Defines per-Component generation plans, direct-interface
  context bounds, invalidation, tests, acceptance, and derivation identities.
- `component-artifact-graph`: Defines authored resources, source manifests, typed artifact
  exports/imports, build actions, linking, packaging, and aggregate artifact provenance.
- `standard-project-lifecycle`: Defines the public topological lifecycle, interruption,
  resume, per-Component cache reuse, aggregate evidence, and CLI delegation contract.
- `application-project-services`: Defines software-neutral services for planning, building,
  inspecting, and releasing root application Components and shared Component graphs.
- `release-qualification`: Defines target-specific release manifests, package kinds,
  publication/import, deployment readiness, and current receipt requirements.

### Modified Capabilities

None. The existing bootstrap change has not been archived into canonical capability specs;
these narrower capabilities state the executable-composition behavior explicitly without
silently rewriting that historical change.

## Impact

- Affects v2 schemas, Component/project contracts, execution plans, recipes, source and
  artifact manifests, builders, linkers, packagers, caches, workspaces, SBOMs, receipts,
  publications, public application services, CLI adapters, samples, and documentation.
- Replaces project-specific lifecycle-driver orchestration for ordinary projects with a
  standard framework service; content-pinned external drivers remain an advanced policy
  option rather than the default lifecycle implementation.
- Requires migration characterization for pre-release wire shapes and exact invalidation
  tests, but does not add runtime compatibility or OVA-specific behavior to the core.
- Enables OVA and other application adapters to manage specifications and resources while
  consuming one provider-neutral generation and release authority.
