from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from collections.abc import Mapping
from pathlib import Path

from literate_ai.adapters.source import (
    CommandResult,
    GitSourceAdapter,
    GitSourceError,
    SubprocessCommandRunner,
)
from literate_ai.contracts import VerificationResult
from literate_ai.sources import (
    GitSignatureVerification,
    QuarantineError,
    QuarantineStore,
)
from literate_ai.storage import FileSystemCAS


class FakeRunner:
    def __init__(
        self,
        results: Mapping[tuple[str, ...], tuple[int, str, str]],
    ) -> None:
        self.results = dict(results)
        self.calls: list[tuple[str, ...]] = []

    def run(
        self,
        args: tuple[str, ...],
        *,
        cwd: Path,
        timeout_seconds: int,
    ) -> CommandResult:
        del cwd, timeout_seconds
        self.calls.append(args)
        try:
            returncode, stdout, stderr = self.results[args]
        except KeyError as exc:
            raise AssertionError(f"unexpected command: {args!r}") from exc
        return CommandResult(args, returncode, stdout, stderr)


def git_results(
    root: Path, *, status: str = ""
) -> dict[tuple[str, ...], tuple[int, str, str]]:
    commit = "0123456789abcdef0123456789abcdef01234567"
    return {
        ("git", "rev-parse", "--show-toplevel"): (0, f"{root}\n", ""),
        ("git", "rev-parse", "HEAD"): (0, f"{commit}\n", ""),
        ("git", "symbolic-ref", "--short", "-q", "HEAD"): (0, "main\n", ""),
        (
            "git",
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        ): (0, status, ""),
        ("git", "submodule", "status", "--recursive"): (
            0,
            " abcdef0123456789abcdef0123456789abcdef01 vendor/lib (heads/main)\n",
            "",
        ),
        ("git", "ls-files", "-z", "--cached"): (
            0,
            "tracked.py\0vendor/lib\0",
            "",
        ),
        ("git", "verify-commit", "--raw", commit): (0, "", "good"),
        ("git", "log", "-1", "--format=%G?%x00%GF%x00%GS", commit): (
            0,
            "G\0ABCDEF1234\0Alice Example\n",
            "",
        ),
    }


class GitAndQuarantineTests(unittest.TestCase):
    def _capture(self, root: Path, *, status: str = ""):
        runner = FakeRunner(git_results(root, status=status))
        adapter = GitSourceAdapter(runner)
        return adapter, runner, adapter.capture(root)

    def test_dirty_git_capture_is_never_covered_by_commit_signature(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "tracked.py").write_text("VALUE = 2\n", encoding="utf-8")
            adapter, runner, capture = self._capture(root, status=" M tracked.py\0")
            verification = adapter.verify_signed_commit(root, capture)
            self.assertEqual(verification.result, VerificationResult.REJECTED)
            self.assertIn("dirty", verification.reason)
            self.assertNotIn(
                ("git", "verify-commit", "--raw", capture.git.head_commit),
                runner.calls,
            )

    def test_quarantine_promotes_only_exact_verified_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"
            source.mkdir()
            (source / "tracked.py").write_text("VALUE = 1\n", encoding="utf-8")
            adapter, _runner, capture = self._capture(source)
            verification = adapter.verify_signed_commit(source, capture)
            cas = FileSystemCAS(base / "cas")
            store = QuarantineStore(base / "intake", cas)
            record = store.materialize(capture, source)
            trusted = store.promote(record, verification)
            trusted.verify()
            self.assertTrue(cas.contains(trusted.decision_ref))

            wrong = GitSignatureVerification(
                source_snapshot_id=f"sha256:{'f' * 64}",
                source_tree_id=capture.tree_id,
                commit=capture.git.head_commit,
                result=VerificationResult.VERIFIED,
                signer="Alice",
                key_fingerprint="ABC",
                trust_code="G",
                verifier="git:test",
                detail_digest=f"sha256:{'e' * 64}",
            )
            with self.assertRaises(QuarantineError):
                store.promote(record, wrong)


class SubprocessCommandRunnerProcessTreeTests(unittest.TestCase):
    """Regression coverage for issue #79: `SubprocessCommandRunner.run` (the
    location the issue names directly) previously used bare
    `subprocess.run(timeout=...)`, which kills only the direct `git`
    process on timeout, leaving any helper tree (e.g. a hook or a
    credential-helper daemon) it spawned alive."""

    @unittest.skipUnless(os.name == "posix", "requires POSIX process groups")
    def test_timeout_kills_the_whole_process_tree_not_just_the_direct_child(
        self,
    ) -> None:
        """Reproducer from the issue: a wrapper (standing in for `git` with
        a hook) that daemonizes a sleeper, then the runner times out. The
        sleeper must not remain."""

        runner = SubprocessCommandRunner()
        with tempfile.TemporaryDirectory() as temporary:
            pid_file = Path(temporary) / "grandchild.pid"
            grandchild_script = Path(temporary) / "grandchild.py"
            grandchild_script.write_text(
                "import os, time\n"
                f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
                "time.sleep(60)\n"
            )
            parent_script = Path(temporary) / "parent.py"
            parent_script.write_text(
                "import subprocess, sys, time\n"
                f"subprocess.Popen([sys.executable, {str(grandchild_script)!r}])\n"
                "time.sleep(60)\n"
            )

            with self.assertRaises(GitSourceError):
                runner.run(
                    (sys.executable, str(parent_script)),
                    cwd=Path(temporary),
                    timeout_seconds=1,
                )

            deadline = time.monotonic() + 5
            while not pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(pid_file.exists(), "grandchild never started")
            grandchild_pid = int(pid_file.read_text().strip())

            deadline = time.monotonic() + 5
            alive = True
            while time.monotonic() < deadline:
                try:
                    os.kill(grandchild_pid, 0)
                except ProcessLookupError:
                    alive = False
                    break
                time.sleep(0.05)
            self.assertFalse(
                alive, "grandchild survived timeout; only the direct child was killed"
            )


if __name__ == "__main__":
    unittest.main()
