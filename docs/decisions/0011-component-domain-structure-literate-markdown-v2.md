# ADR 0011: Component Domain Structure Becomes Typed Metadata Behind `literate-markdown@2`

- Status: Accepted for 1.1
- Date: 2026-08-22
- Accepted: 2026-09-13, by the project owner in the release session
- Decision owners: literate-ai maintainers
- Roadmap: [MITIGATION-FW-001](../roadmap/active-work.md#mitigation-fw-001-execute-the-framework-defect-mitigation-program), Phase 4 of [framework-defect-mitigation-program](../roadmap/framework-defect-mitigation-program.md)

## Context

A `component.md` is expressive about *authority plumbing* — coordinates, versions,
capability provides/requires, flavor slots, entrypoints, source dependencies, assets,
provider resolutions — but nearly empty of *domain structure*. Everything semantic
about what a component does must live in freeform prose, capped at 16,384 characters
(`_MAXIMUM_DESCRIPTION_CHARACTERS`, `adapters/component_markdown.py`), and nothing in
that prose is machine-checkable. Two incidents made the cost concrete:

1. The full-stack Rust/JavaScript sample's two-stage protocol — exact output fields,
   argv wiring, per-role file layout — could not be expressed by any component
   metadata slot and was therefore hardcoded into the framework prompt builder
   (`adapters/models/coding_cli.py`). Phase 2 removed that special case (MITIGATION-FW-002),
   but only by moving it into one sample's prose; the next complex component will hit
   the same wall.
2. Acceptance contracts are URI selectors with no type, and the shipped starter sample
   declares `acceptance_contracts: []`. "Measurable acceptance" is documented as core
   to the premise yet is unenforced and untyped where it matters.

Meanwhile the verifier architecture is already sound: generated tests never grade
acceptance, independent oracles exist, and locks bind parsed authority. What is
missing is typed structure for the generator to implement and the verifier to check —
not a new trust model. Separately, the strict parser rejects unknown frontmatter keys,
so adding fields to `literate-markdown@1` documents would break every existing reader;
the published-schema immutability rule forbids editing released contracts.

## Decision

1. **A new specification-provider version, not an edit.** `literate-markdown@2`
   accepts additive optional frontmatter fields; `@1` documents remain valid forever
   and can never contain the new fields. The provider version participates in
   specification-set identity exactly as `specification_provider` already does, so an
   upgrade is an explicit authoring act, visible in every lock.

2. **Four structured domain fields** (all optional, all lock-bound so they are
   authority rather than decoration):

   - `data_contracts:` — typed schema attachments (`kind: json-schema | protobuf |
     avro | csv-header`) referencing pinned `assets` entries with enforced media
     types. Wire formats stop being prose.
   - `invariants:` — bounded predicate expressions over entrypoint inputs/outputs,
     evaluated exclusively by the independent verifier. This closes the remaining
     "generated tests grade themselves" surface for declarative properties without
     letting generated code near its own grading.
   - `error_taxonomy:` — enumerated `{code, exit_or_status, when}` rows, checked
     against observed failure behavior by the verifier instead of trusted from prose.
   - `performance_budgets:` — declarative `{metric, bound, probe}` rows consumed by
     qualification, replacing shell-command counting with measured budgets.

3. **Typed acceptance contracts.** `acceptance_contracts` entries gain a `type`
   discriminator — `golden-io`, `exit-code`, `schema-match`, `property` (referencing
   an invariant) — each with a deterministic verifier adapter. A generatable
   component (not `sample: true`) with zero acceptance contracts fails validation
   unless it carries an explicit waiver field, making "measurable acceptance" an
   enforced floor instead of a slogan.

4. **New contract identities, untouched published schemas.** New wire types enter the
   v2 schema catalog under fresh identities. No released v1 schema changes; the
   published-immutability test suite remains untouched by construction.

5. **Prompt-builder neutrality becomes enforceable.** With structure available in
   metadata, the Phase 2 rule ("the envelope stays domain-neutral") extends from a
   tripwire test to validation: generation context assembly may include locked bytes
   verbatim but may not branch on domain vocabulary.

### Classification (per ADR 0003)

- **Invariant:** verifier-only evaluation of invariants, property acceptance, and
  error-taxonomy checks; generated code never evaluates its own grading; fail-closed
  parsing per provider version.
- **Policy:** the four initial field types and four acceptance kinds are the v1 set
  of `@2`; extending them is an ordinary versioned contract addition.
- **Deferred claim:** completeness of the predicate DSL ("any property a reviewer
  would write is expressible") is explicitly not claimed at launch; the DSL ships
  bounded, documents its grammar, and grows by contract addition.

## Consequences

- The 16 KiB prose cap stays: structure moves *out* of prose into checked fields, so
  the cap remains a forcing function toward decomposition into named boundaries.
- Every new field widens lock identity inputs; existing locks are unaffected because
  their `@1` documents cannot carry the fields. Upgrading a component to `@2` is a
  deliberate re-lock, reviewed like any specification change.
- Authoring cost rises slightly for complex components and falls for consumers:
  schemas, error rows, and budgets become diffable, validatable, and reusable across
  flavors instead of re-narrated in prose.
- Rejected alternatives: (a) keep everything in prose and rely on model diligence —
  rejected because the full-stack incident demonstrated this fails predictably at the
  first composition boundary; (b) freeform YAML extension of `@1` — rejected because
  unknown-key rejection is load-bearing fail-closed hygiene and published-schema
  immutability forbids mutating released contracts.
- Follow-on work lands separately: qualification consuming `performance_budgets`,
  remote-worker probe execution, and the stateful reference sample (program Phase 5)
  that proves the whole chain without prompt special-cases.
