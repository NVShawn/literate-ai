# Literate AI 0.9.1 (historical release marker)

Literate AI 0.9.1 was released on 2026-09-16 from the former repository
`NVIDIA-dev/literate-ai` (now archived), at commit `dd58fcf4e1a8bffa0d13b64a98b14a938a17718c`.

That repository's history was not carried into
[jordanhubbard/literate-ai](https://github.com/jordanhubbard/literate-ai), which starts from a
sanitized snapshot of the 1.1 development line. This tag marks the release so that version
history and changelog references stay coherent; it does not contain the 0.9.1 source.

Install a current release from
[jordanhubbard/literate-ai releases](https://github.com/jordanhubbard/literate-ai/releases).

---

## 0.9.1 - 2026-09-16

- Restore the supported installed migration path from 0.8.4 projects. Framework
  updates now recognize equivalent GitHub SSH and HTTPS origins, and the reviewed
  `project lifecycle rebind-standard` transaction can advance exact installed
  distribution authority without hand-editing project pins. The rebind preserves
  project-owned custom receipt policies and advances only Standard-derived runners.
