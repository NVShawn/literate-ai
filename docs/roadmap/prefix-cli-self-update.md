# Prefix-installed CLI self-update

- **Status:** completed
- **Owning queue item:** [HOST-SELF-UPDATE-001](active-work.md#x-host-self-update-001-prefix-installed-litai-self-updates-from-github-releases)
- **Completion / archival evidence:** [HOST-SELF-UPDATE-001](active-work.md#x-host-self-update-001-prefix-installed-litai-self-updates-from-github-releases) is closed. `make python-check` 3444 tests (32 skipped) on 2026-09-05; `make install-check` on clean `03175d8d` returned isolated smoke `ok: true` with `LITAI_NO_SELF_UPDATE=1` and no GitHub Releases API contact.

# Prefix-installed CLI self-update implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A prefix-installed `litai` launcher stages a newer GitHub Release wheel in the background and, on the next enrolled invocation, installs it into the same prefix and re-execs the original argv.

**Architecture:** Keep GitHub I/O and venv mutation in `src/literate_ai/adapters/host_self_update.py`. `dispatch.main` calls `maybe_host_self_update(argv)` before argparse. The installer writes `literate-ai/host-install-manifest@2` and both launchers export `LITAI_HOST_INSTALL=1` plus `LITAI_PREFIX`. Checkouts, CI, `@1` manifests, and `LITAI_NO_SELF_UPDATE=1` stay inert.

**Tech Stack:** Python 3.11+, `urllib.request`, `packaging.version`, existing private venv `python -m pip`, POSIX/Windows launchers, unittest.

**Spec:** [ADR 0037](../decisions/0037-prefix-installed-cli-self-update.md)

## Global Constraints

- Only enrolled prefix installs (`LITAI_HOST_INSTALL=1`, matching `@2` manifest with `self_update: true`, interpreter inside that venv).
- Update source is `https://api.github.com/repos/jordanhubbard/literate-ai/releases/latest` and its wheel asset `literate_ai-<PEP440>-py3-none-any.whl`.
- Never downgrade; never use PyPI or git; never replace the live venv from the background worker.
- Fail open: warn on stderr, run the current command. GitHub timeout 10s; pip timeout 120s.
- Success cache 24h; failed-check retry 15 minutes. Foreground never waits on GitHub.
- Opt out: `LITAI_NO_SELF_UPDATE=1`, `CI`, `GITHUB_ACTIONS`, `LITAI_EVIDENCE_RUN`, `LITAI_SELF_UPDATE_REEXEC=1`.
- `litai update` remains project-file reconciliation (ADR 0007). No new subcommand.
- Tests must not hit the network. Run focused tests with `PYTHONPATH=src` then `make python-check`.
- Do not commit unless the operator asks.

---

## File map

- Create: `src/literate_ai/adapters/host_self_update.py` — enrollment, cache, GitHub selection, staging, apply, re-exec, worker.
- Create: `tests/critical/test_host_self_update.py` — hermetic coverage for the adapter.
- Modify: `src/literate_ai/adapters/user_paths.py` — add `@2` schema constant; keep `@1` for uninstall compatibility.
- Modify: `scripts/install_litai.py` — write `@2` with `self_update: true`.
- Modify: `scripts/litai-launcher`, `scripts/litai-launcher.cmd` — export host-install env.
- Modify: `src/literate_ai/cli/dispatch.py` — call `maybe_host_self_update` at the start of `main`.
- Modify: `src/literate_ai/adapters/host_uninstall.py` — accept `@1` and `@2`; delete `self-update/` staging.
- Modify: `scripts/installed_project_smoke.py` (and any sibling install-check driver) — `LITAI_NO_SELF_UPDATE=1`.
- Modify: `docs/user/installation.md`, `CHANGELOG.md`, this file's checkboxes as tasks land.

---

### Task 1: Manifest schema @2, launcher env, enrollment

**Files:**
- Modify: `src/literate_ai/adapters/user_paths.py`
- Create: `src/literate_ai/adapters/host_self_update.py`
- Create: `tests/critical/test_host_self_update.py`
- Modify: `scripts/install_litai.py`
- Modify: `scripts/litai-launcher`
- Modify: `scripts/litai-launcher.cmd`
- Modify: `tests/smoke/test_install_litai.py` if it asserts exact `@1` JSON

**Interfaces:**
- Consumes: `HostInstallLayout.for_prefix`
- Produces:
  - `HOST_INSTALL_MANIFEST_SCHEMA_V1 = "literate-ai/host-install-manifest@1"`
  - `HOST_INSTALL_MANIFEST_SCHEMA = "literate-ai/host-install-manifest@2"` (written by new installs)
  - `HOST_INSTALL_ENVIRONMENT = "LITAI_HOST_INSTALL"`
  - `PREFIX_ENVIRONMENT = "LITAI_PREFIX"`
  - `OPT_OUT_ENVIRONMENT = "LITAI_NO_SELF_UPDATE"`
  - `REEXEC_ENVIRONMENT = "LITAI_SELF_UPDATE_REEXEC"`
  - `@dataclass(frozen=True, slots=True) class HostSelfUpdateEnrollment: prefix, environment, launcher, manifest, staging_root: Path`
  - `def resolve_host_self_update_enrollment(*, environ: Mapping[str, str], executable: Path) -> HostSelfUpdateEnrollment | None`

- [x] **Step 1: Write failing enrollment tests**

```python
# tests/critical/test_host_self_update.py
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.host_self_update import resolve_host_self_update_enrollment
from literate_ai.adapters.user_paths import (
    HOST_INSTALL_MANIFEST_SCHEMA,
    HostInstallLayout,
)


def _write_manifest(layout: HostInstallLayout, *, schema: str, self_update: bool = True) -> None:
    Path(layout.environment).mkdir(parents=True, exist_ok=True)
    Path(layout.launcher).parent.mkdir(parents=True, exist_ok=True)
    Path(layout.launcher).write_text("launcher\n", encoding="utf-8")
    document = {
        "schema": schema,
        "prefix": str(layout.prefix),
        "environment": str(layout.environment),
        "launcher": str(layout.launcher),
        "self_update": self_update,
    }
    if schema.endswith("@1"):
        document.pop("self_update")
    Path(layout.manifest).write_text(
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


class HostSelfUpdateEnrollmentTests(unittest.TestCase):
    def test_enrolls_only_when_flag_manifest_and_venv_python_match(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            prefix = Path(temporary, "prefix").resolve()
            layout = HostInstallLayout.for_prefix(prefix)
            _write_manifest(layout, schema=HOST_INSTALL_MANIFEST_SCHEMA)
            python = Path(layout.environment) / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_text("python\n", encoding="utf-8")
            enrolled = resolve_host_self_update_enrollment(
                environ={
                    "LITAI_HOST_INSTALL": "1",
                    "LITAI_PREFIX": str(prefix),
                },
                executable=python,
            )
            self.assertIsNotNone(enrolled)
            self.assertEqual(enrolled.staging_root, Path(layout.application_root) / "self-update")

    def test_skips_v1_manifest_checkout_ci_and_opt_out(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            prefix = Path(temporary, "prefix").resolve()
            layout = HostInstallLayout.for_prefix(prefix)
            _write_manifest(layout, schema="literate-ai/host-install-manifest@1")
            python = Path(layout.environment) / "bin/python"
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_text("python\n", encoding="utf-8")
            self.assertIsNone(
                resolve_host_self_update_enrollment(
                    environ={"LITAI_HOST_INSTALL": "1", "LITAI_PREFIX": str(prefix)},
                    executable=python,
                )
            )
            _write_manifest(layout, schema=HOST_INSTALL_MANIFEST_SCHEMA)
            self.assertIsNone(
                resolve_host_self_update_enrollment(
                    environ={
                        "LITAI_HOST_INSTALL": "1",
                        "LITAI_PREFIX": str(prefix),
                        "LITAI_NO_SELF_UPDATE": "1",
                    },
                    executable=python,
                )
            )
            self.assertIsNone(
                resolve_host_self_update_enrollment(
                    environ={
                        "LITAI_HOST_INSTALL": "1",
                        "LITAI_PREFIX": str(prefix),
                        "CI": "true",
                    },
                    executable=python,
                )
            )
            self.assertIsNone(
                resolve_host_self_update_enrollment(
                    environ={"LITAI_PREFIX": str(prefix)},
                    executable=python,
                )
            )
            self.assertIsNone(
                resolve_host_self_update_enrollment(
                    environ={"LITAI_HOST_INSTALL": "1", "LITAI_PREFIX": str(prefix)},
                    executable=Path(sys.executable),
                )
            )
```

- [x] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=src python3 -m unittest tests.critical.test_host_self_update -v`

Expected: FAIL because `host_self_update` is missing.

- [x] **Step 3: Implement enrollment and installer/launcher writes**

`resolve_host_self_update_enrollment` returns `None` when any of: missing `LITAI_HOST_INSTALL=1`; empty/relative `LITAI_PREFIX`; `LITAI_NO_SELF_UPDATE` truthy (`1`/`true`/`yes` casefold); `CI` or `GITHUB_ACTIONS` truthy; `LITAI_EVIDENCE_RUN` or `LITAI_SELF_UPDATE_REEXEC` truthy; manifest missing, symlink in custody, not `@2`, `self_update` not true, prefix/environment/launcher mismatch; `executable.resolve()` not equal to or under `Path(layout.environment)`. Staging root is `application_root / "self-update"`.

POSIX launcher after computing `prefix`:

```sh
LITAI_PREFIX=$prefix
LITAI_HOST_INSTALL=1
export LITAI_PREFIX LITAI_HOST_INSTALL
```

Windows launcher: `set "LITAI_HOST_INSTALL=1"` next to the existing `LITAI_PREFIX` assignment.

`install_litai.py` writes schema `@2` plus `"self_update": true`.

- [x] **Step 4: Re-run tests**

Expected: PASS.

- [x] **Step 5: Do not commit unless the operator asked**

---

### Task 2: GitHub latest-release selection, cache throttle, no-downgrade

**Files:**
- Modify: `src/literate_ai/adapters/host_self_update.py`
- Modify: `tests/critical/test_host_self_update.py`

**Interfaces:**
- Consumes: `HostSelfUpdateEnrollment`, `literate_ai.version.DISTRIBUTION_VERSION`
- Produces:
  - `@dataclass(frozen=True, slots=True) class HostSelfUpdatePlan: tag, version, wheel_name, wheel_url: str`
  - `def select_github_release_wheel(payload: Mapping[str, object], *, installed_version: str) -> HostSelfUpdatePlan | None`
  - `def cache_allows_github_check(cache: Mapping[str, object], *, now: float) -> bool`
  - `SUCCESS_CACHE_SECONDS = 86400`
  - `FAILURE_RETRY_SECONDS = 900`
  - Cache document keys: `checked_at` (unix float), `ok` (bool), `tag`, `version`

- [x] **Step 1: Write failing selection/throttle tests**

Use a fixture payload shaped like GitHub's latest-release JSON: `tag_name`, `draft`, `prerelease`, `assets: [{name, browser_download_url}]`. Cases:

1. `v0.12.0` wheel selected when installed is `0.11.0`.
2. Returns `None` when tag is `0.11.0` or older (no downgrade / no equal).
3. Returns `None` when `draft` or `prerelease` is true (defense in depth; `/releases/latest` already skips them).
4. Returns `None` when no `literate_ai-*-py3-none-any.whl` asset exists.
5. `cache_allows_github_check` is false when `ok` and age < 24h, or when a staged wheel for a still-newer version exists (callers pass that as `ok` cache or a separate `staged_version`); true when last check failed and age >= 15 minutes; false when last check failed and age < 15 minutes.

Compare versions with `packaging.version.Version`. Strip a leading `v` from `tag_name`. Invalid versions return `None`.

- [x] **Step 2: Run tests; expect FAIL on missing functions**

- [x] **Step 3: Implement `select_github_release_wheel` and `cache_allows_github_check`**

Do not perform I/O in these functions.

- [x] **Step 4: Re-run tests; expect PASS**

---

### Task 3: Background worker stages a wheel without touching the live venv

**Files:**
- Modify: `src/literate_ai/adapters/host_self_update.py`
- Modify: `tests/critical/test_host_self_update.py`

**Interfaces:**
- Consumes: Task 2 types, `urllib.request` injected as a fetch callable
- Produces:
  - `def stage_github_wheel(enrollment, *, fetch, now: float, installed_version: str, environ: Mapping[str, str]) -> None`
  - `def spawn_self_update_worker(enrollment, argv_executable: Path) -> None`
  - Fetch callable: `(url: str, headers: Mapping[str, str], timeout: int) -> bytes` for JSON; a second write-to-path helper for the wheel
  - GitHub timeout 10 seconds
  - User-Agent `literate-ai`
  - If `GH_TOKEN` or `GITHUB_TOKEN` is set, send `Authorization: Bearer <token>`; never include the token in logs or cache files
  - Worker log: `enrollment.staging_root / "worker.log"`
  - Staged wheel: `enrollment.staging_root / wheel_name`
  - Cache file: `enrollment.staging_root / "cache.json"`
  - Worker lock file: `enrollment.staging_root / "worker.lock"`

- [x] **Step 1: Write failing staging tests**

1. Given a newer fixture release, `stage_github_wheel` writes `cache.json` and the wheel bytes, and does not modify `layout.environment` or `layout.launcher`.
2. If `cache_allows_github_check` is false, fetch is not called.
3. Fetch errors write `ok: false` cache and do not raise.
4. `spawn_self_update_worker` uses `subprocess.Popen` with `start_new_session=True` on POSIX (and `CREATE_NEW_PROCESS_GROUP` on Windows), stdout/stderr redirected to `worker.log`, and does not `wait()`. If `worker.lock` is already held, it does not start a second process (inject lock).

Worker argv (so the child does not need the operator command):  
`[sys.executable, "-m", "literate_ai.adapters.host_self_update", "--stage", str(enrollment.prefix)]`  
The module `if __name__ == "__main__"` path only stages; it never applies.

- [x] **Step 2: Run tests; expect FAIL**

- [x] **Step 3: Implement staging + spawn**

Use `urllib.request.Request` like `host_install._download_artifact`, timeout 10. Catch `OSError` and `urllib.error.URLError`. Atomic replace for cache and wheel (`*.tmp` then `os.replace`).

- [x] **Step 4: Re-run tests; expect PASS**

---

### Task 4: Apply staged wheel, non-blocking lock, re-exec, fail open

**Files:**
- Modify: `src/literate_ai/adapters/host_self_update.py`
- Modify: `tests/critical/test_host_self_update.py`

**Interfaces:**
- Consumes: enrollment, staged wheel, `DISTRIBUTION_VERSION`
- Produces:
  - `def maybe_host_self_update(argv: Sequence[str], *, environ: Mapping[str, str] | None = None, executable: Path | None = None, stderr: TextIO | None = None) -> None`
  - `def apply_staged_wheel(enrollment, *, argv: Sequence[str], installed_version: str, runner, exec_fn, stderr) -> None`
  - Non-blocking exclusive lock on `enrollment.staging_root / "apply.lock"`. Busy → return without applying.
  - Pip: `[str(venv_python), "-m", "pip", "--disable-pip-version-check", "install", "--upgrade", "--force-reinstall", str(wheel)]` with `timeout=120`, env from `scripts/install_litai.py:_install_environment` (or a shared helper — do not copy PYTHON* leakage). `--no-index` plus `--find-links` on the staging dir is acceptable if a lone wheel path is insufficient on that pip.
  - After pip: refresh the stable launcher from templates kept in `host_self_update.py` as `POSIX_LAUNCHER` and `WINDOWS_LAUNCHER` string constants. A unit test asserts those constants equal `scripts/litai-launcher` and `scripts/litai-launcher.cmd` bytes (the scripts remain the edited source; constants are updated in the same change). Write the launcher, chmod 0755 on POSIX, then rewrite the `@2` manifest.
  - Rewrite `@2` manifest for the same prefix.
  - Print `litai: updated PREFIX from INSTALLED to NEW` on stderr.
  - `exec_fn([str(launcher), *argv], environ | {REEXEC_ENVIRONMENT: "1"})` using `os.execvpe` on POSIX. On Windows, `subprocess.call` the `.cmd` with the same env and `raise SystemExit(returncode)` because `execv` of `.cmd` is unreliable.
  - Failures: write a warning to stderr, return, do not exec.

- [x] **Step 1: Write failing apply tests**

1. Staged newer wheel → runner called with local wheel path; `exec_fn` called with original `argv` and `LITAI_SELF_UPDATE_REEXEC=1`.
2. Equal/older staged version → no pip, no exec.
3. Lock busy → no pip, no exec.
4. Runner non-zero → warning, no exec, current files otherwise left as pip left them (do not invent rollback beyond "do not exec").
5. `maybe_host_self_update` with no enrollment → no spawn, no apply.

Inject `runner` and `exec_fn`; do not call real pip or exec in unit tests.

- [x] **Step 2: Run tests; expect FAIL**

- [x] **Step 3: Implement apply + `maybe_host_self_update` orchestration**

Order in `maybe_host_self_update`:

1. Resolve enrollment (None → return).
2. Try apply if a staged wheel is newer (non-blocking lock).
3. If still in this process (no exec), spawn worker unless worker lock is held.

- [x] **Step 4: Re-run tests; expect PASS**

---

### Task 5: Hook `dispatch.main` before argparse

**Files:**
- Modify: `src/literate_ai/cli/dispatch.py` (`main` at line 2697)
- Modify: `tests/critical/test_host_self_update.py` or a small `tests/unit/test_cli_self_update_hook.py`

**Interfaces:**
- Consumes: `maybe_host_self_update`
- Produces: every CLI invocation, including `help`/`--help`, goes through the hook

- [x] **Step 1: Write a test that patches `maybe_host_self_update` and invokes `main(["help"])`**

Assert the hook was called with `["help"]` (or the normalized argv `main` actually receives). `help` must still return 0.

- [x] **Step 2: Run test; expect FAIL (hook missing)**

- [x] **Step 3: At the top of `main`, before `_normalize_help_arguments`:**

```python
from literate_ai.adapters.host_self_update import maybe_host_self_update

raw = sys.argv[1:] if argv is None else list(argv)
maybe_host_self_update(raw)
```

Pass the same `argv` the caller provided so tests that inject `argv=` still work. Do not pass `sys.argv` when `argv` is provided.

- [x] **Step 4: Re-run; expect PASS. Smoke `PYTHONPATH=src python3 -m literate_ai.cli help` still works from a checkout (inert).**

---

### Task 6: Uninstall `@2` and remove staging

**Files:**
- Modify: `src/literate_ai/adapters/host_uninstall.py`
- Modify: `tests/critical/test_host_uninstall.py`

**Interfaces:**
- Consumes: both manifest schemas
- Produces: uninstall of `@1` (legacy four fields) and `@2` (`self_update: true`); staging dir `application_root/self-update` is removed with the application root

- [x] **Step 1: Extend tests**

Keep the existing `@1` fixture working. Add an `@2` fixture with a `self-update/cache.json` file; after uninstall the staging dir and venv are gone. A `@2` document missing `self_update` or with extra unknown keys still fails closed.

Replace `document != expected` with a field-wise check: required keys match layout; schema is `@1` or `@2`; if `@2`, `self_update is True`.

- [x] **Step 2: Run `tests.critical.test_host_uninstall`; expect FAIL on `@2`**

- [x] **Step 3: Implement dual-schema planning; rmtree staging as part of application_root cleanup (already happens if the whole application_root is removed — ensure staging does not prevent `rmdir`. Today uninstall removes launcher, rmtree environment, unlink manifest, then rmdir application_root if empty. Staging would block that rmdir. Explicitly `shutil.rmtree(application_root / "self-update")` before the empty-root rmdir, or rmtree application_root only if it contains solely owned children. Prefer: remove known owned children (venv, manifest, self-update) then rmdir if empty. Do not recursively delete unknown files in application_root.**

- [x] **Step 4: Re-run uninstall tests; expect PASS**

---

### Task 7: install-check stays GitHub-silent

**Files:**
- Modify: `scripts/installed_project_smoke.py`
- Modify: `scripts/installed_e2e_gate.py` if it invokes a prefix-installed launcher
- Modify: `scripts/installed_roundtrip.py` if applicable

**Interfaces:**
- Produces: those drivers set `environment["LITAI_NO_SELF_UPDATE"] = "1"` before invoking the installed launcher

- [x] **Step 1: Add a unit or script-level assertion** that the smoke environment includes `LITAI_NO_SELF_UPDATE=1` (extract the env-building helper if needed so a test can call it). If extracting is too large, set the variable in the existing `environment = dict(os.environ)` block around line 597 of `installed_project_smoke.py` and add a comment that ADR 0037 requires it. A focused test can read the file or test a tiny helper `_installed_launcher_environment(...)`.

- [x] **Step 2: Implement the env assignment in every driver that execs a prefix-installed `litai`.**

- [x] **Step 3: `make install-check` is evidence later; do not run it until Tasks 1–6 pass unit tests.**

---

### Task 8: Operator docs and changelog

**Files:**
- Modify: `docs/user/installation.md`
- Modify: `CHANGELOG.md` (`## Unreleased`)
- Modify: this file's owning queue item checkboxes when closing

**Interfaces:** none

- [x] **Step 1: In Installation and first run, after the default prefix layout paragraph, add that a successful `make install` enrolls that prefix for GitHub Release self-update (ADR 0037): checks run in the background; the next invocation may replace the private venv and re-exec the same argv; opt out with `LITAI_NO_SELF_UPDATE=1`; CI is skipped; `litai update` still means project files.**

- [x] **Step 2: Soften "Agents must obtain user authorization before installing or upgrading any host tool" so it still applies to compilers and package managers, while prefix self-update is standing authorization for that `make install` prefix only.**

- [x] **Step 3: Add one Unreleased changelog bullet naming prefix-installed GitHub self-update and ADR 0037.**

- [x] **Step 4: Run `make python-check` (and `make documentation-check` if docs-index validation requires it).**

---

## Spec coverage

| ADR 0037 rule | Task |
| --- | --- |
| Prefix-only identity (launcher flag + `@2` + venv python) | 1 |
| `@1` / checkout / CI / opt-out / evidence / re-exec inert | 1, 5 |
| GitHub latest non-prerelease wheel; any newer version; no downgrade | 2 |
| Background check; 24h / 15m throttle; never delay this command for GitHub | 3, 5 |
| Stage only; apply next invoke; re-exec argv | 3, 4 |
| Non-blocking lock; fail open | 4 |
| Uninstall `@2` + staging | 6 |
| install-check silent | 7 |
| Docs vs `litai update`; standing prefix authorization | 8 |
| No new subcommand | (none added) |
| Tokens never logged | 3 |

## Out of scope

PyPI, git rebuilds, pipx/Homebrew/apt/WinGet copies, a `litai self-update` verb, applying under a running command, 0.10.x backport.
