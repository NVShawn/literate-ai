# Literate AI 0.10.0 (historical release marker)

Literate AI 0.10.0 was released on 2026-09-05 from the former repository
`NVIDIA-dev/literate-ai` (now archived), at commit `03d3257eea445559f42cdae5bad01b222a34ef06`.

That repository's history was not carried into
[jordanhubbard/literate-ai](https://github.com/jordanhubbard/literate-ai), which starts from a
sanitized snapshot of the 1.1 development line. This tag marks the release so that version
history and changelog references stay coherent; it does not contain the 0.10.0 source.

Install a current release from
[jordanhubbard/literate-ai releases](https://github.com/jordanhubbard/literate-ai/releases).

---

## 0.10.0 - 2026-09-05

- Regenerate the terminal manager/engineering document pair as the 0.10.0 edition
  so publication can bind authenticated preflight and export-back evidence (ADR 0034).
- Close completed CI tracker residue (#83–#86) and delete superseded closed-PR
  remotes; keep release lines and remaining open product issues on the queue.
- Make release closure executable and continuous: plans bind release-class collateral
  and a live contribution sweep; publication requires explicit Google account/resource
  preflight and export-back evidence; tracker dispositions and branch lifecycle survive
  into the next release cycle (ADR 0034).
- Add an explicit plan/apply Standard lifecycle rebind for intentional wheel upgrades.
  Application policy depends on an injected installed-distribution protocol while the
  CLI owns the concrete host adapter, preserving hexagonal dependency direction (#309).
- Require reusable ancestor defects to be searched and filed or linked in the owning
  upstream tracker before implementation, with sanitized fail-closed proposals and
  explicit external-write authorization (#308).
- Teach exact quoted Make language-tool expansion in the common portable-application
  authority and every applicable language specialization, with validator-to-skill and
  initialized-template parity checks (#310).
- Preserve project-owned Component workflow, routing, and skill inputs during
  framework-template retirement. Converted projects can now update without losing
  their local generation closure, while genuinely missing inputs still fail atomically
  and the downstream `telemetry-dashboard` reproduction succeeds (#306).
- Separate conversion source membership from post-build artifact observation. Files
  created by a legacy build remain visible as artifacts without becoming authored
  source authority, while modifications and removals of pre-existing source remain
  detectable (#305).
- Render conversion-aware starter documentation through one initialization/update
  helper so framework updates retain the lift-and-shift ADR link, preserve local edit
  conflicts, validate documentation reachability, and become idempotent (#283).
- Preserve complete stdout/stderr identities and bounded diagnostics without failing a
  successful host-heavy baseline solely because its output exceeds the retained excerpt
  limit (#311).
- Keep direct pytest profiling runs and their xdist workers from writing interpreter
  bytecode into the authored checkout. CI now preserves the same source-tree custody
  invariant as Makefile-driven tests without weakening documentation validation (#312).
- Keep the installed release-state-machine proof offline after canonical initialized
  projects gained continuous contribution closure. The fixture now omits only that
  external tracker policy while real project policies remain fail-closed (#314).
- Require published GitHub releases to be stable, carry non-empty notes, and expose a
  non-empty version-matching wheel. Release contribution sweeps now consume durable
  exact-head Git branch-lifecycle markers, so an obsolete branch remains dispositioned
  after its tracker issue closes.
- Archive completed program documents under `docs/history/roadmap/` and map remaining
  open GitHub issues into the work queue.
