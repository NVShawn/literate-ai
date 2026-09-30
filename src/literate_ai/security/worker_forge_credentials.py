"""Provider-neutral short-lived worker-to-forge Git credential contract.

Exact-revision worker source materialization (see
``literate_ai.adapters.source_materialization`` and
``literate_ai.adapters.parent_checkout``) requires the fetching process to
authenticate to the source forge for the root fetch, recursive submodule
fetches, and Git LFS hydration. A heterogeneous, dynamically created worker
fleet cannot share one long-lived personal access token or a permanent SSH
key registered per host: both preserve a shared-secret blast radius and
create key-lifecycle/revocation debt.

This module supplies a narrowly scoped, short-lived credential that a worker
acquires just in time for one bounded checkout operation, uses only through
an ephemeral ``GIT_ASKPASS`` helper (never a clone URL, ``.git/config``,
command argument, log line, result envelope, checkpoint, or evidence
manifest), and discards on both success and failure. It is never written to
project authority files or the persistent worker catalog
(``literate_ai.contracts.execution_dispatch.ExecutionWorkerCatalog``); those
stay describable and diffable without ever containing secret material.

This is distinct from controller-to-worker SSH transport authentication
(``ExecutionWorker.transport``), which authenticates the controller to a
worker host, not a worker to the source forge.
"""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

_ASKPASS_HELPER_POSIX = """#!/bin/sh
# Ephemeral GIT_ASKPASS helper for one bounded worker-to-forge fetch.
# Deliberately does not echo the prompt or the token to any log.
case "$1" in
    *[Pp]assword*) printf '%s' "$LITERATE_AI_WORKER_FORGE_TOKEN" ;;
    *) printf '%s' "$LITERATE_AI_WORKER_FORGE_USERNAME" ;;
esac
"""

# Git spawns GIT_ASKPASS directly as argv[0], never through a shell, so a
# POSIX "#!/bin/sh" text file is not executable on Windows (WinError 193).
# ``findstr``/``set /p`` avoid an extra trailing newline on the emitted value,
# matching the POSIX helper's ``printf`` behavior.
_ASKPASS_HELPER_WINDOWS = """@echo off
setlocal
set "prompt=%~1"
set "stripped=%prompt:assword=%"
if "%stripped%"=="%prompt%" (
    <nul set /p ="%LITERATE_AI_WORKER_FORGE_USERNAME%"
) else (
    <nul set /p ="%LITERATE_AI_WORKER_FORGE_TOKEN%"
)
exit /b 0
"""

_TOKEN_ENV = "LITERATE_AI_WORKER_FORGE_TOKEN"
_USERNAME_ENV = "LITERATE_AI_WORKER_FORGE_USERNAME"


