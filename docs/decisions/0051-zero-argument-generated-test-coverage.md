# ADR 0051: Exhaustive Generated Coverage for Zero-Argument Callables

- Status: Accepted
- Accepted: 2026-10-31; explicit directing-user acceptance for 1.4
- Date: 2026-10-23
- Decision owners: literate-ai maintainers
- Roadmap: ZERO-ARG-COVERAGE-001; discovered during NATIVE-CHILD-UPDATE-001
- Issue: [#132](https://github.com/jordanhubbard/literate-ai/issues/132), blocking final independent acceptance in [#48](https://github.com/jordanhubbard/literate-ai/issues/48)

## Context

A fresh installed registered-child journey completes a reviewed Standard rebind,
stale-registry refusal, reviewed unqualified replacement, and byte-for-byte recovery
inspection. Its final major rebuild stops before source generation: a pure callable
accepting no arguments has exactly one valid argument vector, `[]`, while the current
generated-test contract requires three distinct vectors and three categories.

The pinned portable-application skill additionally prohibits acceptance-input overlap.
The validator instead permits independently selected overlapping values and requires
coverage of every value-free acceptance invocation signature. Both authorities must
agree. Independent expected-result provenance is essential; mathematical disjointness
is impossible for a singleton domain and does not establish independent correctness.

Adding dummy product parameters, repeating one invocation under three names, or
silently lowering the global minimum would misrepresent the approved contract.
A general language for finite input domains would add substantially more scope than
this demonstrated zero-argument defect requires.

## Decision

Introduce an explicitly versioned generated-test coverage mode for callables whose
complete, authoritative public invocation signature has arity zero. The framework
must establish that closed singleton domain before generation from current public
interface authority; a generated manifest cannot grant itself the exemption. An
acceptance example alone is not evidence that a variadic or optional-argument API
has a singleton domain. If current authoring cannot establish the complete signature,
fail with an actionable pre-generation diagnostic rather than infer it from one case.

For that mode, require exactly one unique generated invocation `[]`, a current
non-acceptance specification reference, a complete expected result, and an honest
exhaustive-coverage designation. Do not manufacture example/boundary/invariant counts.
Generated expectations must be derived from approved public specifications; the
independently authored acceptance expected values and harness remain verifier-owned.
Both suites execute the actual public callable, even though their only valid input
necessarily overlaps. Ordinary implementation tests may provide additional checks,
but repeated calls do not become additional distinct-input coverage.

Bind the mode and supporting interface identity into the recipe, generated-suite
validation, cached artifacts, and lifecycle evidence. Introduce a versioned contract
rather than changing the meaning or exact field set of frozen v1 manifests. Existing
v1/default-domain diversity requirements remain enforced. Mixed signatures, one- or
two-valued nonzero-arity domains, optional parameters and variadic interfaces are
outside this first change and receive no automatic exemption.

Reconcile the pinned skill with the validator's existing allowance for independently
chosen overlapping inputs. Preserve the separation of expected-result authority;
do not expose private oracle answers or waive independent acceptance, project-level
case minima, signature checking, currentness, or tamper controls. If a declared
project minimum exceeds exhaustive distinct-input coverage, report that incompatibility
before generation; do not pad counts or promote a partial receipt.

## Acceptance

The directing user explicitly accepted this decision under ADR 0006 on 2026-10-31.
ZERO-ARG-COVERAGE-001 records its approved execution tasks before implementation.
Deterministic tests must cover the singleton case, rejection of invented arguments,
duplicate or missing cases, false self-declared exemptions, stale interface/mode
identities, private-oracle leakage, and unchanged ordinary v1 validation. A two-input
or unknown domain must not silently enter the singleton mode.

Finish with a fresh installed registered-child journey using the existing unchanged
independent oracle, all original refusal/recovery controls, full required regression,
installed-wheel qualification, ordinary review CI and exact post-merge main CI.
No source or qualification receipt has been produced by this proposal.

## Consequences

### Public authoring contract

Select the new mode by including `interfaces/callable.json` in the Component's
locked local specification artifacts, after the provider's ordinary Markdown
root. Literate Markdown permits JSON supporting artifacts. Its exact fields are `schema`
(`literate-ai/generated-test-coverage-request@1`), `mode`
(`exhaustive-zero-argument`), and `signature`. The signature has exactly
`schema` (`literate-ai/public-callable-signature@1`), `arity` (integer zero),
`optional` (false), and `variadic` (false). This reviewed public declaration
describes the complete callable, not one acceptance invocation. Flavor and
dependency documents cannot select the mode for the Component.

The v2 suite retains the existing case format with one `example` and an explicit
suite-level `coverage` descriptor containing the mode and exact public document
identity. It claims no boundary or invariant cases. Portable source-validation
inputs use v2 only for this mode; default v1 wire objects remain unchanged.

The framework can represent a real zero-argument API without changing the product to
satisfy its test generator. Coverage remains truthful and independent acceptance
remains mandatory. Versioned validation and identity propagation add implementation
work, and the public interface's ability to establish exact arity must be confirmed
before choosing the precise schema. General finite-domain declarations are deferred
until a concrete use case and separate design justify them.
