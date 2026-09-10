# Literate AI 1.0.1 (historical release marker)

Literate AI 1.0.1 was released on 2026-09-10 from the former repository
`NVIDIA-dev/literate-ai` (now archived), at commit `b9d906d35a983236a1ea8bb8c26b486a05c44019`.

That repository's history was not carried into
[jordanhubbard/literate-ai](https://github.com/jordanhubbard/literate-ai), which starts from a
sanitized snapshot of the 1.1 development line. This tag marks the release so that version
history and changelog references stay coherent; it does not contain the 1.0.1 source.

Install a current release from
[jordanhubbard/literate-ai releases](https://github.com/jordanhubbard/literate-ai/releases).

---

## 1.0.1 - 2026-09-10

- Allow `litai onboard adopt` to convert repositories containing nested Git
  submodules, relocating Gitlink-containing trees with `git mv` so initialized
  submodule worktrees and their metadata remain valid (#366).
- Reuse verified sealed build artifacts across fresh lifecycle authorizations,
  rebinding current evidence instead of colliding on repeat `build`/`test`
  invocations (#362).
- Keep the installed-project release smoke test on its resolved live model for
  `rebuild`, `build`, and `test` proofs.
- Register isolated generated Python modules before executing them so standard
  runtime features such as `@dataclass` work through the authorized host runner.
- Allow compiler version discovery up to 60 seconds so parallel macOS CI does
  not reject a healthy Xcode toolchain during sample validation.