class WorkerForgeCredentialError(RuntimeError):
    """A typed, non-secret diagnostic for worker-to-forge credential failures."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class ForgeCredentialScope:
    """The exact repository and access level a credential must be bound to."""

    repository: str
    read_only: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.repository, str) or not self.repository.strip():
            raise WorkerForgeCredentialError(
                "worker.forge_credential_scope_invalid",
                "credential scope requires a nonempty repository",
            )


@dataclass(frozen=True, slots=True)
class WorkerForgeCredential:
    """A short-lived worker-to-forge credential.

    Never persisted, never logged. ``__repr__`` and ``__str__`` redact the
    token so an accidental log/print/exception message cannot leak it.
    """

    provider: str
    scope: ForgeCredentialScope
    token: str
    username: str
    expires_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.token, str) or not self.token:
            raise WorkerForgeCredentialError(
                "worker.forge_credential_invalid",
                "credential token must be nonempty",
            )
        if not isinstance(self.username, str) or not self.username:
            raise WorkerForgeCredentialError(
                "worker.forge_credential_invalid",
                "credential username must be nonempty",
            )
        if self.expires_at.tzinfo is None:
            raise WorkerForgeCredentialError(
                "worker.forge_credential_invalid",
                "credential expiry must be timezone-aware",
            )

    def is_expired(self, *, now: datetime | None = None) -> bool:
        current = now if now is not None else datetime.now(UTC)
        return current >= self.expires_at

    def _redacted(self) -> str:
        return (
            f"WorkerForgeCredential(provider={self.provider!r}, "
            f"scope={self.scope!r}, username={self.username!r}, "
            "token='***redacted***', "
            f"expires_at={self.expires_at.isoformat()!r})"
        )

    def __repr__(self) -> str:
        return self._redacted()

    def __str__(self) -> str:
        return self._redacted()


class WorkerForgeCredentialProvider(Protocol):
    """Provider-neutral just-in-time acquisition of a worker-to-forge credential."""

    def acquire(self, scope: ForgeCredentialScope) -> WorkerForgeCredential:
        """Acquire one narrowly scoped, short-lived credential bound to ``scope``."""
        ...


@dataclass(frozen=True, slots=True)
class GitHubInstallationTokenResponse:
    """The bounded shape a GitHub App installation-token fetcher must return."""

    token: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class GitHubAppInstallationTokenProvider:
    """Acquire a read-only GitHub App installation token per checkout.

    ``token_fetcher`` is injected so production code can bind it to a real
    GitHub App JWT exchange while tests bind it to a deterministic fake --
    this module never performs the network call itself.
    """

    app_id: str
    installation_id: str
    token_fetcher: Callable[[ForgeCredentialScope], GitHubInstallationTokenResponse]

    def acquire(self, scope: ForgeCredentialScope) -> WorkerForgeCredential:
        if not isinstance(scope, ForgeCredentialScope):
            raise WorkerForgeCredentialError(
                "worker.forge_credential_scope_invalid",
                "GitHub App token acquisition requires a ForgeCredentialScope",
            )
        try:
            response = self.token_fetcher(scope)
        except WorkerForgeCredentialError:
            raise
        except Exception as exc:  # noqa: BLE001 - re-typed below, never logged raw
            raise WorkerForgeCredentialError(
                "worker.forge_credential_unavailable",
                "GitHub App installation token acquisition failed",
            ) from exc
        if not isinstance(response, GitHubInstallationTokenResponse):
            raise WorkerForgeCredentialError(
                "worker.forge_credential_provider_invalid",
                "GitHub App token fetcher must return a "
                "GitHubInstallationTokenResponse",
            )
        return WorkerForgeCredential(
            provider="github-app-installation-token",
            scope=scope,
            token=response.token,
            username="x-access-token",
            expires_at=response.expires_at,
        )


@dataclass(frozen=True, slots=True)
class GitLabCIJobTokenProvider:
    """Bind the native ``CI_JOB_TOKEN`` GitLab supplies to a running CI job.

    The token is scoped by GitLab to the running job and its declared
    allowlisted projects; it is never written anywhere by this provider. Its
    lifetime tracks the job's own bounded deadline, supplied by
    ``job_expires_at`` since GitLab does not expose a token TTL directly.
    """

    environment: Mapping[str, str]
    job_expires_at: Callable[[], datetime]

    def acquire(self, scope: ForgeCredentialScope) -> WorkerForgeCredential:
        token = self.environment.get("CI_JOB_TOKEN")
        if not token:
            raise WorkerForgeCredentialError(
                "worker.forge_credential_unavailable",
                "GitLab CI job token is not available in this environment",
            )
        return WorkerForgeCredential(
            provider="gitlab-ci-job-token",
            scope=scope,
            token=token,
            username="gitlab-ci-token",
            expires_at=self.job_expires_at(),
        )


@contextmanager
def worker_forge_git_environment(
    credential: WorkerForgeCredential,
    *,
    now: datetime | None = None,
) -> Iterator[dict[str, str]]:
    """Yield a Git process environment bound to one credential for one fetch.

    Delivers the secret through a private ``GIT_ASKPASS`` helper script and
    an environment mapping scoped to this ``with`` block only -- never a
    clone URL, never ``.git/config``, never a command argument. The helper
    script and its containing directory are removed on both success and
    failure. An already-expired credential is refused before any file is
    written.
    """

    if not isinstance(credential, WorkerForgeCredential):
        raise WorkerForgeCredentialError(
            "worker.forge_credential_invalid",
            "worker forge git environment requires a WorkerForgeCredential",
        )
    if credential.is_expired(now=now):
        raise WorkerForgeCredentialError(
            "worker.forge_credential_expired",
            "worker-to-forge credential has expired and cannot be used",
        )
    directory = Path(tempfile.mkdtemp(prefix="litai-forge-cred-"))
    try:
        os.chmod(directory, stat.S_IRWXU)
        if os.name == "nt":
            helper = directory / "askpass.cmd"
            helper.write_text(_ASKPASS_HELPER_WINDOWS, encoding="utf-8")
        else:
            helper = directory / "askpass.sh"
            helper.write_text(_ASKPASS_HELPER_POSIX, encoding="utf-8")
            helper.chmod(stat.S_IRWXU)
        environment = {
            "GIT_ASKPASS": str(helper),
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "Never",
            _TOKEN_ENV: credential.token,
            _USERNAME_ENV: credential.username,
        }
        yield environment
    finally:
        _remove_tree(directory)


def _remove_tree(directory: Path) -> None:
    for entry in sorted(directory.rglob("*"), key=lambda item: -len(item.parts)):
        try:
            if entry.is_file() or entry.is_symlink():
                entry.unlink(missing_ok=True)
            else:
                entry.rmdir()
        except OSError:
            pass
    try:
        directory.rmdir()
    except OSError:
        pass


_DEFAULT_TIMEOUT_SECONDS = 120

GitRunner = Callable[[Sequence[str], Path, Mapping[str, str]], None]


def _default_runner(
    command: Sequence[str], cwd: Path, environment: Mapping[str, str]
) -> None:
    completed = subprocess.run(
        list(command),
        cwd=cwd,
        env=dict(environment),
        capture_output=True,
        timeout=_DEFAULT_TIMEOUT_SECONDS,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise WorkerForgeCredentialError(
            "worker.forge_fetch_failed",
            detail or f"git failed: {' '.join(command)}",
        )


def materialize_worker_source(
    destination: Path,
    repository_url: str,
    revision: str,
    *,
    provider: WorkerForgeCredentialProvider,
    scope: ForgeCredentialScope,
    now: datetime | None = None,
    runner: GitRunner | None = None,
    include_lfs: bool = True,
) -> dict[str, object]:
    """Fetch one exact revision using a credential acquired just in time.

    Covers the root fetch, recursive submodule fetches, and (when
    ``include_lfs``) Git LFS hydration under the same explicit, bounded
    credential lifecycle: the credential is acquired, used only through the
    ``GIT_ASKPASS`` environment for every phase, and discarded -- files and
    environment bindings removed -- immediately after, on both success and
    failure. Nothing here writes to project authority files or a worker
    catalog.
    """

    if not isinstance(scope, ForgeCredentialScope):
        raise WorkerForgeCredentialError(
            "worker.forge_credential_scope_invalid",
            "materialization requires a ForgeCredentialScope",
        )
    run = runner or _default_runner
    try:
        credential = provider.acquire(scope)
    except WorkerForgeCredentialError:
        raise
    except Exception as exc:  # noqa: BLE001 - re-typed, never logs the raw cause
        raise WorkerForgeCredentialError(
            "worker.forge_credential_unavailable",
            "worker-to-forge credential acquisition failed",
        ) from exc

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    phases: list[str] = []
    with worker_forge_git_environment(credential, now=now) as environment:
        run(
            (
                "git",
                "clone",
                "--no-checkout",
                "--depth",
                "1",
                repository_url,
                str(destination),
            ),
            destination.parent,
            environment,
        )
        phases.append("root-fetch")
        run(
            ("git", "fetch", "--depth", "1", "origin", revision),
            destination,
            environment,
        )
        run(("git", "checkout", "--quiet", "FETCH_HEAD"), destination, environment)
        run(
            (
                "git",
                "submodule",
                "update",
                "--init",
                "--recursive",
                "--depth",
                "1",
            ),
            destination,
            environment,
        )
        phases.append("submodules")
        if include_lfs:
            run(("git", "lfs", "pull"), destination, environment)
            phases.append("lfs")
    return {
        "schema": "literate-ai/worker-forge-source-materialization@1",
        "provider": credential.provider,
        "repository": scope.repository,
        "revision": revision,
        "destination": str(destination),
        "phases": tuple(phases),
    }


__all__ = [
    "ForgeCredentialScope",
    "GitHubAppInstallationTokenProvider",
    "GitHubInstallationTokenResponse",
    "GitLabCIJobTokenProvider",
    "GitRunner",
    "WorkerForgeCredential",
    "WorkerForgeCredentialError",
    "WorkerForgeCredentialProvider",
    "materialize_worker_source",
    "worker_forge_git_environment",
]
