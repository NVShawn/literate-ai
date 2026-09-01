# Literate AI 0.8.3 (historical release marker)

Literate AI 0.8.3 was released on 2026-09-01 from the former repository
`NVIDIA-dev/literate-ai` (now archived), at commit `58a4c3bafb2a7a22d4cc21bb08a971605ca69959`.

That repository's history was not carried into
[jordanhubbard/literate-ai](https://github.com/jordanhubbard/literate-ai), which starts from a
sanitized snapshot of the 1.1 development line. This tag marks the release so that version
history and changelog references stay coherent; it does not contain the 0.8.3 source.

Install a current release from
[jordanhubbard/literate-ai releases](https://github.com/jordanhubbard/literate-ai/releases).

---

## 0.8.3 - 2026-09-01

- **Validate the live-qualification model before trusting it, and surface the
  real coding-CLI error.** A wrong or mis-qualified model id in
  `literate.test.json` (or `--model` / `LITAI_LIVE_MODEL`) was accepted
  unvalidated and only failed deep inside a generation run, masked as a generic
  "Unexpected server error". Two fixes: the opencode invocation now runs with
  `--print-logs --log-level ERROR`, so the real cause (e.g.
  `ProviderModelNotFoundError`) reaches the captured output; and a new preflight
  runs one minimal bounded task through the selected coding CLI + model before any
  generation, failing closed with `coding_cli.model_unavailable` (naming the model
  and the underlying cause) in seconds instead of minutes into a run. A new `litai
  worker verify-model` command lets an operator validate a `literate.test.json`
  before trusting it; the `configure-test-workers` skill directs operators to run
  it. Skippable via `LITAI_SKIP_MODEL_PREFLIGHT` for a deliberate dry run.
