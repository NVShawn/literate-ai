# Phase 2 compatibility prerelease and rollback report

This is the historical prerelease report. The completed decision and retained-reader
policy are recorded in [the final cutover report](phase-2-final-cutover.md).

## Release state

| Artifact | State | Identity |
| --- | --- | --- |
| `v0.1.0a1` | Published Phase 1 compatibility release | Git commit `a67dfdb5058c73cdb3f65c2517151ef21129a90e`; wheel SHA-256 `03ee1a5216fe5e6435f269f6e18edf78a65d92fc76c50c2632da4be172eb9f58` |
| `v0.1.1a1` | Published Phase 2 compatibility prerelease | Git commit `bd86984f4ca3b1cf9adb4d75d6d4105b7e345c6c`; wheel SHA-256 `2b042455853fd292a9e1994d358c932b3d9ca1020befc9f2815fa47d13c76d45` |

The `0.1.1a1` wheel is the second published compatibility artifact. Its Git commit,
release tag, uploaded size of 209,506 bytes, and GitHub asset digest are immutable and
independently verifiable. OVA may pin the exact commit above; it must not pin a moving
branch or use an unverified local wheel.

## Candidate scope

The candidate adds the provider-neutral Phase 2 migration boundary:

- exact framework release identity over the distribution version and public contract
  set;
- typed requests and results for source identity, intelligence, composition,
  generation, validation, security/build, packaging/linking, publication, and Settings;
- exact wire-byte and canonical semantic-payload bindings;
- fail-closed release, input, port, and read-only-shadow validation; and
- downstream OVA conformance evidence for per-seam shadow/framework execution,
  immutable comparison records, rollback, exact Flavors, clean-cache signed and
  classified self-hosting, stable second-generation identities, and macOS launch.

The wheel remains software-neutral. OVA, Omniverse, Isaac, NVIDIA, CUDA, RTX, ROS, and
other product or vendor identities remain downstream policy.

## Gates completed for this prerelease

- the complete Literate AI Python suite passes;
- Ruff lint and formatting checks pass;
- every active and durable OpenSpec document passes strict validation;
- the wheel builds and installs outside the checkout;
- the installed package reports `0.1.1a1`, exposes the deterministic snapshot-replay
  runtime then described as self-hosting, and includes the six reviewed
  source-to-specification skills; and
- OVA's focused Phase 2 suite and real sandboxed macOS Granian framework-mode launch
  pass against the local framework checkout.

These are prerelease-quality and compatibility proofs. They are not a production
observation window.

Hosted CI run `30768120684` passed on macOS and Ubuntu with Python 3.11 and 3.12,
including installed-wheel smoke in every job. The published release is available at
<https://github.com/NVIDIA-dev/literate-ai/releases/tag/v0.1.1a1>.

## Observation window originally required after publication

After both `v0.1.0a1` and `v0.1.1a1` are genuinely published and independently
verifiable, OVA must still run the documented compatibility window. For every migrated
seam it must:

1. pin an immutable released framework identity;
2. collect real read-only shadow comparisons over representative user Components and
   caches;
3. canary a single framework writer only after exact comparison succeeds and legacy
   write authority is removed;
4. rehearse rollback against a real migrated cache copy while retaining immutable
   diagnostic evidence; and
5. satisfy the agreed observation duration and error/parity thresholds on supported
   macOS and Linux targets.

The existence of two artifacts, even after both are published, does not substitute for
this runtime evidence.

## Rollback procedure

If the `0.1.1a1` candidate or eventual release fails before framework write authority
is enabled:

1. restore the exact `0.1.0a1` pin or disable the optional framework import;
2. set every OVA seam to `legacy` and restart the control plane;
3. verify OVA health and Settings diagnostics; and
4. retain candidate caches and comparison evidence without translating or deleting
   legacy cache state.

If a canary seam has framework write authority, first stop new work, return that seam
to its last legacy authority, and verify that the legacy reader can consume the retained
pre-cutover state. Any framework-only writes remain quarantined for diagnosis; they are
never silently rewritten into the legacy cache.

## Historical duplicate-code removal gate

Downstream general lifecycle implementations remain in place through the entire
observation window. Removal requires two published compatibility releases, completed
per-seam production observation, successful canary and rollback drills, supported-host
CI, and a confirmed legacy-cache retention policy. Until all conditions are true, OVA
Those were the prerelease conditions. The final cutover report records which gates were
proved and the repository owner's explicit decision to proceed without claiming the
elapsed production observation window.
