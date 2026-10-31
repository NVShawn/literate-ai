# Google skills: discovery, composition and reuse assessment

This assessment supports [SKILL-DISCOVERY-001](../roadmap/active-work.md#skill-discovery-001).
It compares authored guidance and distribution structure, not measured agent success.
The resulting [Literate AI skill catalog](../user/skills.md) applies the most immediate
lesson: make capabilities discoverable by user task before explaining internal taxonomy.

## Evidence and scope

Reviewed on 2026-10-31 from actual Git checkouts:

- Literate AI baseline `c093657e8839d2adbcb4858a1195dcec9dd29dcc`: all
  88 canonical `skills/**/SKILL.md` files, grouped by their declared purpose;
  root onboarding and skill architecture supply the composition rules.
- [google/skills at `bb9b3f464aae37af360d079c1d863524955b0363`](https://github.com/google/skills/tree/bb9b3f464aae37af360d079c1d863524955b0363):
  cloned with `git clone https://github.com/google/skills.git`; all tracked skill
  paths and index entries enumerated; representative finder, solution, product,
  troubleshooting, migration and plugin files read in depth.

Google has **178 canonical skills** under `skills/`: 157 cloud, 14 ads, 3 developers,
2 analytics and 2 identity. Its generated index has 178 entries. Of those skill
directories, 167 have a `references/` directory and 38 have `scripts/`. These are
filesystem counts, not execution or quality scores. Plugin-bundled copies are excluded.
The 16 external plugin submodules recorded in `.gitmodules` were not initialized or
audited. No Google skill scripts, cloud commands, installation or live deployments
were executed. Findings about product behavior concern instructions, not validated
service results; private Google verification is outside this assessment.

The counts can be reproduced in each checkout by enumerating
`Path('skills').rglob('SKILL.md')` in Python; group by the second path segment and
compare Google's result to `len(json.loads(Path('index.json').read_text())['skills'])`.
The reviewed Google index entrypoint paths also match the canonical file set exactly.
Source links below select the reviewed commit so future catalog changes do not silently
change the evidence behind this analysis.

## How Google composes skills

Google primarily groups skills by **product domain**, then composes them through
**discovery and explicit cross-references**. That differs from Literate AI's split
by transformation direction and its inherited, pinned recipe graph.

1. **Discovery is a separate, small layer.** The
   [categorized README](https://github.com/google/skills/blob/bb9b3f464aae37af360d079c1d863524955b0363/README.md)
   gives humans an immediate capability map. The
   [generated index](https://github.com/google/skills/blob/bb9b3f464aae37af360d079c1d863524955b0363/index.json)
   exposes names, descriptions and entrypoint URLs. The
   [finder](https://github.com/google/skills/blob/bb9b3f464aae37af360d079c1d863524955b0363/skills/developers/finding-google-skills/SKILL.md)
   retrieves and filters that index, shortlists at most three skills, then loads only
   those matches. It validates retrieval and reports failure instead of inventing a
   catalog result. Its live `main` URLs are a freshness mechanism, not immutable pins.
2. **A task entry point owns its supporting material.** Most skill directories
   have references; some have scripts and assets. The
   [build/deploy agents solution](https://github.com/google/skills/tree/bb9b3f464aae37af360d079c1d863524955b0363/skills/cloud/google-cloud-solution-build-deploy-agents)
   combines a staged workflow, design principles, product mappings and solution,
   implementation and validation templates. Details load when needed rather than
   all appearing in the initial discovery description.
3. **Solution skills orchestrate product guidance.**
   [Solution architecture](https://github.com/google/skills/blob/bb9b3f464aae37af360d079c1d863524955b0363/skills/cloud/google-cloud-solution-architecture/SKILL.md)
   proceeds from requirements through architecture, validation and presentation;
   specific solutions refer to relevant product guidance. Descriptions include use
   and exclusion criteria to route between overlapping skills. Directory ancestry
   does not establish the mandatory parent/delta semantics Literate AI defines.
4. **Tools and current documentation are part of composition.**
   [Developer Knowledge](https://github.com/google/skills/blob/bb9b3f464aae37af360d079c1d863524955b0363/skills/developers/retrieving-developer-knowledge/SKILL.md)
   selects connected MCP tools or a REST fallback and checks for failed lookups.
   The [gcloud skill](https://github.com/google/skills/blob/bb9b3f464aae37af360d079c1d863524955b0363/skills/cloud/gcloud/SKILL.md)
   requires exact leaf-command help, bounded output and explicit project/location
   scope. Repeated attribution references establish request tagging, not dependency
   graph integrity.
5. **Plugins bundle an operating environment.** The
   [Cloud Developer plugin](https://github.com/google/skills/tree/bb9b3f464aae37af360d079c1d863524955b0363/plugins/cloud/google-cloud-developer)
   packages skills, discovery rules, MCP configuration and client manifests.
   [Agent](https://github.com/google/skills/blob/bb9b3f464aae37af360d079c1d863524955b0363/.agents/plugins/marketplace.json)
   and [Claude](https://github.com/google/skills/blob/bb9b3f464aae37af360d079c1d863524955b0363/.claude-plugin/marketplace.json)
   marketplaces expose local bundles and version-referenced external plugins.
   These references should not be mistaken for Literate AI's full content-digest
   closure or its operator-local MCP catalog.

The overall model is a discoverable collection of task packages with selective
context loading and prose orchestration. It is not evidence of a uniform,
machine-enforced workflow engine or a digest-checked graph between all skills.

## Where each catalog is stronger

“Stronger” here means a concrete authoring or discovery advantage visible in the
reviewed files. No controlled cross-model execution comparison was performed.

| Dimension | Google advantage | Literate AI advantage / implication |
| --- | --- | --- |
| New-user discovery | Categorized capability list, machine-readable index, finder and explicit exclusion criteria | The new task catalog closes the immediate human-discovery gap; architecture alone was too indirect |
| Cloud operations breadth | Deep GKE, Cloud Run, IAM, databases, observability, cost and security guidance, plus ads/analytics/identity | Our catalog focuses on provider-neutral SDLC, portable generation and inverse analysis; cloud depth is a real coverage gap, not evidence of inferior portable generation |
| Task packaging | Local references, templates and scripts provide concrete deliverable shapes | Our agent skills deliberately wrap deterministic commands; preserve that approach and add references only where judgment needs them |
| Troubleshooting | [GKE workload troubleshooting](https://github.com/google/skills/blob/bb9b3f464aae37af360d079c1d863524955b0363/skills/cloud/gke-workload-troubleshooting/SKILL.md) branches on observed states and proposes GitOps patches | Host health and lifecycle evidence already exist here; a symptom-to-evidence map could make them easier to use |
| Dependency composition | Lightweight references are easy to author and load selectively | Our exact versions/digests, ordered dependency closure and nested deltas make recipe identity and drift explicit |
| Workflow authority | Solution packages provide readable staged checklists and validation templates | Our specifications, Flavors, workflows, routing and independent acceptance have distinct enforced owners; prose cannot replace those contracts |
| Documents and presentations | Solution templates package technical findings for presentation | Our dedicated artifact-authoring, document-pair and video skills cover editable output, regeneration, rendering and publication read-back; this is not a Google superiority area |
| Distribution | Product plugins expose skills, tools and prompts together across clients | We already have [coding CLI release plugins](../user/coding-cli-plugins.md); improve discoverability there rather than introducing a competing installer |
| Validation evidence | Public [link checker](https://github.com/google/skills/blob/bb9b3f464aae37af360d079c1d863524955b0363/.github/workflows/link-checker.yml) and stated internal review | Our changed-skill SkillEvaluator gate and catalog validation are inspectable; public Google sources do not prove a stronger executable admission gate |

Google's [contribution policy](https://github.com/google/skills/blob/bb9b3f464aae37af360d079c1d863524955b0363/CONTRIBUTING.md)
says verification happens internally and external code contributions are not accepted.
That is a governance choice, not independent proof of quality. Likewise, our static
skill admission does not prove a coding agent applies guidance correctly under pressure.

## What to reuse, and how

The following are prioritized adaptations, not claims that new runtime features were
implemented by this documentation change.

| Priority / state | Source pattern | Narrow Literate AI owner and proposed adaptation | Evidence required |
| --- | --- | --- | --- |
| P0 — applied here | Categorized README | Documentation: complete task catalog linked from root README, docs index, user guide and architecture; distinguish skills from Flavors and roadmap capability | Every canonical path appears once, counts match, links resolve, target claims match current authority |
| P1 — recommended | Finder plus generated index | Catalog/discovery: derive a local search view from validated skills with path, purpose, layer, ancestor chain and content identity; return a bounded shortlist | Exact coverage, duplicates rejected, stale projection detected, offline lookup, and a clear separation between search suggestions and recipe selection |
| P1 — recommended | Use/exclusion routing criteria | Narrow agent skills: add examples of matching and neighboring tasks where ambiguity is demonstrated; keep generation prompts compact | Fresh-context routing scenarios, including negative cases; no duplicated parent policy or changed authorization |
| P1 — recommended | Solution templates and linked references | Existing authoring/development skills: add minimal optional decision and evidence-ledger templates where outputs currently vary; use PROJECT.md and the work queue as authority | Demonstrate a real task yielding complete artifacts without a second work ledger; evaluate changed skill bytes and affected sample |
| P2 — recommended | State-based troubleshooting | Worker-health / lifecycle evidence: index symptoms to deterministic observations, then propose a bounded repair plan | Actual failure fixtures, bounded observation, explicit unavailable evidence and separate verification of any repair |
| P2 — recommended | Product-specific depth | A separately selected Google Cloud integration catalog/Flavor, if product demand warrants it; borrow Cloud Run/GKE/IAM examples and solution validation structure | Exact upstream revision/license record, target-scoped contracts, native integration evidence and authorization tests; no ambient cloud requirement for portable projects |
| P2 — recommended | Rust migration parity/telemetry | Source-to-specification and promotion workflows: study [migrate-to-rust](https://github.com/google/skills/blob/bb9b3f464aae37af360d079c1d863524955b0363/skills/developers/migrate-to-rust/SKILL.md) for seeded differential checks and measured before/after reporting | Retain original behavior authority, independent parity evidence and explicit numerical tolerances; do not mandate a rewrite language, coverage percentage or PGO for unrelated tasks |

For a future implementation, begin with the local generated discovery view: it extends
this inventory, makes routing testable and avoids adding another hand-maintained
catalog. Keep topical tags orthogonal to the existing agent/generation/inverse layout;
renaming the whole tree by topic would churn pins and obscure lifecycle boundaries.

## What should not be cherry-picked wholesale

- **Live remote instructions as recipe authority.** Google's finder follows current
  remote entrypoints. Here, discover from a local validated catalog and resolve exact
  selected pins before generation; external documentation can inform a reviewed update.
- **Vendor selection as a generic default.** The finder intentionally considers Google
  when it could fit an unspecified stack. Our provider-neutral core must leave product
  intent and platform selection with the appropriate authority.
- **Repeated prose as enforcement.** Some Google solution workflows gate many intermediate
  artifacts on user approval. Transfer useful requirements/validation structure while
  preserving existing session authorization and deterministic gates; do not add repeated
  confirmations to work already authorized.
- **An inferred diagnosis as observed truth.** GKE guidance has offline fallbacks that
  propose fixes from symptoms. Here those remain hypotheses until actual evidence proves
  them; a suggested patch is not a successful repair or lifecycle receipt.
- **Automatic attribution, installation or cloud writes.** Request tags, credentials,
  service enablement and client-specific MCP setup are integration policy, not portable
  generation technique. Plugin availability also does not prove a connected tool works.

The reviewed repository carries [Apache-2.0](https://github.com/google/skills/blob/bb9b3f464aae37af360d079c1d863524955b0363/LICENSE).
This change imports no Google skill text or scripts. Any later copy should retain
applicable notices and attribution, record its exact origin, review bundled resources
and dependencies, and pass our normal skill admission plus behavior qualification.
External plugin licenses and contents need their own review.

The practical direction is to combine Google's accessible task discovery and selective
reference loading with Literate AI's existing explicit authority, dependency and evidence
boundaries. The human catalog is now present; automated discovery and task-specific
adaptations remain separately reviewable recommendations.
