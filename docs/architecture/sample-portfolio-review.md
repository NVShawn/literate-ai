# Sample portfolio review

The sample catalog has two jobs. It is executable conformance evidence for Literate AI,
and it is the first design library most people will read, fork, and adapt. A technically
valuable torture case may remain in the catalog without pretending to be a product, but
the first screen must lead with Components that solve recognizable problems.

This review uses four criteria:

1. **Correctness surface** — bounded inputs, explicit invalid cases, deterministic
   ordering and arithmetic, a closed output contract, and independently known results.
2. **Composition value** — a public capability or boundary that can sensibly participate
   in a larger application.
3. **Adaptation value** — behavior a reader might preserve while changing vocabulary,
   interfaces, or selected Flavors.
4. **Framework coverage** — a lifecycle, language, topology, cache, publication, or
   inverse-generation path that needs executable regression evidence.

The value score is about likely reuse, not implementation difficulty. The test score is
about architectural coverage, not product popularity.

| Sample | Portfolio role | Reuse value | Test value | Review |
| --- | --- | ---: | ---: | --- |
| Greeting Card Starter | start-here tutorial | 2/5 | 4/5 | Correct and intentionally small. Good first lifecycle, normalization, aggregation, and known-output example; not presented as a production architecture. |
| Loan Risk Gate | specification-provider sample (DMN + assets) | 4/5 | 5/5 | Genuine UNIQUE loan-risk table plus pinned income-band JSON and an explicit portable public interface. Proves `specification_provider: dmn` and MEDIUM-layer `assets:` without inventing a DMN engine. |
| Playback Controller | specification-provider sample (SCXML) | 4/5 | 5/5 | Parallel transport/audio regions with deep history plus an explicit portable public interface. Proves `specification_provider: scxml` and a production `.trace.json` sidecar. |
| Containerized Access-Log Tally | deployment-axis matrix sample | 3/5 | 4/5 | Owns the deploy-docker + container-assembly cell and demonstrates the specification hierarchy (interfaces/spec.md wire boundary; image rules live wholly in the Flavor and its skill). Not yet wired into the live ladder pending authenticated generation. |
| Critical Path Scheduler | reusable planning engine | 5/5 | 5/5 | Strong real algorithm with validation, integer bounds, stable tie-breaking, full earliest/latest schedule, slack, and useful project/workflow/build applications. |
| Build Pipeline Dependency Planner | reusable Rust planning kernel | 4/5 | 5/5 | Correct smaller DAG/topology/longest-chain boundary. It overlaps the scheduler deliberately to prove a Rust-only recipe and a simpler build-pipeline contract. |
| Content-Addressed Record Vault | reusable storage kernel | 4/5 | 5/5 | Deduplication, SHA-256 identity, ordered recovery, empty-cache fill, and restart behavior are useful in artifact stores and offline queues. It is not a distributed CAS. |
| Portable Deployment Matrix | reusable release-planning kernel | 4/5 | 5/5 | Recognizable CI/release function and the clearest independent Flavor-axis example. Artifact/toolchain mappings are intentionally small, not a universal platform database. |
| Full-Stack Rust and JavaScript Release Dashboard | reusable full-stack pattern | 5/5 | 5/5 | Compelling two-role application with explicit protocol ownership, integer risk calculation, frontend view model, and trusted orchestration boundary. |
| Exact Statistics Library | reusable library + consumer | 4/5 | 5/5 | Exact rational statistics are broadly useful and demonstrate independently generated library consumption. The statistical surface is intentionally basic. |
| JavaScript Ledger Workbench | reusable finance kernel | 5/5 | 5/5 | Strong integer-money, reconciliation, budget, ordering, and deterministic tie-breaking example with obvious personal-finance and expense-control forks. |
| Private AI Endpoint Router | reusable AI infrastructure | 4/5 | 4/5 | Valuable privacy and capability-routing primitive with explainable fallback. It models selection, not health probing, load balancing, credentials, or remote egress policy. |
| Multi-Role Text Job Pipeline | compositional topology example | 3/5 | 5/5 | The API/worker source-role boundary is useful; word counts are deliberately simple so the topology remains visible. Fork this shape, not its toy workload. |
| Reproducible Release Manifest | reusable supply-chain primitive | 5/5 | 5/5 | Canonical path ordering, per-file hashes, and one release identity are broadly applicable to plugins, sites, configuration, and asset bundles. |
| Warehouse Manifest Round-trip | reusable fulfillment kernel + semantic oracle | 4/5 | 5/5 | Rich normalization, aggregation, integer discounting, ranking, and stable identity make it credible product logic and the best forward/inverse comparison fixture. |
| Deployment Security Gate | reusable policy kernel | 4/5 | 5/5 | Explicit blocked/constrained/maximum-privilege decisions and persistent override warning are useful admission-policy behavior. It is not a vulnerability scanner. |
| Framework Compatibility Readiness | framework conformance only | 1/5 | 5/5 | Correctly proves repository-independent version/skill readiness and authority separation. Keep it in the regression catalog, but do not recommend it as an end-user starting point. |
| Composable Invoice Service | reusable Component graph | 5/5 | 5/5 | Best small Component-composition example: application, service, and integer-money library have distinct ownership and public capability edges. |
| Linux Cgroup Budget Interpreter | reusable container resource boundary | 4/5 | 5/5 | Real cgroup v2 semantics, native C++, Conan selection, and an exact Linux-only scheduling boundary. It parses supplied kernel values rather than claiming live host inspection. |
| Windows Path Auditor | reusable installer/workspace boundary | 4/5 | 5/5 | Drive, UNC, rooted, and relative path behavior is recognizable Windows infrastructure work and proves Windows-only native C++ plus Conan selection. |
| macOS LaunchAgent Catalog | reusable desktop-service boundary | 4/5 | 5/5 | A concrete LaunchAgent catalog in Swift proves the Apple toolchain, macOS-only scheduling, and Homebrew Flavor composition without pretending to install a service. |
| CUDA Vector Transform | reusable NVIDIA C++ GPU primitive | 4/5 | 5/5 | Exact affine transform on device memory with a CPU oracle, proving generate-nvidia-cuda-application and nvcc selection on a healthy NVIDIA worker. |
| CUDA Matrix Product | reusable NVIDIA Python GPU primitive | 4/5 | 5/5 | Exact CuPy matrix product on the selected CUDA device, proving the Python CUDA stack preflight and the same accelerator Flavor path. |
| Cluster Health Service | reusable persistent-service pattern | 4/5 | 4/5 | Paginated HTTP, typed 404, MCP tool, and scheduled worker over SQLite. Sample-host JSON probe is the harness surface; live generation passed 2026-08-29 (`cursor-agent` / `gpt-5.6-sol-high`). |
| Cluster Metrics Dashboard | reusable web-application pattern | 4/5 | 4/5 | Series selection independent of fetch, graph/table switch, and multi-key sort. Pinned to JavaScript; harness admits `web-application`. |
| Durable Snapshot Dashboard | durable four-boundary portfolio | 5/5 | 5/5 | Two ordinary root locks preserve frontend → read-only API → SQLite cache and collector → write-only cache boundaries. The executed flow proves restart-independent reads, atomic partial-failure handling, bounded retries, single-winner leases, expiry recovery, and shared freshness without flattening the application into one prompt. |

