# Literate AI 0.1.1 (historical release marker)

Literate AI 0.1.1 was released on 2026-08-02 from the former repository
`NVIDIA-dev/literate-ai` (now archived), at commit `465260cb450dc1c386e257f93ff0277a28f8666b`.

That repository's history was not carried into
[jordanhubbard/literate-ai](https://github.com/jordanhubbard/literate-ai), which starts from a
sanitized snapshot of the 1.1 development line. This tag marks the release so that version
history and changelog references stay coherent; it does not contain the 0.1.1 source.

Install a current release from
[jordanhubbard/literate-ai releases](https://github.com/jordanhubbard/literate-ai/releases).

---

## 0.1.1 - 2026-08-02

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
- Added an evidence-bound final-cutover manifest that proves framework write ownership,
  downstream duplicate removal, retained compatibility reads, and rollback rehearsal
  independently for every lifecycle seam.
- Recorded the repository owner's explicit Phase 2 cutover direction as a visible risk
  acceptance, not as evidence that an elapsed production observation window occurred.

This stable release makes Literate AI the canonical owner of provider-neutral Component
versioning and lifecycle contracts. Downstream products retain their product policy and
read-only compatibility adapters. The production observation window was not completed;
the final report preserves that fact and the repository owner's explicit direction to
complete the cutover without it.
