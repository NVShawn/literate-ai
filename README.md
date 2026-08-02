# Literate AI 0.1.0a1 (historical release marker)

Literate AI 0.1.0a1 was released on 2026-08-02 from the former repository
`NVIDIA-dev/literate-ai` (now archived), at commit `a67dfdb5058c73cdb3f65c2517151ef21129a90e`.

That repository's history was not carried into
[jordanhubbard/literate-ai](https://github.com/jordanhubbard/literate-ai), which starts from a
sanitized snapshot of the 1.1 development line. This tag marks the release so that version
history and changelog references stay coherent; it does not contain the 0.1.0a1 source.

Install a current release from
[jordanhubbard/literate-ai releases](https://github.com/jordanhubbard/literate-ai/releases).

---

## 0.1.0a1 - 2026-08-02

- Added the software-neutral Component, Flavor, source, intelligence, workflow, model,
  security, artifact, publication, settings, and source-to-specification contracts.
- Added deterministic composition, Flavor resolution, content-addressed storage,
  append-only events, atomic workspace acceptance, and filesystem publication.
- Added exact Git/local-source, CodeGraph, OpenSpec, OpenAI-compatible Responses API,
  multi-model portfolio, guarded Python build, and local lifecycle adapters.
- Added read-only OVA compatibility readers and semantic shadow comparison.
- Added six packaged source-to-specification skills and an executable sample/conformance
  ladder, including target Flavor and security-policy cases.
- Added executed two-generation literate-ai self-hosting through the real generation,
  validation, classification, authorization, build, workspace, and event lifecycle.
- Isolated the pinned Node.js OpenSpec contributor tool from the zero-runtime-dependency
  Python distribution.

This alpha proves bounded offline self-hosting with an exact-snapshot structured model
provider. It does not claim a universally hardened build sandbox, general semantic
rewriting by an arbitrary remote model, or completion of OVA's two-release migration
observation period. Separately authorized dynamic observation has concrete fail-closed
`sandbox-exec` and `bwrap` adapters; the Python build adapter remains authorization-gated
but is not a universally hardened operating-system sandbox.
