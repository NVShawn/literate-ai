# Literate AI 0.6.3 (historical release marker)

Literate AI 0.6.3 was released on 2026-08-26 from the former repository
`NVIDIA-dev/literate-ai` (now archived), at commit `a4b20625132fe06170d7cdb237e6a58d78d3140b`.

That repository's history was not carried into
[jordanhubbard/literate-ai](https://github.com/jordanhubbard/literate-ai), which starts from a
sanitized snapshot of the 1.1 development line. This tag marks the release so that version
history and changelog references stay coherent; it does not contain the 0.6.3 source.

Install a current release from
[jordanhubbard/literate-ai releases](https://github.com/jordanhubbard/literate-ai/releases).

---

## 0.6.3 - 2026-08-26

- Bound Component-lock semantic differences independently of traversal work so
  large, mostly equal locks remain reviewable.
