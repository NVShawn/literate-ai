# Private test matrix

Private test matrices map the repository's sample and lifecycle gates onto operator-owned
workers without making hostnames, credentials, or private routing part of project
authority.

## Configure workers and selections

Copy `literate.workers.example.json` to user `workers.json` and replace its SSH
placeholders with user-owned endpoints and workspaces. Copy
`literate.test.example.json` to project-scoped user `test.json` and select those exact
worker IDs with their matching `linux`, `macos`, or `windows` platform Flavors. The
resolved paths are reported by `litai init`: Linux/macOS default to
`${XDG_CONFIG_HOME:-$HOME/.config}/literate-ai`, while Windows uses the user's Roaming
AppData Known Folder. `LITAI_CONFIG_DIR` is an absolute override.

Never commit either private file. Worker names, credentials, dynamic provisioning
skills, and private routes are operator/session configuration rather than application
authority. Set `LITAI_TEST_CONFIG=/absolute/path/to/private-matrix.json` to override the
project-scoped path and
`LITAI_WORKER_CONFIG=/absolute/path/to/private-workers.json` for its worker catalog.

Live generation (`installed-project-e2e --live`, `make samples`, and remote fan-out)
fails closed unless both a coding CLI and a model are explicit. Put durable defaults in
project-scoped user `test.json` as `coding_cli` and `model` (see
`literate.test.example.json` for inert placeholders). Precedence is `--coding-cli` /
`--model`, then `CODING_CLI` / `LITAI_LIVE_MODEL`, then that file. PATH first-available
search is not a live-test default. Remote SSH workers require `opencode` on `PATH`,
configured with credentials for the pinned model's provider; opencode resolves them
from its own configuration or auth store, so Literate AI requires no specific key. Do
not put keys in `workers.json`.
A one-shot `--coding-cli` / `--model` override does not rewrite the JSON file.

```console
LITAI_SESSION_ID=dev make samples \
  SAMPLE_FLAVORS='--sample hello-component --coding-cli cursor-agent --model gpt-5.6-sol-high'
```

POSIX `--target local` release fan-out requires user `workers.json` with
at least one SSH POSIX worker, `opencode` on that worker's `PATH`, and
opencode credentials for the pinned model's provider. Project user `test.json` must
name that `opencode` plus model pair. Probe first, then check a prepared plan:

```console
litai worker probe --all
litai release check PLAN.json --target local --project .
```

Each worker gate runs in a dedicated checkout, `<workspace>/release/<repository>`,
that `litai release` syncs to the exact revision over SSH and SCP before the gate
starts; you do not need to check out the release on the worker yourself.

Without that catalog the gate fails closed (`release.target_unconfigured`). A JSON pin
that is not `opencode` fails closed on the controller (`coding_cli.remote_prerequisite`)
before the gate command is dispatched. Do not put provider keys in
`workers.json` or on the SSH command line.

Before capability-gated work, run `litai worker probe --all`. It probes configured SSH
workers concurrently and atomically writes `worker-observations.json` beneath the
resolved user state root. An absent `nvidia-smi` means no discoverable NVIDIA
GPU; an installed command whose device query fails is degraded and must not be treated
as GPU-capable. These observations never overwrite minimum routing requirements.

Host configuration via detect-before-install also attempts NTP synchronization to
`time.nist.gov` because skewed clocks break coding-agent TLS and tokens. A failed
attempt is logged in `host-bootstrap.json` under `clock_sync` and does not fail
sample-worker readiness.

## Run the supported matrix

The framework's sample suite defaults unpinned samples to exactly
`flavor://literate-ai/lang-python`, `flavor://literate-ai/build-make`, and the current
host OS. Explicit sample pins remain authoritative. Opt into the full supported matrix
with:

```console
make samples-platform-regression \
  TEST_CONFIG=/path/to/matrix.json SAMPLE='*' \
  SAMPLE_FLAVORS="--flavor flavor://literate-ai/os-* \
--flavor flavor://literate-ai/lang-* \
--flavor flavor://literate-ai/build-*"
```

The exact expanded Flavor selectors participate in the remote checkpoint identity, and
the persistent worker cache retains generated-source and build results between runs.
Supply `WORKER_CONFIG=/path/to/workers.json` alongside the matrix command.

For an external dispatcher, add a `kind: command` worker whose `command` is a direct
argument array. It receives one canonical request on standard input (or at the exact
`{request_file}` argument), returns one typed result, and reads credentials only through
declared environment bindings. Successful builds/tests publish an immutable,
credential-free artifact URI and exact content identity. LitAI validates this protocol;
the external command remains responsible for scheduling and provisioning.

A derived project may connect the same private matrix to its CI or task router and run
`litai rebuild` for the selected Component and platform Flavor on each worker. The
packaged CLI does not yet claim a generic derived-project fanout verb; consult
`litai help` for the commands actually supported by the installed release.
