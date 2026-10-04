"""Provider-neutral short-lived worker-to-forge Git credential contract."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from literate_ai.security.worker_forge_credentials import (
    ForgeCredentialScope,
    GitHubAppInstallationTokenProvider,
    GitHubInstallationTokenResponse,
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


class WorkerForgeGitEnvironmentTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
