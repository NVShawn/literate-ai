# Literate AI 0.7.2 (historical release marker)

Literate AI 0.7.2 was released on 2026-08-27 from the former repository
`NVIDIA-dev/literate-ai` (now archived), at commit `be642f4a9d1b58e0cba6c63e84c5d703a7987445`.

That repository's history was not carried into
[jordanhubbard/literate-ai](https://github.com/jordanhubbard/literate-ai), which starts from a
sanitized snapshot of the 1.1 development line. This tag marks the release so that version
history and changelog references stay coherent; it does not contain the 0.7.2 source.

Install a current release from
[jordanhubbard/literate-ai releases](https://github.com/jordanhubbard/literate-ai/releases).

---

## 0.7.2 - 2026-08-28

- Fail closed when a Flavor catalog keeps a deprecated alias directory beside its
  canonical `package-*` or `doc-*` Flavor. `litai catalog migrate-flavor-names`
  removes the alias tree when the canonical directory already exists; init from
  `+pip` or `+google-workspace` stamps only the canonical directory.
- Regenerate the manager/engineering document pair on every major or minor cut;
  patch cuts keep README citations on that last edition. `0.7.1` is the catch-up
  edition because `0.7.0` skipped the refresh. The 0.7.1 pair is published: 26-slide
  Slides resource `1V9mt1JpEst_2ucC0eJIIw7dff64MrtFRfut8MSiukeE` updated in place, and
  narrative Google Doc `1fMxy0NTT54MV4T9AwmA8F0VahNet93Xhp0d0pszI6GA` created.
