# Literate AI 1.0.0 (historical release marker)

Literate AI 1.0.0 was released on 2026-09-08 from the former repository
`NVIDIA-dev/literate-ai` (now archived), at commit `477313cebc019afe72536e34aac0b5e37c27b471`.

That repository's history was not carried into
[jordanhubbard/literate-ai](https://github.com/jordanhubbard/literate-ai), which starts from a
sanitized snapshot of the 1.1 development line. This tag marks the release so that version
history and changelog references stay coherent; it does not contain the 1.0.0 source.

Install a current release from
[jordanhubbard/literate-ai releases](https://github.com/jordanhubbard/literate-ai/releases).

---

## 1.0.0 - 2026-09-08

- Add a portable, project-scoped lifecycle mutation lock covering `litai rebuild`
  (including `--update-receipt`) so a second concurrent mutation against the same
  project fails closed with a typed `lifecycle.project_locked` diagnostic naming the
  active holder (operation, pid, host) before either side pays for an expensive
  model stage, instead of both racing shared checkpoint/finalized-receipt
  publication and one later failing an unrelated-looking predecessor-contract error.
  Read-only commands are unaffected. Failure-safe: the advisory OS-level lock
  releases automatically if the holder crashes (#318).
