# Course evidence and limits

These lessons show edited transcript excerpts from real local sessions. They are
not unedited screen recordings. Narration is synthetic (Samantha as LitAI,
Daniel as Sam); characters are fictional. SSH worker setup is walkthrough-only.

## Tested sessions

- Initial greenfield run: installed Literate AI 1.0.1, authenticated Codex,
  model selector `gpt-5.6-sol`, scaffolded hello Component. The actual agent
  generated Python source and three tests. Initial independent acceptance failed
  because the scaffold oracle expected unrelated fields. Aligning the disposable
  verifier with the written greeting/name specification allowed the rebuild to
  reuse its candidate, pass all three tests, and commit a current receipt.
  `litai verify` passed three gates with source intelligence skipped by policy.
  A pip wheel built on the implicit local worker and passed package verification
  with the same model selection. No registry upload or application release occurred.
- Framework repair: the upcoming release corrects the starter oracle at its
  source, with a fresh-scaffold regression verifying its expectations against
  the written greeting specification. The lesson distinguishes this repair from
  the earlier demo-local correction.
  A fresh `onboard create --apply --acknowledge` against the public repository
  parent also produced the corrected oracle without hand-editing the child.
- Brownfield: public [TinyXML2](https://github.com/leethomason/tinyxml2) at
  `8224e427b655b83dae5e2298f1e6919523a78737`, adopted in a disposable clone.
  CMake build and CTest passed during baseline and wrapper parity. The first
  retained run failed because CTest 4.4.3 emits `100% tests passed out of 1`.
  The corrected parser accepts that exact all-passed form as well as the older
  failed-count form, without inventing counts for partial/unknown summaries.
- Repaired live TinyXML2 run: CMake build exit 0; CTest exit 0; one passed,
  zero failed. [Retained evidence](evidence/tinyxml2-retained.json) binds the
  actual harness output and project revision. The finalized candidate was
  published via `project test-receipt update`; `convert-stage advance --to retained`
  succeeded. Stage is **retained**, release authority **original-source**.
  No native rewrite, specification qualification, or upstream TinyXML2 changes
  are claimed.

Framework fixes and video tooling were developed against source base
`360efaceecc6b03f78c943f7259ce7317c27be02`; the containing Git commit identifies
the exact course and tool changes. This is development-release material, not a
claim that the new video command already exists in an older published wheel.

The separate `build --from-accepted-source` continuation on the original 1.0.1
demo still reported `source_cache.runtime_absent` even with the same model
selector. This path is not shown as a successful step; its cause remains under
investigation and is not covered by the two confirmed defect repairs above.

## Review and reproduction

The media receipt detects changed files and source evidence. It is not a signature
or independent proof that narrative claims are correct. Re-execute the documented
demo workflows in disposable projects when refreshing for a new release; update
this ledger and manifests before rendering. Keep selected model inputs identical
between build and verification. Never substitute successful compilation for
independent acceptance, exact test counts, packaging, or release evidence.

TinyXML2 is zlib-licensed; this package shows command/output excerpts, not its
implementation source. See its [license](https://github.com/leethomason/tinyxml2/blob/8224e427b655b83dae5e2298f1e6919523a78737/LICENSE.txt).
Character artwork and prompts are retained in `assets/`; no real person is depicted.
