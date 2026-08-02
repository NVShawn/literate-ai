# Literate AI 0.1.1a1 (historical release marker)

Literate AI 0.1.1a1 was released on 2026-08-02 from the former repository
`NVIDIA-dev/literate-ai` (now archived), at commit `bd86984f4ca3b1cf9adb4d75d6d4105b7e345c6c`.

That repository's history was not carried into
[jordanhubbard/literate-ai](https://github.com/jordanhubbard/literate-ai), which starts from a
sanitized snapshot of the 1.1 development line. This tag marks the release so that version
history and changelog references stay coherent; it does not contain the 0.1.1a1 source.

Install a current release from
[jordanhubbard/literate-ai releases](https://github.com/jordanhubbard/literate-ai/releases).

---

## 0.1.1a1 - Unreleased

- Added provider-neutral typed contracts and a fail-closed application bridge for
  independently migrating all nine Component lifecycle seams.
- Bound lifecycle requests and results to the exact framework release, canonical
  semantic payload identity, and original wire-byte digest.
- Added read-only shadow enforcement, immutable downstream comparison evidence, and
  reversible framework-authority conformance coverage.
- Added downstream OVA evidence for exact Flavor policy, clean-cache signed and
  classified self-hosting, stable two-generation identities, rollback, and a real
  sandboxed macOS Granian launch against the local framework checkout.
- Added strict SemVer 2.0 domain versions (separate from the PEP 440 distribution),
  exact `ComponentRevisionRef` and `VersionedContentRef` contracts, multi-revision
  Component/model/skill resolution, and exact profile/Flavor/model routing locks.
- Upgraded bundle and publication manifests to bind coordinate, semantic version, and
  immutable revision; imports now require the expected Component revision and reject
  mismatches before transfer.
- Added deterministic compatibility readers that reject future schemas and fail closed
  when legacy documents lack enough information for an unambiguous exact migration.

This prerelease candidate is prepared but not committed, tagged, or published by this
change. It does not complete the production observation window or authorize removal of
duplicated downstream lifecycle code.