## Human readability standard

Every top-level sample now opens with a domain story and a diagram before its normative
requirements. The prose explains why someone would reuse the Component; the requirement
and scenario blocks retain exact behavior; the diagram shows the transformation or
Component boundary without restating framework mechanics. Component manifests are
readable Markdown frontmatter, so coordinates, capabilities, skills, Flavors, and
acceptance intent remain reviewable beside the behavioral prose.

Conformance enforces those reader-facing properties for every catalog entry. A new
sample cannot silently regress to a mechanism-only title, omit its portfolio role, drop
its diagram, or minify a Component manifest. Domain-specific guidance stays in the spec;
portable generation, build, testing, and composition guidance stays in skills.

## Correctness conclusion

All twenty-one applications define complete recipes for runnable artifacts. Twenty use
two pinned verifier cases plus a third post-build entropy-derived case during E2E
execution. The durable portfolio instead owns a stateful verifier across its four
independently built process boundaries, including restart and failure transitions that
cannot be represented by one entrypoint invocation. Public generation recipes do not
contain expected results. The verifier binds the exact acceptance interfaces and runs
the same built artifacts used by generated
current-state tests. The behavior contracts use integer arithmetic where financial or
scheduling precision matters and specify ordering or tie-breaking wherever map, graph,
or set order could leak into output. The repository conformance suite independently
checks that every sample remains a strict spec-driven application with no checked-in
generated source.

That is a strong executable correctness boundary. It is not a proof that every possible
generated implementation is defect-free, and the intentionally small Components do not
claim production concerns absent from their specs. In particular, the record vault is
not distributed storage, the endpoint router is not a service mesh, and the security
gate is not a scanner.

## Portfolio conclusion

The original catalog was better as a conformance ladder than as a product-design shelf.
Several directory names described the framework mechanism under test—“empty cache,”
“flavor matrix,” “publication import,” or “multi repository”—before saying what the
generated application did. The underlying behaviors were more useful than those names
made them appear. The current display names, descriptions, program stories, and diagrams
lead with the reusable problem while preserving stable sample coordinates and every
test boundary.

New users should start with Greeting Card Starter, then Reproducible Release Manifest,
Composable Invoice Service, and one richer application such as the Ledger Workbench or
Release Dashboard. Decision tables and state charts start at Loan Risk Gate and Playback
Controller. Framework Compatibility Readiness belongs at the end of the catalog.

## Important gaps

The portfolio still overrepresents deterministic JSON calculators because they are
portable, cheap to regenerate, and easy to verify independently. Popular software is
also built from stateful and interactive boundaries. The next high-value samples should
add, in this order:

1. a durable job queue with idempotency keys and queued-work semantics beyond the
   snapshot collector's bounded lease/retry proof;
2. a schema-driven configuration loader with layered overrides and secret references;
3. a text ingestion, indexing, and search pipeline; and
4. authentication/authorization policy with auditable decisions.

Each should remain portable and independently verifiable. They should not flatten an
entire product into one Component merely to look impressive: public contracts and small
composable capability boundaries are part of the lesson.
