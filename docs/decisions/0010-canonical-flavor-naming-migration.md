# ADR 0010: Canonical Flavor Names Use One Axis-Prefixed Dash Scheme, Migrated Behind Explicit Re-Pinning

- Status: Accepted
- Date: 2026-08-22
- Closed: 2026-08-28 (0.8.0). Directory names follow canonical names. Dotted
  selectors fail closed. Bare target aliases remain.
- Decision owners: literate-ai maintainers
- Roadmap: [MITIGATION-FW-001](../roadmap/active-work.md#mitigation-fw-001-execute-the-framework-defect-mitigation-program), Phase 3 of [framework-defect-mitigation-program](../roadmap/framework-defect-mitigation-program.md)

## Context

Flavor identity is currently spelled three incompatible ways inside canonical
coordinates: dash-prefixed (`lang-python`, `os-linux`, `build-make`,
`toolchain-swift-apple`), dot-prefixed (`package.pip`), and bare
(`google-workspace`, `microsoft-365`). One flavor accepts up to five selector
spellings (`pip`, `package.pip`, `package-pip`, `packaging=pip`,
`flavor://literate-ai/package.pip`), four of the nine axes have no flat alias at all,
and directory names, canonical names, and target values diverge (`bazel` /
`build-bazel` / `bazel-preferred`). Since Phase 2 (MITIGATION-FW-002), the *selector
tables* derive from the shipped catalog's `flavor.md` frontmatter, so adding a flavor
no longer requires Python edits — but the *coordinate scheme itself* remains the
mixed convention frozen into every shipped `flavor.md`.

The migration has been deferred for one reason: flavor authority bytes feed flavor
revision identities, which are bound into checked-in `component.lock.json` files,
sample harness manifests, and test receipts. Renaming `package.pip` to
`package-pip` without a coordinated re-pin silently invalidates every lock that
selected it, converting an aesthetic cleanup into distributed corruption.

## Decision

1. **One canonical scheme.** Every canonical Flavor name is `<axis-prefix>-<value>`
   with a dash separator, using this fixed prefix table:

   | Axis | Prefix | Example |
   | --- | --- | --- |
   | `platform.os` | `os-` | `os-linux` |
   | `platform.architecture` | `arch-` | `arch-arm64` |
   | `accelerator` | `accel-` | `accel-nvidia-cuda` |
   | `implementation.language-ecosystem` | `lang-` | `lang-python` |
   | `implementation.ui-framework` | `ui-` | `ui-react` |
   | `implementation.javascript-packages` | `js-` | `js-npm` |
   | `build.system` | `build-` | `build-make` |
   | `toolchain` | `toolchain-` | `toolchain-swift-apple` |
   | `packaging` | `package-` | `package-pip` |
   | `deployment` | `deploy-` | `deploy-docker` |
   | `documentation.ecosystem` | `doc-` | `doc-google-workspace` |

   The separator encodes no semantics; it exists so one regex can distinguish a
   canonical name from a bare value. Namespacing stays `flavor://<namespace>/<name>`.

2. **Deprecation aliases, not silent acceptance.** The 0.8.0 window close removed
   dotted canonical names from live selectors. Bare target values remain valid
   indefinitely through `flavor_candidate_aliases` and the derived init alias
   table. Lock bytes always store the canonical form. `canonical_flavor_coordinate`
   still maps leftover dotted identities so duplicate alias directories fail
   closed instead of forking.

3. **Directory names follow canonical names** (`flavors/package-pip/`, not
   `flavors/pip/`) so disk, catalog, and selector agree. Target values are decoupled:
   `build-bazel`'s target value becomes `bazel` (dropping `bazel-preferred`) as part
   of this migration rather than a separate B4 item.

4. **Migration is a reviewed re-pin, not a rewrite.** A
   `litai catalog migrate-flavor-names` command with `--dry-run` / `--record`
   semantics (mirroring `scripts/review_lifecycle_driver.py`) enumerates every
   affected lock, harness manifest, receipt fixture, and sample metadata identity,
   shows the exact drift, and re-pins only after review. The rename commit contains
   catalog changes and re-pinned consumers together; CI fails on any half-migrated
   state because byte-equality and current-lock checks cannot pass across the split.

5. **No wire-schema change.** Coordinates are opaque strings in published contracts;
   historical documents that cite old coordinates remain valid records of past
   derivations. Only newly written locks carry canonical coordinates.

### Classification (per ADR 0003)

- **Invariant:** selector ambiguity fails closed; one flavor identity resolves to
  exactly one coordinate; lock bytes bind canonical forms only.
- **Policy:** the specific prefix table and the dash separator. Another conforming
  catalog namespace may choose different prefixes by recording its own decision.
- **Deferred claim:** "every shipped flavor has exactly one accepted spelling" holds
  only after the deprecation window closes; until then command evidence states which
  spellings were consumed.

## Consequences

- Adding an axis requires adding its prefix to the table in this ADR and to the
  derived-alias logic — one place each, both reviewed together.
- Existing user scripts that pass `+package.pip` keep working through the window and
  start warning; nothing breaks at upgrade time.
- The migration touches every shipped packaging flavor plus `google-workspace`/
  `microsoft-365`; the sample matrix and self-hosting receipts re-pin in the same
  commit. Estimated blast radius is known and enumerable because Phase 2 made the
  catalogs byte-comparable and the TCB explicit.
- Rejected alternative: keep `package.*` and declare the split meaningful. Rejected
  because the split carries no semantics, fragments validation patterns, and forced
  the dual dict/alias machinery Phase 2 just deleted.
- A catalog must not keep a deprecated alias directory beside its canonical Flavor
  (`flavors/pip` next to `flavors/package-pip`, `flavors/google-workspace` next to
  `flavors/doc-google-workspace`). That is two authorities for one coordinate.
  Validation fails closed; `litai catalog migrate-flavor-names` removes the alias
  tree when the canonical directory already exists. Init and update stamp only the
  canonical directory. Bare selectors such as `+pip` remain valid indefinitely as
  the ergonomic short form and must resolve to the canonical directory, not recreate
  the alias. Directory names now follow the canonical names (`python` →
  `lang-python`). Dotted selectors such as `+package.pip` fail closed.
