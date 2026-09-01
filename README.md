# Literate AI 0.8.2 (historical release marker)

Literate AI 0.8.2 was released on 2026-08-31 from the former repository
`NVIDIA-dev/literate-ai` (now archived), at commit `c8bb20c983a4a2ebfaad1644e76433203aaf4214`.

That repository's history was not carried into
[jordanhubbard/literate-ai](https://github.com/jordanhubbard/literate-ai), which starts from a
sanitized snapshot of the 1.1 development line. This tag marks the release so that version
history and changelog references stay coherent; it does not contain the 0.8.2 source.

Install a current release from
[jordanhubbard/literate-ai releases](https://github.com/jordanhubbard/literate-ai/releases).

---

## 0.8.2 - 2026-09-01

- **Fix the checkpointed unittest runner crash for Standard-bound projects
  (#215).** `lifecycle_driver_implementation_identity` iterated a nonexistent
  `implementation_paths` list for a `StandardProjectLifecycleDriver`, raising an
  `AttributeError` before test discovery in any Standard-bound derived project.
  A Standard driver's implementation identity is now its framework distribution
  identity.
- **Fix persistent-service acceptance so it can pass (#214).** The
  persistent-service oracle launched the artifact in one-shot `--litai-smoke`
  mode, so it ran a single case and exited before readiness
  (`persistent-service exited before acceptance completed`). Add a
  `--litai-serve` launch mode: the acceptance now launches the artifact as a
  listening server, and the Node/native runtime drivers stream inherited stdio
  so a served process stays attached and pollable. The service generation skills
  mandate that a persistent-service artifact honor `--litai-serve` (bind
  host/port, serve, expose `GET /health`, stay alive). Fails closed if a service
  does not bind/serve.
