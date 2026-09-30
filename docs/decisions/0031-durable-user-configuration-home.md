# ADR 0031: Move Durable User Configuration Out of Project Trees

- Status: Accepted
- Date: 2026-09-01 (accepted by operator direction after discovery of the
  project-root worker catalog)
- Decision owners: literate-ai maintainers
- Release target: 0.9.0
- Roadmap: [USER-CONFIG-MIGRATION-001](../roadmap/active-work.md#x-user-config-migration-001-move-08x-private-assets-to-user-configuration-home)
- Amends: [ADR 0017](0017-explicit-live-test-coding-cli.md) ignored test-config
  location, [ADR 0021](0021-operator-local-mcp-catalog.md) application-directory
  name, and [ADR 0023](0023-mutagenic-cli-channel-fan-out.md) event-journal custody

## Context

Literate AI 0.8.x places durable, private, user-owned files in a project checkout:
`literate.workers.json`, `literate.test.json`, and
`literate.worker-observations.json`. Git ignores them, but ignoring a file does not
make the checkout an appropriate configuration store. A fresh clone cannot discover
them, multiple projects encourage duplicated endpoint/access configuration, repository
cleanup can remove them, and ordinary tree inspection can accidentally encounter
private routing data.

The operator MCP catalog already lived outside the checkout in 0.8.x, but under the
legacy `$HOME/.config/litai` migration-input path; mutable channel-event journals
shared that configuration directory. This created two application-directory names
and conflated authored configuration with changing state.

The worker catalog supplied during the 0.9.0 investigation made the lifecycle mistake
concrete. This ADR corrects custody without committing, printing, or silently copying
private configuration.

## Decision

### One user configuration root

Durable user configuration lives beneath one centrally resolved root:

- Linux and macOS: absolute `LITAI_CONFIG_DIR` when set; otherwise absolute
  `XDG_CONFIG_HOME/literate-ai`; otherwise `$HOME/.config/literate-ai`.
- Windows: absolute `LITAI_CONFIG_DIR` when set; otherwise the current user's Roaming
  AppData Known Folder plus `literate-ai`. The implementation resolves the Known Folder
  behind the platform adapter rather than hardcoding a drive or trusting the current
  working directory.

macOS deliberately follows the project's POSIX CLI convention here rather than
`~/Library/Application Support`. Cache, data, state, temporary, installation, and
target-machine paths remain separate semantic policies under ADR 0030.

The root contains:

- `workers.json`: the user's reusable execution-worker catalog;
- `mcps.json`: the user's reusable operator MCP catalog; and
- `projects/<project-id>/test.json`: the project-specific live-test matrix and
  coding-CLI/model selection, keyed by the validated `project_id` because sample IDs and
  defaults are project authority-relative.

Explicit CLI paths remain highest precedence, followed by the existing environment
overrides, then these defaults. A relative override is invalid. Public example files
remain checked into each project and are never migrated as private configuration.

### Keep changing state out of configuration

`literate.worker-observations.json` and channel-event journals are durable mutable
state, not authored configuration. A companion user-state resolver owns them outside
the checkout and outside the configuration root. Their final Linux/macOS/Windows state
roots are implemented through ADR 0030's host-path policy. Configuration migration may
move them in the same transaction, but it must preserve the distinction.

### Provide an explicit 0.8.x migration

Ship `litai config migrate` backed by a deterministic Python migration service. It is
dry-run by default; `--apply` performs the move. The service detects:

- project-root `literate.workers.json`;
- project-root `literate.test.json`;
- project-root `literate.worker-observations.json`;
- legacy 0.8.x `$HOME/.config/litai/mcps.json`; and
- legacy 0.8.x `$HOME/.config/litai/events/`.

Detection validates the existing schema/contract, rejects symlinks and non-regular
files, and reports source/destination/kind without file contents, endpoints,
credentials, or channel payloads. Apply creates private directories/files, stages on
the destination filesystem, verifies the staged bytes and parsed contract, atomically
publishes, and removes the legacy source only after the destination is durable.

The migration never overwrites. An existing byte-identical destination is an
idempotent success and permits removal of the validated legacy duplicate; a differing
destination is a typed conflict and leaves both sides untouched. Partial failure keeps
the original and reports exactly which item did not move. The legacy project-root
ignore entries remain through 0.9.x as a safety net against accidental commits.

### Fail closed instead of silently falling back forever

The 0.9.0 resolvers use only explicit overrides or new default paths. If a new default
is absent and a recognized 0.8.x asset exists, they raise a typed migration-required
diagnostic naming `litai config migrate --apply`; they do not silently continue using
the checkout or mutate configuration during a read-only command.

`litai init`, validation, help, initialized skills, and documentation report the new
locations. They do not create private files from examples automatically.

## Consequences

Private worker access details and test selections survive clones and repository cleanup
and are available consistently to CLI and MCP adapters. Mutable observations and event
journals no longer masquerade as config. Projects retain checked-in inert examples and
can still select an arbitrary absolute path for automation.

The 0.9.0 change is intentionally visible: an unmigrated 0.8.x project fails with a
specific recovery command rather than appearing unconfigured or continuing a deprecated
lookup. Tests require dry-run purity, idempotence, conflict preservation, symlink and
relative-path rejection, simulated Linux/macOS/Windows roots, project-ID isolation,
permission handling where supported, and no secret-bearing values in output.
