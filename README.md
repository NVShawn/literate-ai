# Literate AI 0.4.1 (historical release marker)

Literate AI 0.4.1 was released on 2026-08-18 from the former repository
`NVIDIA-dev/literate-ai` (now archived), at commit `7e58e549a7be4f75c019788e1e4233a08ee0fec6`.

That repository's history was not carried into
[jordanhubbard/literate-ai](https://github.com/jordanhubbard/literate-ai), which starts from a
sanitized snapshot of the 1.1 development line. This tag marks the release so that version
history and changelog references stay coherent; it does not contain the 0.4.1 source.

Install a current release from
[jordanhubbard/literate-ai releases](https://github.com/jordanhubbard/literate-ai/releases).

---

## 0.4.1 - 2026-08-18

- SSH lifecycle dispatch now sends Windows PowerShell receivers directly instead of
  requiring an unavailable remote Bash, while POSIX workers retain login-shell PATH
  semantics. GPU worker results also preserve exact human-readable model names such as
  `NVIDIA RTX PRO 4500 Blackwell Generation`, so a completed remote lifecycle is no
  longer rejected by the portable-scalar validator. Invalid remote contracts now name
  the exact failing field. (#126, #127)

- Repository-lineage fetches now use a content-identified bounded policy: 300 seconds
  total, 180 seconds without forced Git progress, and 30 seconds for one
  non-interactive SSH connection by default. `init`, `update`, and `reparent` accept
  validated bounded overrides; results and actionable timeout diagnostics identify the
  policy and provenance, while exact-revision authority, process-tree cleanup, and
  cache-independent shallow fetches remain unchanged. (#122)

- Bounded tool execution now terminates post-exit descendants that retain inherited
  output streams, drains the resulting EOF, and preserves the completed direct tool's
  status and bounded output. Repository-lineage Git/SSH fetches no longer fail merely
  because a transport helper outlives Git. (#120)

- Added `litai perf show --dir PATH` / `litai perf chart --dir PATH`, letting either
  command report on an exact directory of archived `*.jsonl` span files directly
  instead of resolving `OBJ_DIR` through `--project`. `OBJ_DIR` is disposable and
  wiped by `make clean`/`really-clean`, so this is what lets a copy of
  `OBJ_DIR/.litai/perf` made before a clean stay reportable afterward.

- Fixed SSH-dispatched remote commands (worker probes, `release check --target
  local`, build/test/run dispatch) running under a non-login, non-interactive
  remote shell, which silently drops any `PATH`/toolchain setup a worker only
  performs in `.bash_profile`/`.bashrc` (a real fleet worker hit this as a spurious
  "no compatible Python found in PATH" failure despite the tool being installed).
  `ssh_arguments()` now wraps the remote command as `bash -lic '...'`, forcing both
  login and interactive shell semantics on the remote end regardless of how sshd
  would otherwise invoke the worker's default shell -- no worker-side dotfile
  changes required.

- Fixed remote accepted-source restore from a fresh workspace when admission used an
  inherited IDE session. Dispatch now discovers the non-secret provider/tool identity
  from verified transported cache membership, binds it into request authority, and lets
  workers reproduce the admitted key without session credentials or a generator
  transport. Missing membership and mixed providers fail closed, while worker ambient
  provider selection can no longer turn an exact copied cache into
  `source_cache.runtime_absent`. (#104)

- Fixed `standard_command.entrypoint_cardinality` unconditionally rejecting any
  Standard executable Component that declares zero entrypoints, which made
  pure-library/capability-only Components unbuildable and forced projects to
  hand-author synthetic CLI entrypoints purely to satisfy the framework (#114,
  confirmed independently by a synthetic telemetry-dashboard fixture). A
  Component with no declared entrypoint now builds normally; its `test`/`execute`
  phases run the language's own dependency-free test discovery
  (`unittest discover`/`node --test`) directly against generated source instead of
  the single-shot `__main__` self-check dispatch used for CLI entrypoints, and
  independent project acceptance exempts it the same way it already exempts a
  non-`portable-application` entrypoint (#32). Declaring two or more entrypoints on
  one Component is still rejected; see #114/#115 for the fuller Component-subclass
  architecture this is a narrower, real step toward. Native/compiled languages
  without a stdlib test runner fail closed with a typed
  `standard_command.library_native_unsupported` error rather than silently doing
  nothing.

- Fixed `make install`/`scripts/install_litai.py` reporting a working tree with
  uncommitted or untracked changes as an opaque "installation command 'python' failed
  with status 1" wrapped around a buried pip/build-backend traceback. The underlying
  refusal (a distribution build from Git requires a clean, exact revision) was already
  correct; the installer now checks the same condition itself, first, and fails with a
  clear, actionable message naming the changed files before attempting the build.

- Synced the root `README.md` with 0.4.0: added `CMake` to the build-system Flavor list
  (missed when `build-cmake` landed), documented `litai release check`'s `--target
  local|github|gitlab` choice, and linked the published manager/engineering overview
  deck near the top.

- `litai release check --target local` now fans the declared gate out in parallel to
  every non-Windows SSH worker configured in `literate.workers.json` instead of
  deterministically picking just one; every dispatched worker must pass, and a failure
  names the first failing worker deterministically. Windows workers are reported as
  excluded with a typed reason (`release.windows_gate_unsupported`) rather than
  silently vanishing from the fleet, since the declared gate (`make release-check`) has
  no Windows-native equivalent yet — Windows CI in this project already avoids `make`
  entirely for the same reason.

- Added `litai perf`, structured per-step performance telemetry recorded as a standard
  part of every `litai` invocation: schema, run id, stage, target kind/id, coding CLI,
  model, start/end timestamps, duration, and pass/fail outcome, written as JSON-Lines
  under the disposable `OBJ_DIR/.litai/perf/` directory (diagnostic, not authority — a
  read-only or missing build root never fails the run it observes). A `--target local`
  fan-out additionally records one span per dispatched worker in the same log. `litai
  perf show [--group-by stage|target_id|target_kind|coding_cli|model|run_id] [--stage
  ...] [--run-id ...]` prints an aggregated table (count/failed/total/mean/min/max);
  `litai perf chart --output FILE.svg` renders a dependency-free SVG bar chart with no
  new third-party graphing library.
