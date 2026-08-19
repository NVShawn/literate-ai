# Literate AI 0.5.0 (historical release marker)

Literate AI 0.5.0 was released on 2026-08-19 from the former repository
`NVIDIA-dev/literate-ai` (now archived), at commit `79ba14098ae9e81313673965d74a6c105fe9ff09`.

That repository's history was not carried into
[jordanhubbard/literate-ai](https://github.com/jordanhubbard/literate-ai), which starts from a
sanitized snapshot of the 1.1 development line. This tag marks the release so that version
history and changelog references stay coherent; it does not contain the 0.5.0 source.

Install a current release from
[jordanhubbard/literate-ai releases](https://github.com/jordanhubbard/literate-ai/releases).

---

## 0.5.0 - 2026-08-19

- Fixed `litai test`/`litai build --force-regeneration` accepts accumulating
  multiple accepted source-cache entries at the exact same cache key with no
  reconciliation: each forced-regeneration accept skips the ordinary read (by
  design, to bypass a stale hit), so it never discovered a prior accepted entry
  already at that key, and unconditionally published a new one alongside it.
  Once two or more accepted entries existed at one key, a subsequent *plain*
  `litai test`/`litai build` (ordinary cache reuse, no `--force-regeneration`)
  failed with `source-cache.ambiguous`, and retrying `--force-regeneration`
  cleared it only once before the very next plain run reproduced the same
  failure, since each forced accept added another entry instead of replacing
  the prior one. `FilesystemStandardAcceptedSourcePublisher` now retires the
  superseded exact-key membership immediately after a forced-regeneration
  accept publishes its replacement, so a plain run self-heals without manual
  intervention; the retired entry's immutable manifest and CAS objects are
  never deleted, only its exact-key membership pointer, since accepted-source
  content is content-addressed and permanently reachable through
  `published_entries()`. Distinct from the already-fixed #54 (an uncaught
  `SourceCacheError` for the same "multiple accepted entries" read-time state):
  that fix only routed the error into the typed CLI envelope and added
  `--source-cache-entry` for manual disambiguation; it did not address why
  forced regeneration kept accumulating entries in the first place. (#137)
- Fixed `spec derive --translator coding-cli` silently dropping literal constant
  and lookup-table values instead of extracting them into a pinned asset: a closed
  literal dict/list/tuple/set (e.g. a hardware/family lookup table) was previously
  classified only as ordinary prose-describable behavior, so the model's structural
  Requirements about the table's *shape* (closed key set, priority order) could get
  accepted as `spec coverage: covered` while the concrete per-key values were
  visible only in raw evidence and never promoted into spec authority -- and a
  later forward regeneration from the accepted spec had no source of truth for
  those values and silently guessed wrong ones. `collect_behavioral_surface_inventory`
  now recognizes a closed literal table as its own required `literal-data` surface,
  and `derive_model_checkout` deterministically re-extracts the exact values from
  cited evidence (via `ast.literal_eval`, independent of any model output) into a
  hash-pinned JSON asset artifact; the corresponding coverage entry is reported as
  the new `asset-pinned` state rather than plain `covered`, so a reviewer can see
  the literal values were pinned verbatim rather than paraphrased. (#116, #118)
- Fixed `spec derive --translator coding-cli` permanently blocking promotion on a
  single transient model-call shortfall: a behavioral surface classified as only
  `inferred-intent` (rather than the stronger `observed-current-behavior` /
  `compatibility-quirk` claim kinds) rendered `unsupported` in `spec coverage`
  ("observations were not sufficient for a draft statement"), which set
  `review_gate.promotion_eligible: false` and blocked `spec accept`, even though an
  identical re-run against the exact same, unchanged source recovered full coverage.
  Mirroring the bounded retry issue #35 (LITAI-005) added for forward generation's
  `coding_cli.generated_metadata_invalid`, `derive_model_checkout` now retries a
  language's model-call translation up to 2 additional times specifically when it
  classifies an in-scope base-scope claim as `inferred-intent`. Every other claim
  kind (`suspected-defect`, `conflict`, `unknown`, and Flavor-scoped claims) is never
  retried: those are the model's genuine, reproducible epistemic findings about the
  source, and retrying them away would silently suppress a real signal. Deterministic
  `--translator static` derivation never calls a model and is unaffected. (#117)

- Fixed a Standard Component build that failed with `provider artifact ... has no
  runtime binding` when a root Component depended on the same provider for both
  generation/build and packaging (an exact `dependency_kind: packaging` edge).
  Packaging-kind edges no longer join a consumer's build-time provider artifacts;
  only artifact-export (build/runtime) providers get process environment bindings,
  while packaging edges continue to carry exact package provenance into package
  assembly and acceptance through the accepted lifecycle's own artifact graph. (#110)

- `spec accept --project-target` now verifies every promoted Component's
  qualification lock -- not just the root Component's -- before reporting a
  successful promotion, so a Component graph node whose materialized
  `specification_roots` cannot resolve fails promotion outright instead of
  letting a broken project be reported as `derived-source-retained` and only
  surfacing later as `litai lock` failing with
  `component_lock.content_unavailable`. Added an end-to-end regression test
  covering the documented `spec attest` -> `derive --translator coding-cli
  --allow-model-egress` -> `review` -> `accept --project-target` flow against a
  recovered Component whose specification uses the layered
  ("literate-markdown") output provider, asserting that every
  `specification_roots` entry in the promoted `component.md` resolves to a
  materialized file under `components/<name>/` and that `litai lock` succeeds
  against the promoted tree without a manual copy step. (#112)

- SSH lifecycle workers now return a digest-bound manifest plus deterministic evidence
  bundle instead of leaving source/object/artifact/receipt custody only in worker-local
  paths. The coordinator independently verifies and imports every listed file before
  issuing a custody receipt, then acknowledges the exact transfer so bounded idempotent
  cleanup can run. Failed Windows preflight diagnostics retain their redacted nested
  cause through the same transfer; missing, tampered, partial, duplicate, oversized, or
  unacknowledged transfers fail closed. (#131)
