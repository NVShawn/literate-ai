# Phase 2 typed lifecycle bridge

Literate AI now exposes a provider-neutral, fail-closed execution boundary for a
downstream product that migrates one lifecycle seam at a time. The public contracts
are `FrameworkReleaseIdentity`, `LifecycleRequest`, `LifecycleResult`, and
`LifecycleSeam`; `LifecycleBridge` is the application service. A request binds both
the parsed payload identity and the digest of the original wire bytes.

The bridge invokes a registered typed `LifecyclePort` only when all of these facts are
exact:

- the configured release identity equals the installed distribution version and
  public contract set;
- the request names that release identity;
- the input wire bytes and parsed payload still match the request; and
- the port's declared seam matches its registration key.

A read-only request fails with `migration.shadow-write-attempt` if its port reports a
write. Release, request, input, and missing-port failures have stable codes and occur
before authority can be transferred. The framework owns no OVA, Omniverse, Isaac,
NVIDIA, CUDA, RTX, or ROS identity; downstream adapters supply that policy.

The bridge is necessary but does not itself claim that a downstream lifecycle seam
has cut over. A product must register a real port backed by the corresponding typed
framework use case, compare exact outputs and external behavior, remove legacy write
authority, and pass its canary and rollback gates first.

## OVA conformance evidence

OVA's downstream bridge exercises all nine seams in legacy, read-only shadow,
framework, and rollback modes against this package's typed contracts. Its self-host
gate faults the actual `sample.ova-self-hosting` Component, OpenSpec, skill, and entry
point into an empty user cache; verifies authoritative signed-source admission; runs
classification and sandboxed object build; then proves stable identities across two
framework generation calls and rollback.

On macOS, OVA's real Granian control-plane smoke test also passes in framework mode
against the pinned local `literate-ai` checkout with the OS sandbox required. It checks
the app and Settings endpoints, source-tree immutability, and temporary-cache cleanup.
This is not a native RTX claim. OVA Phase 2 task 7.4 remains open until OVA's
repository-wide non-native suite also passes on the settled shared tree.
