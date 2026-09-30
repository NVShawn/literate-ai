## 0. Reference Implementation Ecosystem

- [x] 0.1 Establish the Python 3.11+ reference distribution with an exactly pinned
  CycloneDX JSON-validation dependency and installed-wheel package check.
- [x] 0.2 Move the pinned npm/OpenSpec dependency tree under `tools/openspec/` and make
  Python installation independent of Node.js.
- [x] 0.3 Record the dependency-admission policy and language-neutral adapter boundary in
  ADR 0002 and the OpenSpec capability.

## 1. Contract and Baseline

- [x] 1.1 Export valid, invalid, interrupted, and drifted OVA lifecycle fixtures from the
  pinned baseline.
- [x] 1.2 Define versioned JSON Schemas, canonical hashing, stable errors, and pure
  migrations for all public objects.
- [x] 1.3 Add dependency-direction tests proving framework core has no OVA or adapter
  imports.

## 2. Neutral Domain and Ports

- [x] 2.1 Implement Component, specifications, capability, source, intelligence, workflow,
  model, validation, security, package, publication, settings, and event domains.
- [x] 2.2 Define application use cases and provider/storage/policy ports.
- [x] 2.3 Implement reference OpenSpec, Git/local source, CodeGraph, filesystem CAS,
  OpenAI-compatible model, Python validator/builder, and filesystem publisher adapters.
- [x] 2.4 Implement versioned Flavor definitions/revisions, axes/slots, target profiles,
  typed contributions, resolution decisions, effective specifications/revisions, and
  JSON Schemas.
- [x] 2.5 Implement strict domain SemVer, exact generic/Component revision references,
  and version-aware Component, Flavor, profile, model, workflow, and skill contracts
  while keeping Python distribution versions on PEP 440.

## 3. Durable Lifecycle

- [x] 3.1 Implement descriptor discovery, policy-driven resolution, lazy exact-source
  materialization, canonical source snapshots, and structured evidence.
- [x] 3.2 Implement durable workflow DAGs, run-scoped model decisions, retries,
  reconciliation, and transactional tree acceptance.
- [x] 3.3 Implement immutable source/build/artifact bundles, dependency projections,
  explicit publication, and typed scoped settings.
- [x] 3.4 Bind vocabulary, evidence, workflow runs, cache keys, bundles, publications,
  refresh, and security decisions to exact effective revisions and Flavor sets.
- [x] 3.5 Permit multiple immutable versions to coexist; require exact dependency,
  bundle, publication, and import refs; and migrate legacy documents only through
  explicit deterministic readers that reject ambiguity and future schemas.
- [x] 3.6 Add OpenCode as an exact bounded coding-CLI transport across forward and
  inverse model work, including provider schemas, clean configuration, credential
  custody, isolation evidence, documentation, focused tests, and installed-CLI proof.
- [x] 3.7 Reject an OpenCode executable that lacks the exact pure noninteractive command
  capabilities before model egress, with bounded secret-free diagnostics, focused
  forward/inverse regressions, and installed-wheel compatibility proof.

## 4. Security Enforcement

- [x] 4.1 Implement quarantine, origin/signature verification, revocation, scanners,
  normalized findings, and dependency propagation.
- [x] 4.2 Implement classification and short-lived exact builder authorization.
- [x] 4.3 Implement explicit maximum-privilege `yolo` acknowledgement, persistent warning,
  audit, revocation, and downstream provenance.
- [x] 4.4 Pass adversarial tests before enabling any builder by default.

## 5. Living Conformance

- [x] 5.1 Implement the neutral sample ladder with positive, negative, empty-cache,
  restart, publication, routing, and security cases.
- [x] 5.2 Implement the generated self-contained framework-readiness application and a
  separately pinned deterministic snapshot replay with stable second-generation
  identities; do not claim model-authored framework self-hosting.
- [x] 5.3 Run contract/conformance/integration gates on macOS and Linux.
- [x] 5.4 Add Windows/Linux, CPU/CUDA, Python/Rust, conflict, cache isolation,
  publication/import, security, and source-to-specification Flavor samples.

## 6. OVA Phase 1 Compatibility

- [x] 6.1 Implement lossless `ova.yaml`, settings, identity, cache, package, provenance,
  and publication compatibility readers.
- [x] 6.2 Run side-effect-free OVA shadow comparison over the baseline fixture matrix.
- [x] 6.3 Publish the pinned Phase 1 framework/compatibility release and rollback guide
  after all exit gates pass.

## 7. OVA Phase 2 Rebase

- [x] 7.1 Add the downstream OVA adapter and cut over each lifecycle seam behind reversible
  feature flags.
- [x] 7.2 Rebase OVA Settings/CLI/samples on the typed API while retaining OVA product and
  platform policy.
- [x] 7.3 Express OVA macOS/Linux, RTX/CUDA, ROS, and language/toolchain target policy as
  downstream Flavors without adding NVIDIA-specific identities to framework core.
- [x] 7.4 Prove clean-cache, signed-source, classified OVA-on-framework self-hosting plus
  full tests and macOS launch.
- [x] 7.5 Ship two compatibility releases, rehearse rollback, remove duplicated general
  lifecycle code, and publish the final migration report.

## 8. Literate-AI-Only Source-to-Specification Authoring

- [x] 8.1 Create the separate `src/literate_ai/source_to_specification/` bounded context
  and versioned request, observation, draft, coverage, uncertainty, review, and run
  contracts.
- [x] 8.2 Define `SpecAuthoringSkill` and named/versioned `SpecAuthoringSkillSet` contracts,
  content pinning, trust/classification, capability matching, ordered multi-skill
  execution, and skill/model provenance.
- [x] 8.3 Ship initial architecture, API-surface, behavior/state, tests, security, and
  operations skill packs under `skills/source-to-specification/`.
- [x] 8.4 Implement exact-source inventory, structured observation synthesis, OpenSpec
  draft rendering/strict validation, evidence coverage, conflict detection, versioned
  promotion policy, and authorized review acceptance.
- [x] 8.5 Implement incremental refresh across source, skill-set, intelligence/evidence,
  model/redaction policy, specification-provider, and accepted-base identities without
  silently overwriting reviewed requirements.
- [x] 8.6 Add `litai spec derive`, `audit`, `review`, `accept`, `refresh`, `conformance`,
  `coverage`, and `diff` use cases with stable JSON/API contracts; only `accept` mutates a
  specification target, and no command mutates analyzed source.
- [x] 8.7 Add language-neutral, multi-language, legacy/bug-compatible, incomplete-tests,
  multi-repository, and self-analysis samples under `tests/fixtures/source_to_specification/`.
- [x] 8.8 Implement signed local-source attestation and separately authorized sandboxed
  dynamic observation; pass provenance reconstruction, prompt-injection, egress,
  redaction, untrusted/contradictory-skill, unsupported-inference, coverage-threshold,
  deterministic-fixture, and promotion-authority gates before drafts become authoritative.
