"""Non-building acquisition of exact repository dependency source."""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from literate_ai.adapters.builders._process import run_bounded_process
from literate_ai.adapters.source.git import CommandResult, GitSourceAdapter
from literate_ai.application.repository_sources import (
    RepositoryCheckout,
    RepositorySourceResolutionError,
)
from literate_ai.contracts import (
    RepositoryFetchDeadlinePolicy,
    RepositoryRevisionKind,
    RepositorySourceDependency,
    canonical_identity,
)
from literate_ai.sources import SourceCapture


class _RepositoryGitRunner:
    """Keep fetch credentials available; isolate checkout and capture configuration."""

    def __init__(self, policy: RepositoryFetchDeadlinePolicy) -> None:
        self.policy = policy

    def run(
        self, args: tuple[str, ...], *, cwd: Path, timeout_seconds: int
    ) -> CommandResult:
        fetching = len(args) > 1 and args[1] == "fetch"
        authentication = {
            "GIT_SSH",
            "GIT_SSH_COMMAND",
            "GIT_ASKPASS",
            "GIT_PROXY_COMMAND",
        }
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_") or (fetching and key in authentication)
        }
        environment.update(
            GIT_TERMINAL_PROMPT="0",
            GCM_INTERACTIVE="Never",
            GIT_OPTIONAL_LOCKS="0",
            GIT_LFS_SKIP_SMUDGE="1",
            LC_ALL="C.UTF-8",
        )
        if fetching:
            environment.setdefault(
                "GIT_SSH_COMMAND",
                "ssh -o BatchMode=yes -o ConnectionAttempts=1 "
                f"-o ConnectTimeout={self.policy.connect_seconds}",
            )
        else:
            environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        result = run_bounded_process(
            (
                args[0],
                "-c",
                f"core.hooksPath={os.devnull}",
                "-c",
                "core.autocrlf=false",
                "-c",
                "core.filemode=true",
                "-c",
                "core.symlinks=true",
                "-c",
                "submodule.recurse=false",
                *args[1:],
            ),
            cwd=cwd,
            environment=environment,
            timeout_seconds=self.policy.total_seconds if fetching else timeout_seconds,
            inactivity_timeout_seconds=self.policy.no_progress_seconds
            if fetching
            else None,
            stdout_limit_bytes=16 * 1024 * 1024,
            stderr_limit_bytes=1024 * 1024,
            error_prefix="repository-source.git",
        )
        return CommandResult(
            args,
            result.returncode,
            result.stdout.decode("utf-8", errors="strict"),
            result.stderr.decode("utf-8", errors="replace"),
        )


class GitRepositorySourceAcquirer:
    """Fetch selectors into disposable detached checkouts without running builds."""

    def __init__(
        self,
        *,
        git_binary: str = "git",
        deadline_policy: RepositoryFetchDeadlinePolicy | None = None,
        scratch_root: Path | None = None,
    ) -> None:
        self.git_binary = git_binary
        self.runner = _RepositoryGitRunner(
            deadline_policy or RepositoryFetchDeadlinePolicy()
        )
        self.scratch_root = scratch_root

    @contextmanager
    def acquire(
        self, dependency: RepositorySourceDependency
    ) -> Iterator[RepositoryCheckout]:
        executable = shutil.which(self.git_binary)
        if executable is None:
            raise RepositorySourceResolutionError(
                "repository-source.git-unavailable",
                "Git is required to lock repository source",
            )
        with tempfile.TemporaryDirectory(
            prefix="litai-source-", dir=self.scratch_root
        ) as directory:
            root = Path(directory).resolve()

            def required(*arguments: str) -> str:
                result = self.runner.run(
                    (executable, *arguments), cwd=root, timeout_seconds=120
                )
                if result.returncode:
                    # Remote output can contain private endpoints or credentials.
                    raise RepositorySourceResolutionError(
                        "repository-source.git-failed",
                        "Git could not acquire exact repository source",
                    )
                return result.stdout

            required("init", "--template=", ".")
            selector = dependency.revision_selector
            revision = (
                "HEAD"
                if selector.kind is RepositoryRevisionKind.DEFAULT
                else f"refs/heads/{selector.value}"
                if selector.kind is RepositoryRevisionKind.BRANCH
                else selector.value
            )
            required(
                "fetch",
                "--depth=1",
                "--no-tags",
                "--no-recurse-submodules",
                "--progress",
                "--",
                dependency.repository_url,
                revision,
            )
            commit = required("rev-parse", "--verify", "FETCH_HEAD^{commit}").strip()
            if selector.immutable and commit != selector.value:
                raise RepositorySourceResolutionError(
                    "repository-source.selector-mismatch",
                    "Git fetched a different exact commit",
                )
            required("checkout", "--detach", "--force", commit, "--")
            yield RepositoryCheckout(
                root,
                commit,
                canonical_identity(
                    {
                        "resolver": "git-repository-source@1",
                        "checkout": "detached-no-filters",
                    }
                ),
            )


class GitRepositorySourceCapturer:
    """Capture acquired bytes using the same isolated Git inspection policy."""

    def __init__(self, *, git_binary: str = "git") -> None:
        self.adapter = GitSourceAdapter(
            runner=_RepositoryGitRunner(RepositoryFetchDeadlinePolicy()),
            git_binary=git_binary,
        )

    def capture(self, checkout: RepositoryCheckout) -> SourceCapture:
        return self.adapter.capture(checkout.source_root)
