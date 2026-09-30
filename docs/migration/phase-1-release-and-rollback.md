# Phase 1 alpha release and rollback

Released artifact: `v0.1.0a1` at commit
`a67dfdb5058c73cdb3f65c2517151ef21129a90e`. The published wheel
`literate_ai-0.1.0a1-py3-none-any.whl` has SHA-256
`03ee1a5216fe5e6435f269f6e18edf78a65d92fc76c50c2632da4be172eb9f58`.

Phase 1 introduces literate-ai as a read-only compatibility and shadow dependency. OVA
remains the behavioral authority. The alpha must never rewrite an OVA manifest, cache,
package, provenance record, publication record, or setting while it is in shadow mode.

## Pinning

Consumers pin the immutable Git commit behind the `v0.1.0a1` tag or a wheel whose SHA-256
has been verified against the release record. Floating branches and unqualified version
ranges are not migration inputs. Source checkouts are indexed independently at their
exact revision; the wheel is not treated as a substitute for source intelligence.

## Entry gate

Before enabling shadow mode:

1. Verify the framework repository validation matrix on macOS and Linux.
2. Install the exact alpha into an isolated environment and confirm `litai spec
   skills` finds all built-in manifests outside the source checkout.
3. Run `python -m literate_ai.compatibility.ova --baseline BASELINE --candidate CURRENT`.
4. Preserve the report digest with the OVA test evidence.
5. Leave all OVA cache and generation writers on the legacy implementation.

Exit code `0` means semantic parity, `1` means drift or missing artifacts, and `2` means
invalid or interrupted input. Only `0` is eligible for an observation run.

## Rollback

Rollback is configuration-only during Phase 1:

1. Set OVA's framework mode back to `legacy`.
2. Restart OVA and verify its health and Settings page.
3. Retain the shadow report and framework cache as diagnostic evidence; do not translate
   or delete OVA cache state.
4. Remove the optional literate-ai installation only after confirming no process imports
   it.

Phase 2 rollback is a feature-flag reversal to the last legacy seam. Destructive cache
conversion is forbidden, so the legacy reader and cache remain usable throughout the
two-release observation period.
