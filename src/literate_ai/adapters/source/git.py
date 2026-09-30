"""Git source intake with injectable, non-shell command execution."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from literate_ai.adapters._processes import run_with_tree_kill
from literate_ai.contracts import VerificationResult, canonical_identity
from literate_ai.sources import (
    GitChange,
    GitFacts,
    GitSignatureVerification,
    GitSubmoduleFact,
    SourceCapture,
    SourceSnapshotter,
)


class GitSourceError(RuntimeError):
    """A Git checkout could not be inspected through stable machine output."""


@dataclass(frozen=True, slots=True)
class CommandResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    def run(
        self,
        args: tuple[str, ...],
        *,
        cwd: Path,
        timeout_seconds: int,
    ) -> CommandResult: ...


class SubprocessCommandRunner:
    """Execute a fixed argument vector without a shell."""

    def run(
        self,
        args: tuple[str, ...],
        *,
        cwd: Path,
        timeout_seconds: int,
    ) -> CommandResult:
        environment = os.environ.copy()
        environment.update(
            {
                "GIT_OPTIONAL_LOCKS": "0",
                "LC_ALL": "C.UTF-8",
            }
        )
        try:
            completed = run_with_tree_kill(
                args,
                cwd=cwd,
                env=environment,
                stdin=subprocess.DEVNULL,
                text=True,
                timeout=timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitSourceError(f"command could not run: {args[0]}") from exc
        return CommandResult(
            args, completed.returncode, completed.stdout, completed.stderr
        )


class GitSourceAdapter:
    """Capture tracked bytes, dirty facts, gitlinks, LFS pointers, and signatures."""

    provider_id = "git"

    def __init__(
        self,
        runner: CommandRunner | None = None,
        *,
        git_binary: str = "git",
        timeout_seconds: int = 30,
        snapshotter: SourceSnapshotter | None = None,
    ) -> None:
        if not git_binary:
            raise ValueError("Git binary must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("Git timeout must be positive")
        self.runner = runner or SubprocessCommandRunner()
        self.git_binary = git_binary
        self.timeout_seconds = timeout_seconds
        self.snapshotter = snapshotter or SourceSnapshotter()

    def capture(self, root: str | Path) -> SourceCapture:
        configured = Path(root).expanduser().resolve(strict=True)
        top_level = Path(
            self._required(configured, "rev-parse", "--show-toplevel").stdout.strip()
        ).resolve(strict=True)
        if configured != top_level:
            raise GitSourceError("Git capture root must be the repository top level")

        head = self._required(top_level, "rev-parse", "HEAD").stdout.strip()
        if not head:
            raise GitSourceError("Git HEAD did not resolve to a commit")
        branch_result = self._run(top_level, "symbolic-ref", "--short", "-q", "HEAD")
        if branch_result.returncode not in {0, 1}:
            raise GitSourceError("Git branch inspection failed")
        head_ref = branch_result.stdout.strip() or None
        status_result = self._required(
            top_level,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        )
        submodule_result = self._required(
            top_level, "submodule", "status", "--recursive"
        )
        git = GitFacts(
            head_commit=head,
            head_ref=head_ref,
            changes=self._parse_status(status_result.stdout),
            submodules=self._parse_submodules(submodule_result.stdout),
        )
        tracked_result = self._required(top_level, "ls-files", "-z", "--cached")
        tracked_paths = tuple(
            path for path in tracked_result.stdout.split("\0") if path
        )
        return self.snapshotter.snapshot_git_paths(top_level, tracked_paths, git)

    def verify_signed_commit(
        self,
        root: str | Path,
        capture: SourceCapture,
    ) -> GitSignatureVerification:
        """Verify HEAD and bind the decision to the exact captured worktree."""

        if capture.git is None:
            raise GitSourceError("signed-commit verification requires a Git capture")
        if capture.git.dirty:
            return self._rejected(capture, "dirty Git sources are not covered by HEAD")
        if capture.lfs_pointers:
            return self._rejected(
                capture,
                "unhydrated Git LFS objects are not covered as source bytes",
            )

        repository = Path(root).expanduser().resolve(strict=True)
        commit = capture.git.head_commit
        verification = self._run(repository, "verify-commit", "--raw", commit)
        signature = self._run(
            repository,
            "log",
            "-1",
            "--format=%G?%x00%GF%x00%GS",
            commit,
        )
        trust_code, key_fingerprint, signer = self._signature_fields(signature)
        detail_digest = canonical_identity(
            {
                "command": "git verify-commit --raw",
                "commit": commit,
                "returncode": verification.returncode,
                "stdout": verification.stdout,
                "stderr": verification.stderr,
                "signature_status_returncode": signature.returncode,
            }
        ).uri
        verified = (
            verification.returncode == 0
            and signature.returncode == 0
            and trust_code == "G"
            and bool(key_fingerprint)
            and bool(signer)
        )
        return GitSignatureVerification(
            source_snapshot_id=capture.snapshot_id,
            source_tree_id=capture.tree_id,
            commit=commit,
            result=(
                VerificationResult.VERIFIED if verified else VerificationResult.REJECTED
            ),
            signer=signer,
            key_fingerprint=key_fingerprint,
            trust_code=trust_code,
            verifier=f"{self.git_binary}:verify-commit-v1",
            detail_digest=detail_digest,
            reason="" if verified else "Git did not report a good trusted signature",
        )

    def _rejected(
        self, capture: SourceCapture, reason: str
    ) -> GitSignatureVerification:
        assert capture.git is not None
        return GitSignatureVerification(
            source_snapshot_id=capture.snapshot_id,
            source_tree_id=capture.tree_id,
            commit=capture.git.head_commit,
            result=VerificationResult.REJECTED,
            signer="",
            key_fingerprint="",
            trust_code="",
            verifier=f"{self.git_binary}:verify-commit-v1",
            detail_digest=canonical_identity(
                {"commit": capture.git.head_commit, "rejected_before_command": reason}
            ).uri,
            reason=reason,
        )

    def _run(self, cwd: Path, *arguments: str) -> CommandResult:
        args = (self.git_binary, *arguments)
        result = self.runner.run(
            args,
            cwd=cwd,
            timeout_seconds=self.timeout_seconds,
        )
        if result.args != args:
            raise GitSourceError(
                "command runner returned a result for different arguments"
            )
        return result

    def _required(self, cwd: Path, *arguments: str) -> CommandResult:
        result = self._run(cwd, *arguments)
        if result.returncode != 0:
            raise GitSourceError(
                f"Git command failed ({' '.join(arguments)}): {result.stderr.strip()}"
            )
        return result

    @staticmethod
    def _parse_status(output: str) -> tuple[GitChange, ...]:
        fields = output.split("\0")
        changes: list[GitChange] = []
        index = 0
        while index < len(fields):
            field = fields[index]
            index += 1
            if not field:
                continue
            if len(field) < 4 or field[2] != " ":
                raise GitSourceError("Git porcelain status record is malformed")
            status = field[:2]
            path = field[3:]
            original: str | None = None
            if "R" in status or "C" in status:
                if index >= len(fields) or not fields[index]:
                    raise GitSourceError(
                        "Git rename status is missing its original path"
                    )
                original = fields[index]
                index += 1
            changes.append(GitChange(status, path, original))
        return tuple(changes)

    @staticmethod
    def _parse_submodules(output: str) -> tuple[GitSubmoduleFact, ...]:
        states = {
            " ": "clean",
            "-": "uninitialized",
            "+": "different",
            "U": "conflict",
        }
        facts: list[GitSubmoduleFact] = []
        for line in output.splitlines():
            if not line:
                continue
            marker = line[0]
            if marker not in states:
                raise GitSourceError("Git submodule status marker is unsupported")
            commit, separator, remainder = line[1:].partition(" ")
            path = remainder.partition(" (")[0] if separator else ""
            if not commit or not path:
                raise GitSourceError("Git submodule status record is malformed")
            facts.append(GitSubmoduleFact(path, commit, states[marker]))
        return tuple(sorted(facts, key=lambda item: item.path))

    @staticmethod
    def _signature_fields(result: CommandResult) -> tuple[str, str, str]:
        if result.returncode != 0:
            return "", "", ""
        fields = result.stdout.rstrip("\n").split("\0")
        if len(fields) != 3:
            raise GitSourceError("Git signature status output is malformed")
        return fields[0], fields[1], fields[2]


__all__ = [
    "CommandResult",
    "CommandRunner",
    "GitSourceAdapter",
    "GitSourceError",
    "SubprocessCommandRunner",
]
