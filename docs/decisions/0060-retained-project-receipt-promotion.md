# ADR 0060: Promote a verified retained-project run into the project test receipt

- Status: Accepted
- Date: 2026-11-04
- Acceptance: The directing user explicitly accepted ADR 0060 on 2026-11-04 ("I acceptt ADR0060, proceed").
- Decision owners: directing user and literate-ai maintainers
- Release target: 1.5.1 consumer request; framework release line to be chosen on acceptance
- Roadmap: [RETAINED-RECEIPT-PROMOTE-001](../roadmap/active-work.md#retained-receipt-promote-001-promote-a-verified-retained-project-run-into-the-project-test-receipt)
- Issue: [#295](https://github.com/jordanhubbard/literate-ai/issues/295)
- Governance: [ADR 0006](0006-significant-feature-request-governance.md)

## Context

A project on the original-source retained lifecycle (`--retained-project`, manifest and
profile) can pass and be admitted with a retained receipt, yet has no supported route to
its configured `test_receipt`. `litai test --update-receipt` refuses retained authority;
`project test-receipt update` accepts only a finalized candidate envelope; `run-retained`
requires a converted harness inventory; and a Standard run requires the Standard runner
and suite. A project with a custom retained-suite `test_receipt_policy` therefore reports
`project.test_receipt_missing` indefinitely (issue #295).

## Decision

Add `litai project test-receipt promote-retained CANDIDATE`. It executes nothing. It
re-opens the retained receipt from its custody store and refuses unless all of these hold
against the *current* project:

- the receipt is the canonical document its identity names and every record and root
  verifies in custody;
- the retained plan, input manifest, verifier policy bytes and test policy equal what the
  current project re-plans, the current inputs are unchanged, and the retained source SBOM
  equals the one derived from the current plan;
- the completed run binds the current plan, project receipt policy, test policy and
  verifier policy; every command observation is a clean run of its plan action; every test
  report and acceptance observation is bound to the plan, matches the reviewed inventory,
  and the receipt summary equals the sum of its own reports;
- no test was skipped (a project test receipt accounts for passing tests only);
- the policy's `runner_identity` equals the runner identity *derived from the run*
  (below), the policy suite is not the retained-harness suite, every evidence kind the
  policy requires is one the run can supply, and the passed count meets
  `minimum_test_count`.

On success it writes the finalized candidate envelope, which `update` then validates
again with its unchanged policy checks. No policy check is relaxed, skipped or
parameterized.

### Runner identity

A retained run has no intrinsic runner identity. It is defined as the canonical identity
over each `test` action's tool identity and argv template and every `test-definition`
input member (path and digest). The `test-policy` member is excluded because it names the
receipt policy and would make the definition circular. A project whose policy pins any
other value is refused with the derived value in the message; adopting the derived value
is the project's own reviewed policy change, never an implicit rebind.

### Envelope contents

Evidence kinds supplied: acceptance-result, build-result, lifecycle-command,
lifecycle-plan, lifecycle-request, observation-result, resolved-sbom, source-cache-decision,
source-cache-lifecycle, source-sbom, test-report, test-runner, workspace-admission. The
four source-cache and lifecycle-request/command values are derived `not-applicable`
records required by the envelope contract. A policy requiring any other kind is refused.

## Consequences

The envelope remains a typed TCB boundary, not a cryptographic attestation; transported
receipts still need independent admission. The retained resolved SBOM is verified for
custody and context but not recomputed. Adoption requires a project to pin the derived
runner identity in its policy, which changes its project authority and therefore its
lifecycle-driver and documentation identities.

### Known limits (from independent review)

- The runner identity does not cover the test policy's cases and reports (only its
  circular `project_policy_identity` field is non-hashable); shrinking the reviewed
  inventory and re-running yields the same runner. Hashing the policy without that field
  is a follow-up.
- `lifecycle-request`, the cache records and `workspace-admission` are derived from run
  identities, not independently attested; `lifecycle-command` binds the verified command
  observations. A policy relying on them as proof is overstating them.
- The resolved SBOM and payload manifest are checked for custody only. A writer of the
  custody store could fabricate a coherent receipt; acceptance observations are checked for
  case identity and exit code, not stdout/stderr.
