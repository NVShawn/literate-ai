# Literate AI 0.10.1 (historical release marker)

Literate AI 0.10.1 was released on 2026-09-05 from the former repository
`NVIDIA-dev/literate-ai` (now archived), at commit `b0888f7f51d031c2f0a1660cc484f3ff861f3329`.

That repository's history was not carried into
[jordanhubbard/literate-ai](https://github.com/jordanhubbard/literate-ai), which starts from a
sanitized snapshot of the 1.1 development line. This tag marks the release so that version
history and changelog references stay coherent; it does not contain the 0.10.1 source.

Install a current release from
[jordanhubbard/literate-ai releases](https://github.com/jordanhubbard/literate-ai/releases).

---

## 0.10.1 - 2026-09-05

- Fail closed before launching an external project lifecycle driver when
  `--from-accepted-source` is requested. Accepted-source-only continuation is a
  Standard lifecycle contract; the shared rebuild parser previously accepted the flag
  for external drivers without carrying its no-coding-CLI invariant into their request
  authority, allowing normal model generation to proceed (#331).
- Make `package-npm` an executable, selection-bound Standard lifecycle contract.
  JavaScript generation remains dependency-free unless that Flavor is selected;
  selected projects may replay an exact `package-lock.json` through the detected npm
  CLI into disposable object storage, package the installed runtime closure, and
  never authorize global installation or registry publication. Standard npm v3
  package paths, exact optional runtime nodes, satisfied required peers, and explicitly
  absent optional peers are projected without requiring nonstandard redundant fields
  (#326, #329, #330).
- Reject zero-entrypoint Standard command projection explicitly until an attributable
  importable-library test and acceptance harness exists. The former Python unittest
  and Node test-discovery commands emitted runner-native output that the Standard
  evidence contract could never accept, so they promised a lifecycle that always
  failed later instead of implementing the library artifact requested by #280.
- Treat a recorded root repo-man build as the automatic baseline, parity, and retained
  receipt authority for nested `build.*` stages. Internal nested drivers remain visible
  and manually callable through `litai.harness.mk`, but conversion no longer repeats
  them after the canonical root orchestration; unrelated dotted stages and repo-man
  inventories without a root build remain independently executable (#325).
- Keep release contribution closure valid when the selected patch line lives in a
  linked worktree: the clean attached default-branch checkout is release authority,
  not an undisposed peer contribution. Use a platform-native synthetic executable in
  the Codex authentication regression so Windows reaches the no-model-request contract
  instead of rejecting a POSIX-only test path.
- Keep the release skill's mandatory guidance self-contained in initialized projects.
  Significant mid-release features still require an ADR, explicit human acceptance,
  and durable work recording, without routing derived projects to a framework-only
  documentation path (#322).
- Delay accepted-source cache visibility until root packaging, packaged execution, and
  independent project acceptance all succeed. A project-level rejection can no longer
  leave a reusable but verifier-rejected coding-agent result that deterministic retries
  replay forever; publication evidence is now recorded only after the cache write.
- Keep an outer-planned derivation key stable when a bounded generated-candidate retry
  adds rejection feedback to the accepted attempt's coding-agent prompt. The final
  prompt remains exact provenance, while receipt membership continues to close over
  the immutable pre-generation manifest.
- Read a canonical format-only committed source-cache target as an empty, non-mutating
  miss. Git cannot preserve the writable layout's empty directories; nonempty targets
  still require their entry, key, and CAS namespaces, while absent optional
  source-intelligence namespaces remain empty and partial layouts fail closed with the
  exact rejected path.
- Require a first stable initial, major, or minor release candidate to contain the live
  remote default branch throughout plan, prepare, check, and publish. A release line
  can no longer silently omit approved pre-release trunk work, while patch releases
  remain selective (ADR 0034). Make the checked-in Homebrew formula a declared version
  mirror derived from one Ruby constant, restoring 0.11.0 synchronization after the
  concurrent stable-line publication and allowing release preparation to update it
  atomically. Bind repair checkpoints to the exact global gate-plan prefix so a newly
  inserted prerequisite cannot inherit an out-of-order prior pass, and require a clean
  from-gate-one rerun after repaired completion before accepting release evidence.
- Treat the canonical GitHub HTTPS, SCP-style SSH, and `ssh://` spellings of the same
  owner/repository as one initialization origin during `litai update`, while preserving
  exact distinctions for other hosts, repositories, credentials, ports, and local
  origins (#316).
- Make operator adoption the 0.10 front door: project-independent `doctor`,
  composed `status`, acknowledged plan-then-apply `onboard create|adopt`, explicit
  evidence-bound brownfield stages, and release-candidate-wheel create/adopt golden
  paths. Original source remains release authority until current regenerative
  qualification, and deterministic host/filesystem work stays in Python adapters
  rather than model prompts (ADR 0035).
- Distinguish inherited Component catalogs from the active persisted Component-lock
  set. Retained brownfield receipts now validate every selected lock and require their
  own current legacy-wrapper lock without demanding target-local locks for unrelated
  reusable Components inherited from the framework.
- Regenerate the terminal manager/engineering document pair as the 0.11.0 edition
  so the last minor before 1.0 binds prefix CLI self-update, JavaScript bundle
  self-check, npm Flavor version authority, LocalStandard cache identity, and
  structured persistent-service errors, with authenticated preflight and export-back
  evidence (ADR 0034).
