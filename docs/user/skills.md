# What does Literate AI have skills for?

Literate AI has skills for software development, source analysis, build and release
engineering, host preparation, document and presentation generation, instructional
videos, and operator integrations. Start with the task map below; the complete
inventory follows it.

This inventory covers the **88 canonical `SKILL.md` files under `skills/`** in the
1.4.0 development tree reviewed on 2026-10-31: **39 agent**, **38
specification-to-source**, and **11 source-to-specification** skills. A catalog entry
means guidance exists, not that every target or workflow has passed native acceptance.
See [current features](current-features.md) for release posture and
[skill architecture](../architecture/skills.md) for enforcement details.

## Choose by task

| I want to… | Start here | What it supplies |
| --- | --- | --- |
| Adopt or develop a project | [Project development](#project-development-and-governance) | Requirements, work tracking, conversion, development posture and review flow |
| Generate application code | [Language generation](#portable-language-generation) and [application patterns](#applications-services-browser-ui-and-protocols) | Portable language technique, backend/frontend, REST, gRPC and MCP |
| Understand an existing codebase | [Source analysis](#source-to-specification-analysis) | Evidence-backed proposals for architecture, APIs, behavior, operations and language Flavors |
| Build software or containers | [Build systems](#build-systems-containers-and-acceleration) | Bazel, Cargo, CMake, Make, Mix, repo_man, Docker and CUDA technique |
| Prepare Linux, macOS or Windows hosts | [Workers and toolchains](#workers-os-preparation-and-toolchains) | Detection, supported bootstrap, alignment and toolchain checks |
| Package or deploy a release | [Packaging and release](#testing-packaging-and-release-operations) and [OS boundaries](#os-specific-packaging-and-deployment-boundaries) | Accepted-artifact packaging and explicit publication; target support belongs to selected Flavors/providers |
| Make slides, documents or courses | [Artifact authoring](#documents-presentations-video-and-visual-inspection) and [document-pair recipe](#document-generation-recipe) | Editable artifacts, regeneration, rendered QA and selected-ecosystem publication |
| Connect operator systems | [Integrations](#operator-and-external-system-integration) | MCP catalog, Jira, inbound work and opt-in external runner contract |

## How to use these skills

For agent work, begin at the repository's declared onboarding skill (normally
[root `SKILL.md`](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/SKILL.md)). It routes to the
[agent parent](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/agent/SKILL.md) and the relevant child. Read every
ancestor on that child's path; nested children add deltas rather than repeat rules.
The catalog parent is included in the count, so 39 does not mean 39 independent
end-user workflows.

Generation skills enter a recipe through exact Component/Flavor pins. They are
technique, not independently executable commands. Use `litai plan COMPONENT ...`
to inspect the selected recipe before authorized generation. Inverse skills run
through the source-to-specification workflow; their observations become intent only
after review. `litai project validate` checks the catalog graph.

These are repository capabilities, separate from skills installed in a particular
coding assistant. Installing a presentation plugin does not add it to this catalog.
The root onboarding skill is an additional entry point outside the 88. Packaged
initialization templates are distribution copies, not extra capabilities; Flavors,
Components, workflows and routing policies are separate catalog kinds. A derived
project may inherit a different effective inventory through its declared parents.

## OS-specific packaging and deployment boundaries

There is no separate `deploy-linux`, `deploy-macos` or `deploy-windows` skill in
this tree. Host preparation and deployment concerns compose across agent skills,
OS/toolchain/packaging Flavors, and deterministic providers:

| Target | Relevant guidance | Boundary |
| --- | --- | --- |
| Linux | Host bootstrap/toolchain skills; package-artifacts documents apt, pip, Conan and ZIP compatibility | Native tools, selected packaging Flavor and accepted artifacts remain required |
| macOS | Same host skills; Swift prerequisite; package-artifacts documents Homebrew, pip, Conan and ZIP compatibility | Signing, installation and service registration are project/provider concerns; the skill is not a universal macOS installer |
| Windows | Same host skills; Swift prerequisite; package-artifacts documents WinGet, Chocolatey, pip, Conan and ZIP compatibility | Windows toolchain/runtime identities and native evidence still matter |
| Containers / NVIDIA | Docker generation; stack selection plus CUDA generation | Container or CUDA guidance alone does not prove deployment or hardware qualification |
| FreeBSD | See [PROJECT.md](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/PROJECT.md) and the active FREEBSD-001 program | Native platform/pkg qualification remains separately tracked; do not infer complete support from a generic POSIX skill |

This table describes the compatibility guidance in the skills, not a complete
provider support matrix. Consult selected Flavors and [packaging/publication](caches-packages-publication.md)
for the actual lifecycle. Publication and live installation retain their authorization
boundaries.

For documents, `author-presentations-and-documents` supplies the operator workflow;
`document-pair` supplies generation technique; `doc-google-workspace` and
`doc-microsoft-365` supply ecosystem bindings. The generalized Rich HTML5 presentation
skill remains open in PROJECT.md: existing HTML decks and HTML observability do not
establish that broader capability. There are no dedicated general-purpose PDF,
spreadsheet or image-editing skills in this canonical tree.

## Complete inventory

Each row links to its canonical file at the reviewed source revision. Source links
use GitHub because skill files are outside the declared documentation roots.
Categories below assign each entry once;
a skill can still contribute to several tasks in the task map.

### Project development and governance

| Skill | Purpose |
| --- | --- |
| [agent](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/agent/SKILL.md) | Shared agent rules; read ancestor skills and delegate deterministic work to commands. |
| [prompt-master](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/agent/prompt-master/SKILL.md) | Translate a direct request into a scoped, testable task. |
| [track-project-requirements](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/agent/track-project-requirements/SKILL.md) | Maintain PROJECT.md goals and completeness. |
| [record-user-directed-work](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/agent/record-user-directed-work/SKILL.md) | Record work, acceptance evidence and changelog outcomes. |
| [convert-project](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/agent/convert-project/SKILL.md) | Adopt an existing source tree with recovery and baseline evidence. |
| [maintain-parent-readme](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/agent/maintain-parent-readme/SKILL.md) | Reconcile README links with declared repository parents. |
| [develop-in-production-workflow](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/agent/develop-in-production-workflow/SKILL.md) | Verify each production change before landing. |
| [develop-in-production-workflow/staging](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/agent/develop-in-production-workflow/staging/SKILL.md) | Consolidate a locally verified batch before full remote CI. |
| [develop-in-production-workflow/staging/dev](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/agent/develop-in-production-workflow/staging/dev/SKILL.md) | Iterate locally within inherited development gates. |
| [develop-in-production-workflow/staging/dev/survey-peer-work](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/agent/develop-in-production-workflow/staging/dev/survey-peer-work/SKILL.md) | Survey issues, reviews, branches and worktrees. |
| [develop-in-production-workflow/staging/dev/land](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/agent/develop-in-production-workflow/staging/dev/land/SKILL.md) | Land verified work through the declared forge review flow. |

### Documents, presentations, video and visual inspection

| Skill | Purpose |
| --- | --- |
| [author-presentations-and-documents](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/agent/author-presentations-and-documents/SKILL.md) | Author and regenerate project decks and documents, including PowerPoint, Word, Google Slides and Google Docs; render and inspect before delivery. |
| [author-instructional-videos](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/agent/author-instructional-videos/SKILL.md) | Build and verify narrated course packages with captions and retained authoring inputs. |
| [render-html-observability](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/agent/render-html-observability/SKILL.md) | Render declared single-file HTML authority graphs and verify their provenance. |
| [verify-frontend-browser](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/agent/verify-frontend-browser/SKILL.md) | Inspect rendered frontend behavior after execution is authorized. |

### Workers, OS preparation and toolchains

| Skill | Purpose |
| --- | --- |
| [configure-test-workers](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/agent/configure-test-workers/SKILL.md) | Configure private static or dynamic workers without committing machine identities. |
| [align-workers](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/agent/align-workers/SKILL.md) | Compare configured workers to declared requirements and use supported remediation. |
| [worker-health](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/agent/worker-health/SKILL.md) | Inspect worker resource pressure under its private policy. |
| [detect-before-install](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/agent/detect-before-install/SKILL.md) | Detect and bootstrap missing host prerequisites on Linux, macOS and Windows. |
| [checkout-git-repository](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/agent/checkout-git-repository/SKILL.md) | Acquire Git repositories with submodules and Git LFS hydration checks. |
| [preflight-host-toolchains](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/agent/preflight-host-toolchains/SKILL.md) | Resolve selected tools and exact pins without silently installing alternatives. |
| [swift-toolchain-prerequisite](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/agent/swift-toolchain-prerequisite/SKILL.md) | Check the selected Swift toolchain before generation. |
| [select-nvidia-accelerated-stack](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/agent/select-nvidia-accelerated-stack/SKILL.md) | Select a compatible NVIDIA stack using current official evidence. |

### Testing, packaging and release operations

| Skill | Purpose |
| --- | --- |
| [bind-lifecycle-evidence](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/agent/bind-lifecycle-evidence/SKILL.md) | Check identities already bound by the lifecycle and its receipts/SBOMs. |
| [ci-test-plan](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/agent/ci-test-plan/SKILL.md) | Detect supported test planning and compose impact selection with sharding. |
| [ci-test-plan/impact](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/agent/ci-test-plan/impact/SKILL.md) | Wire trusted impact selection with full-suite fallback. |
| [ci-test-plan/shard](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/agent/ci-test-plan/shard/SKILL.md) | Wire supported test partitions into a matrix. |
| [package-artifacts](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/agent/package-artifacts/SKILL.md) | Plan, build and independently verify packages from accepted products. |
| [release-project](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/agent/release-project/SKILL.md) | Plan, prepare, check and explicitly publish a versioned release. |
| [release-project/advance](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/agent/release-project/advance/SKILL.md) | Advance declared default-branch version files. |
| [release-project/backport](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/agent/release-project/backport/SKILL.md) | Backport through the declared release policy. |
| [release-project/ci-status](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/agent/release-project/ci-status/SKILL.md) | Inspect hosted CI through tracker-provided commands. |
| [release-project/evidence](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/agent/release-project/evidence/SKILL.md) | Inspect release gate evidence without replaying expensive work. |
| [release-project/notify-descendants](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/agent/release-project/notify-descendants/SKILL.md) | Notify operator-named descendants when authorized. |
| [release-project/verify-published](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/agent/release-project/verify-published/SKILL.md) | Read back published tags, branches, provider state and collateral. |

### Operator and external-system integration

| Skill | Purpose |
| --- | --- |
| [configure-operator-mcp](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/agent/configure-operator-mcp/SKILL.md) | Discover connected MCPs and maintain the operator-local catalog. |
| [associate-release-jira](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/agent/associate-release-jira/SKILL.md) | Associate a release with Jira when the connected integration applies. |
| [ingest-channel-work](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/agent/ingest-channel-work/SKILL.md) | Admit recipient-addressed channel work into a queue draft. |
| [write-mac-project-contract](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/agent/write-mac-project-contract/SKILL.md) | Project opt-in MAC/OpenShell runner metadata from an exact BOM; MAC is a runner system, not macOS. |

### Generation foundations and repository layout

| Skill | Purpose |
| --- | --- |
| [portable-specification-planning](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/specification-to-source/portable-specification-planning/SKILL.md) | Plan from exact specifications, ancestors and public contracts. |
| [portable-application-implementation](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/specification-to-source/portable-application-implementation/SKILL.md) | Implement the approved plan, current generated tests and required dependency evidence. |
| [repository-layout](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/specification-to-source/repository-layout/SKILL.md) | Separate authored authority, implementation, tests and disposable output. |
| [python-repository-layout](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/specification-to-source/python-repository-layout/SKILL.md) | Use Python src-layout packages and declared console entrypoints. |
| [cpp-ecosystem](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/specification-to-source/cpp-ecosystem/SKILL.md) | Organize C++ public headers, implementation, tests and generated output. |
| [rust-ecosystem](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/specification-to-source/rust-ecosystem/SKILL.md) | Use conventional Cargo package and target layout. |
| [javascript-ecosystem](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/specification-to-source/javascript-ecosystem/SKILL.md) | Apply the selected npm packaging technique with exact dependency inputs. |
| [debug-spec-map](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/specification-to-source/debug-spec-map/SKILL.md) | Add specification source anchors when debug instrumentation is requested. |

### Portable language generation

| Skill | Purpose |
| --- | --- |
| [cpp17-portable-json-application](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/specification-to-source/cpp17-portable-json-application/SKILL.md) | Generate portable C++17 JSON-argument applications and native test entrypoints. |
| [elixir-portable-application](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/specification-to-source/elixir-portable-application/SKILL.md) | Generate portable Elixir/OTP applications and runtime tests. |
| [go-portable-application](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/specification-to-source/go-portable-application/SKILL.md) | Generate standard-library Go applications with a direct build path. |
| [javascript-portable-json-application](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/specification-to-source/javascript-portable-json-application/SKILL.md) | Generate Node.js JavaScript applications using admitted dependencies. |
| [python-portable-application](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/specification-to-source/python-portable-application/SKILL.md) | Generate Python applications with standard-library or admitted native SDK imports. |
| [rust-portable-json-application](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/specification-to-source/rust-portable-json-application/SKILL.md) | Generate Rust applications using direct rustc or the selected Cargo build. |
| [swift-portable-json-application](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/specification-to-source/swift-portable-json-application/SKILL.md) | Generate Swift applications and native runtime tests. |
| [typescript-portable-application](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/specification-to-source/typescript-portable-application/SKILL.md) | Generate Node.js TypeScript applications and native runtime tests. |
| [zig-portable-application](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/specification-to-source/zig-portable-application/SKILL.md) | Generate portable Zig applications and native runtime tests. |

### Build systems, containers and acceleration

| Skill | Purpose |
| --- | --- |
| [bazel-build-system](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/specification-to-source/bazel-build-system/SKILL.md) | Apply the removable Bazel technique and supported ruleset conventions. |
| [cargo-build-system](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/specification-to-source/cargo-build-system/SKILL.md) | Generate the selected Cargo package/build shape. |
| [cmake-build-system](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/specification-to-source/cmake-build-system/SKILL.md) | Apply the removable CMake technique. |
| [make-build-system](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/specification-to-source/make-build-system/SKILL.md) | Apply the removable Make technique. |
| [mix-build-system](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/specification-to-source/mix-build-system/SKILL.md) | Generate Mix project metadata and checksum-pinned dependency inputs. |
| [repo-man-build-system](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/specification-to-source/repo-man-build-system/SKILL.md) | Preserve repository-owned repo_man platform wrapper entrypoints. |
| [docker-container-application](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/specification-to-source/docker-container-application/SKILL.md) | Assemble a Dockerfile from specifications, selected Flavors and technique preferences. |
| [generate-nvidia-cuda-application](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/specification-to-source/generate-nvidia-cuda-application/SKILL.md) | Generate against the selected, locked NVIDIA/CUDA stack. |

### Applications, services, browser UI and protocols

| Skill | Purpose |
| --- | --- |
| [backend-application](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/specification-to-source/backend-application/SKILL.md) | Generate a cooperating backend process with shared code and local store. |
| [backend-application/durable-split-service](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/specification-to-source/backend-application/durable-split-service/SKILL.md) | Add restart-safe collection and availability boundaries for an expensive upstream. |
| [backend-application/grpc-application](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/specification-to-source/backend-application/grpc-application/SKILL.md) | Add a typed gRPC/protobuf service boundary. |
| [backend-application/rest-application](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/specification-to-source/backend-application/rest-application/SKILL.md) | Add a REST/HTTP+JSON service boundary. |
| [backend-application/python-service-application](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/specification-to-source/backend-application/python-service-application/SKILL.md) | Add Python backend and MCP implementation technique. |
| [backend-application/rust-service-application](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/specification-to-source/backend-application/rust-service-application/SKILL.md) | Add Rust backend and MCP implementation technique. |
| [backend-application/scheduler-lease-worker](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/specification-to-source/backend-application/scheduler-lease-worker/SKILL.md) | Add scheduled work with lease-based duplicate prevention. |
| [frontend-application](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/specification-to-source/frontend-application/SKILL.md) | Generate the shared browser-facing frontend. |
| [frontend-application/react-application](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/specification-to-source/frontend-application/react-application/SKILL.md) | Add React UI technique when selected. |
| [frontend-application/react-dashboard-application](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/specification-to-source/frontend-application/react-dashboard-application/SKILL.md) | Add a tabular/time-series dashboard over one dataset. |
| [mcp-application](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/specification-to-source/mcp-application/SKILL.md) | Generate MCP tools/resources while preserving protocol and security rules. |
| [mcp-application/webmcp](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/specification-to-source/mcp-application/webmcp/SKILL.md) | Add in-page MCP to the MCP and frontend parents. |

### Document generation recipe

| Skill | Purpose |
| --- | --- |
| [document-pair](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/specification-to-source/document-pair/SKILL.md) | Generate a terminal presentation/companion-document pair for one selected documentation ecosystem. |

### Source-to-specification analysis

| Skill | Purpose |
| --- | --- |
| [architecture](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/source-to-specification/architecture/SKILL.md) | Extract evidence-backed boundaries, dependencies and entrypoints. |
| [api-surface](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/source-to-specification/api-surface/SKILL.md) | Extract public interfaces, inputs, outputs, errors and compatibility. |
| [behavior-state](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/source-to-specification/behavior-state/SKILL.md) | Extract observable workflows, transitions and concurrency invariants. |
| [operations](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/source-to-specification/operations/SKILL.md) | Extract configuration, persistence, recovery and deployment observations. |
| [security](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/source-to-specification/security/SKILL.md) | Extract trust boundaries, permissions and sensitive flows with explicit gaps. |
| [tests](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/source-to-specification/tests/SKILL.md) | Extract asserted behavior, missing coverage and possible preserved defects. |
| [language-cpp](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/source-to-specification/language-cpp/SKILL.md) | Translate C++ evidence into base behavior and language Flavor proposals. |
| [language-elixir](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/source-to-specification/language-elixir/SKILL.md) | Translate Elixir evidence into base behavior and language Flavor proposals. |
| [language-javascript](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/source-to-specification/language-javascript/SKILL.md) | Translate JavaScript/TypeScript evidence into base behavior and Flavor proposals. |
| [language-python](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/source-to-specification/language-python/SKILL.md) | Translate Python evidence while retaining dynamic-behavior uncertainty. |
| [language-rust](https://github.com/jordanhubbard/literate-ai/blob/c093657e8839d2adbcb4858a1195dcec9dd29dcc/skills/source-to-specification/language-rust/SKILL.md) | Translate Rust evidence while retaining unsafe/macro uncertainty. |

## Maintaining this inventory

When adding, removing or moving a canonical skill, update its row, task routing and
counts and reviewed source revision here in the same change. Enumerate sorted `skills/**/SKILL.md` paths and
compare them to this page's skill links; exclude root onboarding, distribution copies
and third-party trees. Verify each canonical path appears exactly once in the complete
inventory. Read changed skill content before updating its purpose or capability claims.

For a comparison with another catalog and prioritized improvements, read the
[Google skills assessment](../architecture/google-skills-assessment.md).
