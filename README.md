# Literate AI 0.8.4 (historical release marker)

Literate AI 0.8.4 was released on 2026-09-14 from the former repository
`NVIDIA-dev/literate-ai` (now archived), at commit `3fe1877828e9d714fa258271b8913a6a74ec6196`.

That repository's history was not carried into
[jordanhubbard/literate-ai](https://github.com/jordanhubbard/literate-ai), which starts from a
sanitized snapshot of the 1.1 development line. This tag marks the release so that version
history and changelog references stay coherent; it does not contain the 0.8.4 source.

Install a current release from
[jordanhubbard/literate-ai releases](https://github.com/jordanhubbard/literate-ai/releases).

---

## 0.8.4 - 2026-09-13

- Resolve the configured sample model before derivation planning, so live execution
  and receipt assembly use the same model-scoped recipe keys.

- Bind the existing DMN and SCXML sample application contracts as public interfaces
  so generation receives their exact JSON fields and result shape.

- Preserve locked authored asset identities and verified bytes through Standard
  sample generation, so asset-bearing samples can reach build and execution.

- Preserve explicit build models and accepted-source continuation settings in
  artifact exports, so `run` reconstructs their authority while still refusing
  genuinely stale artifacts. Default export records retain their existing shape.

- Keep the explicitly selected live model in installed-project `build` and `test`
  smoke calls, matching `rebuild` instead of falling back to ambient defaults.

- Bind the Homebrew formula's version and tag URL to one declared release mirror,
  so release preparation updates the packaged formula with the distribution.

- Serialize shared repository-lineage cache creation and fetches so concurrent
  initialization cannot race on Git shallow state or resolve another fetch’s head.

- Fix concurrent Windows lock-file initialization when another writer acquires
  the placeholder byte first. Repair native Windows test fixtures for SSH process
  ownership, coding-CLI stubs and isolated operator configuration.

- Provision and use the managed runtime for `make release-check-reset`, so a clean
  host can discard stale release and Python checkpoints without requiring project
  dependencies in the bootstrap interpreter.
