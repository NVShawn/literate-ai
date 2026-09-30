"""Provider-neutral short-lived worker-to-forge Git credential contract."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from literate_ai.contracts.execution_dispatch import (
    ExecutionWorker,
    ExecutionWorkerCatalog,
    ExecutionWorkerKind,
)
from literate_ai.security.worker_forge_credentials import (
    ForgeCredentialScope,
    GitHubAppInstallationTokenProvider,
    GitHubInstallationTokenResponse,
    GitLabCIJobTokenProvider,
    WorkerForgeCredential,
    WorkerForgeCredentialError,
    materialize_worker_source,
    worker_forge_git_environment,
)

_SECRET_TOKEN = "ghs_super-secret-installation-token"  # noqa: S105 - test fixture


def _future(seconds: int = 300) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


def _past(seconds: int = 300) -> datetime:
    return datetime.now(UTC) - timedelta(seconds=seconds)


def _git(root: Path, *arguments: str) -> None:
    environment = os.environ.copy()
    environment["GIT_AUTHOR_NAME"] = "forge-cred-test"
    environment["GIT_AUTHOR_EMAIL"] = "forge-cred-test@example.com"
    environment["GIT_COMMITTER_NAME"] = "forge-cred-test"
    environment["GIT_COMMITTER_EMAIL"] = "forge-cred-test@example.com"
    subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=True,
        capture_output=True,
        env=environment,
    )


def _bare_upstream(root: Path) -> tuple[Path, str]:
    """A bare upstream repository plus the exact revision of its one commit."""

    upstream = root / "upstream.git"
    work = root / "work"
    work.mkdir()
    _git(work, "init", "--quiet", "-b", "main")
    _git(work, "config", "user.email", "forge-cred-test@example.com")
    _git(work, "config", "user.name", "forge-cred-test")
    (work / "README.md").write_text("hello\n", encoding="utf-8")
    _git(work, "add", "README.md")
    _git(work, "commit", "--quiet", "-m", "init")
    subprocess.run(
        ("git", "clone", "--quiet", "--bare", str(work), str(upstream)),
        check=True,
        capture_output=True,
    )
    revision = subprocess.run(
        ("git", "-C", str(work), "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return upstream, revision


def _fetch_backed_runner(upstream: Path, revision: str):
    """A fake runner that performs the real, network-free local Git phases.

    It records every call (command and environment) so tests can assert the
    acquired credential's environment reached the root fetch, the submodule
    phase, and the LFS phase, without this module's ``materialize_worker_source``
    ever depending on how the phases are actually executed.
    """

    recorded: list[tuple[tuple[str, ...], dict[str, str], bool]] = []

    def runner(command, cwd, environment) -> None:  # noqa: ANN001
        askpass_existed_at_call_time = Path(environment["GIT_ASKPASS"]).exists()
        recorded.append(
            (tuple(command), dict(environment), askpass_existed_at_call_time)
        )
        if command[0:2] == ("git", "clone"):
            subprocess.run(
                ["git", "clone", "--quiet", str(upstream), str(command[-1])],
                check=True,
                capture_output=True,
            )
        elif command[0:2] == ("git", "fetch"):
            subprocess.run(
                ["git", "-C", str(cwd), "fetch", "--quiet", "origin", revision],
                check=True,
                capture_output=True,
            )
        elif command[0:2] == ("git", "checkout"):
            subprocess.run(
                ["git", "-C", str(cwd), "checkout", "--quiet", "FETCH_HEAD"],
                check=True,
                capture_output=True,
            )
        # submodule update / lfs pull are no-ops: the fixture repository has
        # neither submodules nor LFS pointers.

    return recorded, runner


class GitHubAppInstallationTokenProviderTests(unittest.TestCase):
    def test_acquires_a_scoped_credential_from_an_injected_fetcher(self) -> None:
        scope = ForgeCredentialScope("octo/example")
        expires_at = _future()
        seen: list[ForgeCredentialScope] = []

        def fetch(requested: ForgeCredentialScope) -> GitHubInstallationTokenResponse:
            seen.append(requested)
            return GitHubInstallationTokenResponse(_SECRET_TOKEN, expires_at)

        provider = GitHubAppInstallationTokenProvider("123", "456", fetch)
        credential = provider.acquire(scope)

        self.assertEqual(seen, [scope])
        self.assertEqual(credential.token, _SECRET_TOKEN)
        self.assertEqual(credential.username, "x-access-token")
        self.assertEqual(credential.provider, "github-app-installation-token")
        self.assertEqual(credential.expires_at, expires_at)
        self.assertFalse(credential.is_expired())

    def test_never_calls_a_real_network_endpoint(self) -> None:
        # The fetcher is purely local; no network module is imported or used.
        calls = {"count": 0}

        def fetch(_: ForgeCredentialScope) -> GitHubInstallationTokenResponse:
            calls["count"] += 1
            return GitHubInstallationTokenResponse(_SECRET_TOKEN, _future())

        provider = GitHubAppInstallationTokenProvider("app", "install", fetch)
        provider.acquire(ForgeCredentialScope("octo/example"))
        self.assertEqual(calls["count"], 1)

    def test_wraps_fetcher_failure_in_a_typed_diagnostic(self) -> None:
        def fetch(_: ForgeCredentialScope) -> GitHubInstallationTokenResponse:
            raise RuntimeError("simulated broker outage")

        provider = GitHubAppInstallationTokenProvider("app", "install", fetch)
        with self.assertRaises(WorkerForgeCredentialError) as ctx:
            provider.acquire(ForgeCredentialScope("octo/example"))
        self.assertEqual(ctx.exception.code, "worker.forge_credential_unavailable")

    def test_rejects_a_malformed_fetcher_response(self) -> None:
        provider = GitHubAppInstallationTokenProvider(
            "app",
            "install",
            lambda scope: "not-a-response",  # type: ignore[return-value]
        )
        with self.assertRaises(WorkerForgeCredentialError) as ctx:
            provider.acquire(ForgeCredentialScope("octo/example"))
        self.assertEqual(ctx.exception.code, "worker.forge_credential_provider_invalid")


class GitLabCIJobTokenProviderTests(unittest.TestCase):
    def test_binds_ci_job_token_from_injected_environment(self) -> None:
        expires_at = _future()
        provider = GitLabCIJobTokenProvider(
            {"CI_JOB_TOKEN": "glcbt-fake-job-token"}, lambda: expires_at
        )
        credential = provider.acquire(ForgeCredentialScope("group/project"))
        self.assertEqual(credential.token, "glcbt-fake-job-token")
        self.assertEqual(credential.provider, "gitlab-ci-job-token")
        self.assertEqual(credential.expires_at, expires_at)

    def test_fails_closed_when_no_job_token_is_present(self) -> None:
        provider = GitLabCIJobTokenProvider({}, _future)
        with self.assertRaises(WorkerForgeCredentialError) as ctx:
            provider.acquire(ForgeCredentialScope("group/project"))
        self.assertEqual(ctx.exception.code, "worker.forge_credential_unavailable")


class WorkerForgeCredentialRedactionTests(unittest.TestCase):
    def test_repr_and_str_redact_the_token(self) -> None:
        credential = WorkerForgeCredential(
            "github-app-installation-token",
            ForgeCredentialScope("octo/example"),
            _SECRET_TOKEN,
            "x-access-token",
            _future(),
        )
        self.assertNotIn(_SECRET_TOKEN, repr(credential))
        self.assertNotIn(_SECRET_TOKEN, str(credential))

    def test_requires_timezone_aware_expiry(self) -> None:
        with self.assertRaises(WorkerForgeCredentialError):
            WorkerForgeCredential(
                "github-app-installation-token",
                ForgeCredentialScope("octo/example"),
                _SECRET_TOKEN,
                "x-access-token",
                datetime.now(),  # noqa: DTZ005 - deliberately naive
            )


class WorkerForgeGitEnvironmentTests(unittest.TestCase):
    def test_yields_an_askpass_helper_that_returns_username_and_token(self) -> None:
        credential = WorkerForgeCredential(
            "github-app-installation-token",
            ForgeCredentialScope("octo/example"),
            _SECRET_TOKEN,
            "x-access-token",
            _future(),
        )
        with worker_forge_git_environment(credential) as environment:
            helper = environment["GIT_ASKPASS"]
            self.assertTrue(Path(helper).is_file())
            username = subprocess.run(
                (helper, "Username for 'https://github.com': "),
                check=True,
                capture_output=True,
                env=environment,
                text=True,
            ).stdout
            password = subprocess.run(
                (helper, "Password for 'https://x-access-token@github.com': "),
                check=True,
                capture_output=True,
                env=environment,
                text=True,
            ).stdout
            self.assertEqual(username, "x-access-token")
            self.assertEqual(password, _SECRET_TOKEN)
            helper_path = Path(helper)
        # The helper and its containing directory are removed on exit.
        self.assertFalse(helper_path.exists())
        self.assertFalse(helper_path.parent.exists())

    def test_removes_the_helper_directory_even_when_the_body_raises(self) -> None:
        credential = WorkerForgeCredential(
            "github-app-installation-token",
            ForgeCredentialScope("octo/example"),
            _SECRET_TOKEN,
            "x-access-token",
            _future(),
        )
        captured: dict[str, str] = {}
        with self.assertRaises(ValueError):
            with worker_forge_git_environment(credential) as environment:
                captured.update(environment)
                raise ValueError("simulated fetch failure")
        self.assertFalse(Path(captured["GIT_ASKPASS"]).parent.exists())

    def test_refuses_an_expired_credential_without_writing_any_file(self) -> None:
        credential = WorkerForgeCredential(
            "github-app-installation-token",
            ForgeCredentialScope("octo/example"),
            _SECRET_TOKEN,
            "x-access-token",
            _past(),
        )
        with tempfile.TemporaryDirectory() as directory:
            before = sorted(Path(tempfile.gettempdir()).glob("litai-forge-cred-*"))
            with self.assertRaises(WorkerForgeCredentialError) as ctx:
                with worker_forge_git_environment(credential):
                    self.fail("expired credential must never be used")
            self.assertEqual(ctx.exception.code, "worker.forge_credential_expired")
            after = sorted(Path(tempfile.gettempdir()).glob("litai-forge-cred-*"))
            self.assertEqual(before, after)
            self.assertEqual(os.listdir(directory), [])

    def test_never_reuses_a_credential_once_its_lifetime_elapses(self) -> None:
        expires_at = datetime.now(UTC) + timedelta(seconds=1)
        credential = WorkerForgeCredential(
            "github-app-installation-token",
            ForgeCredentialScope("octo/example"),
            _SECRET_TOKEN,
            "x-access-token",
            expires_at,
        )
        with worker_forge_git_environment(credential):
            pass  # still valid at acquisition time
        later = expires_at + timedelta(seconds=1)
        with self.assertRaises(WorkerForgeCredentialError) as ctx:
            with worker_forge_git_environment(credential, now=later):
                self.fail("credential must not be reused past its lifetime")
        self.assertEqual(ctx.exception.code, "worker.forge_credential_expired")


class MaterializeWorkerSourceTests(unittest.TestCase):
    def test_uses_the_acquired_credential_environment_for_every_git_phase(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            upstream, revision = _bare_upstream(root)
            destination = root / "worker-checkout"
            recorded, fake_runner = _fetch_backed_runner(upstream, revision)

            expires_at = _future()

            def fetch(_: ForgeCredentialScope) -> GitHubInstallationTokenResponse:
                return GitHubInstallationTokenResponse(_SECRET_TOKEN, expires_at)

            provider = GitHubAppInstallationTokenProvider("app", "install", fetch)
            scope = ForgeCredentialScope("octo/example")

            result = materialize_worker_source(
                destination,
                str(upstream),
                revision,
                provider=provider,
                scope=scope,
                runner=fake_runner,
            )

            self.assertEqual(result["phases"], ("root-fetch", "submodules", "lfs"))
            self.assertEqual(
                (destination / "README.md").read_text(encoding="utf-8"), "hello\n"
            )
            self.assertGreaterEqual(len(recorded), 4)
            for _, environment, askpass_existed in recorded:
                self.assertEqual(
                    environment["LITERATE_AI_WORKER_FORGE_TOKEN"], _SECRET_TOKEN
                )
                self.assertEqual(environment["GIT_TERMINAL_PROMPT"], "0")
                self.assertTrue(askpass_existed, "askpass helper must exist during use")
            # The credential's ephemeral files are gone once materialization returns.
            first_askpass = Path(recorded[0][1]["GIT_ASKPASS"])
            self.assertFalse(first_askpass.exists())

    def test_refuses_to_fetch_with_an_expired_credential(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            upstream, revision = _bare_upstream(root)
            destination = root / "worker-checkout"

            def fetch(_: ForgeCredentialScope) -> GitHubInstallationTokenResponse:
                return GitHubInstallationTokenResponse(_SECRET_TOKEN, _past())

            provider = GitHubAppInstallationTokenProvider("app", "install", fetch)
            calls = {"count": 0}

            def fake_runner(command, cwd, environment) -> None:  # noqa: ANN001
                calls["count"] += 1

            with self.assertRaises(WorkerForgeCredentialError) as ctx:
                materialize_worker_source(
                    destination,
                    str(upstream),
                    revision,
                    provider=provider,
                    scope=ForgeCredentialScope("octo/example"),
                    runner=fake_runner,
                )
            self.assertEqual(ctx.exception.code, "worker.forge_credential_expired")
            self.assertEqual(calls["count"], 0)
            self.assertFalse(destination.exists())


class CredentialsNeverPersistTests(unittest.TestCase):
    """Credentials must never land in project authority or the worker catalog."""

    def test_execution_worker_catalog_has_no_credential_carrying_fields(self) -> None:
        catalog = ExecutionWorkerCatalog(
            (
                ExecutionWorker(
                    "worker-1",
                    ExecutionWorkerKind.SSH,
                    endpoint="deploy@worker.example.com",
                    workspace="/srv/litai/worker-1",
                ),
            )
        )
        serialized = json.dumps(catalog.to_dict())
        self.assertNotIn(_SECRET_TOKEN, serialized)
        for worker in catalog.workers:
            payload = worker.to_dict()
            self.assertNotIn("token", payload)
            self.assertNotIn("credential", payload)
            self.assertNotIn("credentials", payload)

    def test_materializing_source_does_not_touch_an_unrelated_catalog_file(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            upstream, revision = _bare_upstream(root)
            destination = root / "worker-checkout"
            catalog_path = root / "execution-workers.json"
            catalog_path.write_text(
                json.dumps({"schema": "literate-ai/execution-worker-catalog@1"}),
                encoding="utf-8",
            )
            before = catalog_path.read_text(encoding="utf-8")

            def fetch(_: ForgeCredentialScope) -> GitHubInstallationTokenResponse:
                return GitHubInstallationTokenResponse(_SECRET_TOKEN, _future())

            provider = GitHubAppInstallationTokenProvider("app", "install", fetch)
            _, fake_runner = _fetch_backed_runner(upstream, revision)

            materialize_worker_source(
                destination,
                str(upstream),
                revision,
                provider=provider,
                scope=ForgeCredentialScope("octo/example"),
                runner=fake_runner,
                include_lfs=False,
            )

            after = catalog_path.read_text(encoding="utf-8")
            self.assertEqual(before, after)
            self.assertNotIn(_SECRET_TOKEN, after)


if __name__ == "__main__":
    unittest.main()
