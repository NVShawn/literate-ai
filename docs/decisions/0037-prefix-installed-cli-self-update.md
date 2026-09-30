# ADR 0037: Prefix-Installed `litai` Detects GitHub Releases and Self-Updates

- Status: Accepted
- Date: 2026-09-05
- Accepted: 2026-09-05 (operator direction after Proposed review)
- Decision owners: literate-ai maintainers
- Release target: 0.11.0
- Roadmap: [HOST-SELF-UPDATE-001](../roadmap/active-work.md#x-host-self-update-001-prefix-installed-litai-self-updates-from-github-releases)
- Relates: [ADR 0007](0007-update-classification-and-semantic-merge.md) (`litai update`
  remains project-file reconciliation), [ADR 0030](0030-executable-cross-layer-authority.md)
  (host-path policy), [ADR 0032](0032-tuple-specific-native-install-sboms.md)
  (host-install layout), [installation](../user/installation.md)

## Context

Operators install a stable `litai` launcher with `make install` / `PREFIX`. That
installer writes a private venv under the prefix (`share/literate-ai/venv`) and a
prefix-relative launcher (`bin/litai` or `bin/litai.cmd`). The launcher `exec`s the
venv entry point. Replacing the CLI today means running `make install` again from a
newer checkout, or installing a published wheel by hand. There is no in-command path
that notices a newer GitHub Release and replaces that same prefix.

`litai update` already exists and means something else: reconcile inherited or
framework-owned **project** files. It must not become the host-CLI upgrader.

Checkout, Make session venvs, `PYTHONPATH=src`, editable installs, and CI must not
rewrite a developer tree or a hosted runner. Installation docs currently require
authorization before installing or upgrading host tools; a silent upgrade of an
unrelated compiler would violate that. Completing `make install` into a prefix is a
narrower, already-authorized mutation of that same prefix.

The 0.10.0 host-install manifest (`literate-ai/host-install-manifest@1`) is an exact
four-field document. `make uninstall` compares the file for byte-level equality with
those four fields, so any self-update identity added to the manifest has to be an
intentional schema change, not an ignored extra key.

## Decision

### Who may self-update

Only a **prefix-installed** launcher participates. All of the following must hold:

1. The stable launcher exported `LITAI_HOST_INSTALL=1` and an absolute `LITAI_PREFIX`
   before `exec`.
2. `share/literate-ai/install.json` exists for that prefix, is a bounded regular file
   with no symlink in the custody path, and matches schema
   `literate-ai/host-install-manifest@2` with `self_update` enabled, plus the existing
   prefix / environment / launcher fields.
3. The running interpreter is the private venv interpreter named by that manifest.

If any check fails, self-update is inert: no network, no worker, no re-exec.

Checkout, `PYTHONPATH=src`, Make session venvs, editable installs, pip/pipx/Homebrew/
apt/WinGet-owned copies, and CI are out of scope. Native package managers remain the
upgrade path for those installs.

Existing `@1` manifests do not enroll. The first self-updating version is installed
with `make install` (or an equivalent installer that writes `@2` and the new launcher).
After that, each applied GitHub update refreshes the launcher and manifest from the
new distribution so enrollment persists.

### Authorization and opt-out

Completing `make install` / `PREFIX` install is standing authorization to replace
**that same prefix** later. It is not authorization to install unrelated host tools.

Operators opt out with `LITAI_NO_SELF_UPDATE=1`. Self-update is also skipped when
`CI` or `GITHUB_ACTIONS` is set, when `LITAI_EVIDENCE_RUN` is set, and when the
process is already the post-update child (`LITAI_SELF_UPDATE_REEXEC=1`).

`make install-check` and any test prefix export `LITAI_NO_SELF_UPDATE=1` so the
install-contract probe never talks to GitHub or replaces the fixture venv.

### Channel

The only update source is GitHub Releases for the exact safe GitHub owner/repository
embedded in the installed wheel's immutable distribution-origin evidence.

- Use the latest published, non-prerelease, non-draft `v*` release that includes a
  wheel asset whose name matches `literate_ai-<PEP440>-py3-none-any.whl`.
- Any newer PEP 440 version replaces the prefix, including a new minor or major.
- Never downgrade. If the installed version is greater than or equal to that latest
  release, do nothing.
- Do not use PyPI, do not clone git, and do not rebuild from a checkout.
- If `GH_TOKEN` or `GITHUB_TOKEN` is set, send it as GitHub authorization. Never log
  the token.

### This invocation never waits on GitHub

A prefix-installed invocation always runs the **current** install for its own argv,
except when a previously staged wheel is ready to apply (below).

At start, if enrolled and not opted out, it may spawn a **detached background worker**.
The foreground command does not wait for that worker. The worker:

- no-ops if a successful check, or a staged wheel for a still-newer release, is
  younger than 24 hours;
- retries no sooner than 15 minutes after a failed check;
- queries GitHub with a 10-second timeout;
- writes a cache record under the prefix application root
  (`share/literate-ai/self-update/`);
- may download the wheel into that staging directory;
- never replaces the live venv, launcher, or manifest.

Worker stdout/stderr go to a file in that staging directory, not to the operator
command's stdout.

### Apply on the next invocation, then re-exec

On the **next** enrolled invocation, if a staged wheel is newer than the running
version:

1. Take an exclusive lock on the prefix self-update directory. If the lock is busy,
   skip apply and run as-is (fail open).
2. Install the staged wheel into the existing private venv with that venv's
   `python -m pip` from the local file only (no index).
3. Refresh the stable launcher from the newly installed distribution and rewrite
   `install.json` as `@2` for the same prefix.
4. Print the old → new version on stderr (never on stdout, so `--json` envelopes stay
   intact).
5. Re-exec the stable launcher with this invocation's original argv, setting
   `LITAI_SELF_UPDATE_REEXEC=1` so the child neither checks nor applies again.

If apply fails, warn on stderr and continue with the current install. Do not re-exec.

The original request to "remember argv and re-invoke after update" is this next-
invocation apply-and-re-exec. The invocation that merely staged the wheel always ran
on the old code, by design, so a running command cannot have its venv replaced
underneath it.

### Concurrency

The same exclusive lock covers apply. A second invocation that loses the lock runs on
the current install. A second worker is not started when a worker lock is already
held. Lock wait does not delay the operator command.

### Failure policy

Fail open. Unreachable GitHub, rate limits, missing wheels, unparseable versions,
timeouts, pip failures, and lock contention warn on stderr and run the current
command. Self-update never becomes the reason `litai` cannot start.

GitHub I/O is capped at 10 seconds. Pip apply is capped at 120 seconds. A hung apply
aborts, leaves the previous venv in place if pip did not finish, warns, and continues.
If pip finished but launcher or manifest refresh failed, warn and do not re-exec;
the next eligible invocation may retry.

### Naming and commands

`litai update` is unchanged: project-file reconciliation (ADR 0007). This ADR adds no
required new subcommand. There is no `litai self-update` in the first cut.

### Uninstall and layout

`make uninstall` must:

- accept `literate-ai/host-install-manifest@2` (not require exact `@1` four-field
  equality);
- remove the self-update staging directory as part of the owned application root;
- keep refusing symlinks, foreign files, and filesystem-root prefixes.

Config, state, and unrelated caches remain operator-owned and are not removed.

### Tests

Unit tests (no live GitHub) cover: enrollment gates; opt-out and CI skip; no-downgrade;
latest-release wheel selection from a fixture payload; 24-hour success throttle and
faster failure retry; staging without venv mutation; apply-then-re-exec argv and
recursion guard; fail-open on network/pip/lock errors; uninstall of `@2` plus staging
dir. `make install-check` remains GitHub-silent.

## Consequences

Prefix-installed operators receive newer published wheels without a second `make
install`, including across minor and major GitHub releases. The first hour after a
release still runs the previously installed code; the following invocation applies the
staged wheel and re-execs.

This amends the installation authorization rule: `make install` is standing permission
to refresh that prefix from GitHub Releases. Unrelated host-tool installation remains
explicit. Documentation must say so, and must distinguish this path from `litai
update`.

Installs that are not prefix-enrolled (checkouts, CI, package-manager copies, leftover
`@1` manifests) stay on today's manual upgrade path until the operator runs the new
installer once.

The GitHub unauthenticated API budget is protected by the 24-hour success cache. Forks
and private mirrors are not update sources. PyPI is not an update source.

This ADR was accepted on 2026-09-05. Implementation is queued as HOST-SELF-UPDATE-001.
