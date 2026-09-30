---
name: configure-test-workers
description: Configure private macOS, Linux, and Windows workers for Literate AI sample fan-out without committing hostnames, usernames, destinations, credentials, or dynamic provisioning logic. Use when creating a local test matrix, selecting a matrix file for litai or Make, or adapting a private task-routing skill to supply ephemeral workers.
metadata:
  author: Literate AI maintainers <literate-ai-maintainers@users.noreply.github.com>
---

# Configure test workers

Keep worker identities and provisioning policy outside the project repository. Let the
Python path adapter choose platform locations; do not construct them from remembered OS
conventions.

1. Run `litai config paths` in the canonical project. Copy
   `literate.workers.example.json` to its reported `worker_config` path, or use an
   explicit absolute `--worker-config` path. For an existing 0.8.x checkout, run
   `litai config migrate` first and apply only a conflict-free plan.
2. Replace every example endpoint/workspace pair with the private SSH assignment. Leave
   unused hardware qualifiers null; pass exact OS/CPU/GPU constraints when known.
3. Copy `literate.test.example.json` to the reported project-scoped `test_config` path,
   or pass another absolute matrix path with `--config`. Select exact worker IDs and one matching platform Flavor;
   never duplicate endpoints, credentials, or provisioning commands in this matrix. The
   `coding_cli` and `model` fields are free for you to choose — including an inexpensive
   model you know — but the id must be one the coding CLI can actually resolve. After
   setting or changing them, run `litai worker verify-model` before trusting the config:
   it runs one bounded preflight and fails closed with `coding_cli.model_unavailable`
   (naming the model and the real cause) if the id does not resolve, instead of failing
   deep inside a live run. List valid ids with the coding CLI itself (for opencode:
   `opencode models`); a bare provider alias that only the interactive session resolves
   (e.g. `router/...`) is not necessarily the id a `run` subprocess accepts (it may
   need the full `custom-provider/router/...` form).
4. If workers are allocated dynamically, invoke the user's private routing skill first.
   Materialize its assignments as a temporary matrix outside version control, then pass
   that path explicitly. Never copy the private routing skill, credentials, API keys, or
   resolved node names into the project.
5. Run the installed host-tool preflight on each assigned worker before Git acquisition
   or sample execution. Treat coding-agent authentication as a separate readiness gate.
6. Run `litai worker probe --all` to refresh the reported user-state
   `worker_observations` inventory. Review degraded NVIDIA results before
   selecting CUDA work: missing `nvidia-smi` means no discoverable NVIDIA device, while
   an installed command that cannot query its driver is an unknown/degraded state, not
   an empty inventory. Never copy observed capacity into authored minimum requirements.
7. Run one sample first, then expand the sample glob only after all workers pass
   platform, source-authority, and toolchain preflight.

The checked-in examples define only synthetic workers and selections. Real worker and
matrix files are durable user configuration outside the project tree.
