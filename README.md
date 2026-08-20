# Literate AI 0.5.1 (historical release marker)

Literate AI 0.5.1 was released on 2026-08-20 from the former repository
`NVIDIA-dev/literate-ai` (now archived), at commit `f3359691063e1700ef7ac01461f16170adbc8757`.

That repository's history was not carried into
[jordanhubbard/literate-ai](https://github.com/jordanhubbard/literate-ai), which starts from a
sanitized snapshot of the 1.1 development line. This tag marks the release so that version
history and changelog references stay coherent; it does not contain the 0.5.1 source.

Install a current release from
[jordanhubbard/literate-ai releases](https://github.com/jordanhubbard/literate-ai/releases).

---

## 0.5.1 - 2026-08-20

- Fixed `litai release check` rejecting a prepared commit range the instant it
  touched a project's configured test-receipt file, even though that receipt
  can only be refreshed *after* prepare's own commit (it binds to the exact
  prepared revision's project authority identity) -- every project with a
  `test_receipt_policy` hit an unsatisfiable "prepare, then the receipt goes
  stale, but recording a fresh one violates scope" bind. `check_release()`
  now also treats the project's own declared `test_receipt` path as
  legitimate prepared-commit scope, alongside the existing changelog/version/
  documentation-authority paths.
- Fixed `_SanitizedCodeGraphRunner.run` leaving an overflowing `codegraph init`
  subprocess running until the poll deadline instead of terminating it the
  instant the output budget was exceeded, and joining its drain threads with
  only a 1s timeout before unconditionally closing the pipes -- racing a
  still-running drain thread's mutation of the shared output bytearray.
  `process.wait()` is now bounded (5s, then a re-terminate plus a second 5s
  wait) and thread joins get a two-stage 5s budget; a still-unresponsive
  process or drain thread after both stages raises instead of hanging or
  decoding a buffer a thread might still be writing. (#78)
- Fixed `exclusive_cache_lock`'s TOCTOU swap guard comparing `(st_dev,
  st_ino)` on Windows, where `st_ino` is not a stable unique file identity
  across every volume and Python version, letting a replaced or
  reparse-point lock file compare falsely equal or spuriously unequal. On
  Windows the comparison now reopens the path without following reparse
  points and compares Win32 `GetFileInformationByHandle` file-index/volume
  identity against the held descriptor's own identity; POSIX keeps the
  existing `(st_dev, st_ino)` comparison. (#76)
- Fixed `lifecycle_driver_implementation_identity()`'s directory scan
  hashing any dot-prefixed file that happened to exist under a declared
  `implementation_paths` directory -- including files invisible to `git
  status`/`git diff` because they match a *global* `core.excludesFile`
  pattern (`.DS_Store`, a nested `.claude/settings.local.json`, editor swap
  files) rather than this repository's own `.gitignore` -- producing a
  byte-for-byte clean tree with a changed TCB digest and no identifiable
  diff. Any rglob entry with a dot-prefixed path component is now excluded,
  matching the existing `__pycache__` exclusion. (#125)
- Fixed `litai update --apply` aborting the whole transaction the moment any
  single `(kind, name)` catalog-import identity collided between a locally
  tracked import and the freshly recomputed inherited plan, even for files
  structurally unrelated to the colliding item. A collision now leaves the
  project's existing local provenance record for that one key authoritative
  (colliding inherited entries are dropped, not merged) while every other
  file's write eligibility is still governed independently by the existing
  per-file plan classification. (#133)
- Fixed `litai release publish` passing the entire `CHANGELOG.md` as
  `--notes-file`, so every GitHub release page opened with the permanent,
  always-empty "Unreleased" heading followed by every past version's notes
  too. Release notes now extract just the named version's own section.
