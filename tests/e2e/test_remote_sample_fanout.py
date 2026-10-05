from __future__ import annotations

import base64
import gzip
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.contracts import (
    ExecutionRequirements,
    ExecutionWorker,
    ExecutionWorkerKind,
)
from literate_ai.remote_source_guard import (
    SourceGuardError,
    _symlink_target_identity,
    source_tree_identity,
    supervise,
)
from scripts.fanout_samples import (
    FANOUT_CHECKPOINT_VERSION,
    GitSource,
    Worker,
    _archive,
    _fanout_checkpoint_identity,
    _linux_git_command,
    _load_fanout_checkpoint,
    _store_fanout_checkpoint,
    _windows_git_command,
)


def git_source(
    repository_url: str = "git@example.invalid:org/repo.git",
    revision: str = "a" * 40,
    *,
    source_identity: str = "sha256:" + "b" * 64,
    guard_digest: str = "c" * 64,
    history_depth: int | None = 1,
) -> GitSource:
    return GitSource(
        repository_url,
        revision,
        "refs/heads/main",
        source_identity,
        guard_digest,
        history_depth,
    )


def ssh_worker(
    worker_id: str,
    destination: str,
    platform_flavor: str,
    source_mode: str = "working-tree",
) -> Worker:
    endpoint, separator, workspace = destination.partition(":")
    if not separator:
        endpoint, workspace = destination, ""
    platform_target = platform_flavor.removeprefix("flavor://literate-ai/os-")
    return Worker(
        ExecutionWorker(
            worker_id,
            ExecutionWorkerKind.SSH,
            requirements=ExecutionRequirements(os_family=platform_target),
            endpoint=endpoint,
            workspace=workspace,
        ),
        platform_flavor,
        source_mode,
    )


def compressed_powershell_script(command: str) -> str:
    match = re.search(
        r"IO\.MemoryStream\(,\[Convert\]::FromBase64String\("
        r"'([A-Za-z0-9+/=]+)'\)\)",
        command,
    )
    if match is None:
        raise AssertionError("compressed PowerShell payload is missing")
    return gzip.decompress(base64.b64decode(match.group(1))).decode("utf-8")


class RemoteSampleFanoutTests(unittest.TestCase):
    def setUp(self) -> None:
        evidence_environment = mock.patch.dict(
            os.environ,
            {
                "OBJ_DIR": "",
                "LITAI_EVIDENCE_RUN": "",
                "LITAI_EVIDENCE_PARENT": "",
            },
            clear=False,
        )
        evidence_environment.start()
        self.addCleanup(evidence_environment.stop)

    def test_git_targets_fetch_exact_revision_without_copying_source_archive(self):
        source = git_source()
        linux = _linux_git_command(
            "~/literate-ai", "run-id", ("sample",), "linux", source, 30
        )
        self.assertIn("materialize-git", linux)
        self.assertIn(":src/literate_ai/remote_source_guard.py", linux)
        self.assertIn(
            'fetch --no-tags --force --progress --depth=1 origin "$source_ref"',
            linux,
        )
        self.assertIn('rev-parse "FETCH_HEAD^{commit}"', linux)
        self.assertIn("GIT_NO_REPLACE_OBJECTS=1", linux)
        self.assertIn("core.hooksPath=/dev/null", linux)
        self.assertIn('export BUILD_DIR="$cache_root/generated"', linux)
        self.assertIn('export OBJ_DIR="$cache_root/_build"', linux)
        self.assertIn("export LITAI_REMOTE_LIVE_GATE=1", linux)
        self.assertIn(
            'export LITAI_REMOTE_PYTHON_ENV="$cache_root/remote-python-env"', linux
        )
        self.assertNotIn("archive --format=tar", linux)
        self.assertNotIn("worktree add", linux)
        self.assertIn("a" * 40, linux)
        self.assertNotIn("repo.tar.gz", linux)

        selected_linux = _linux_git_command(
            "~/literate-ai",
            "selected-run",
            ("sample",),
            "linux",
            source,
            30,
            coding_cli="codex",
            model="gpt-5.6-sol",
        )
        self.assertIn(
            "remote_sample_worker.py --platform-flavor linux "
            "--coding-cli codex --model gpt-5.6-sol",
            selected_linux,
        )

        windows = _windows_git_command(
            "~/literate-ai", "run-id", ("sample",), "windows", source, 30
        )
        script = compressed_powershell_script(windows)
        self.assertLess(len(windows), 8191)
        self.assertIn("materialize-git", script)
        self.assertIn("a" * 40, script)
        self.assertNotIn("repo.tar.gz", script)
        self.assertNotIn("archive --format=tar", script)
        self.assertNotIn("worktree add", script)
        self.assertIn("$sourceRef", script)
        self.assertIn('rev-parse "FETCH_HEAD^{commit}"', script)
        self.assertIn("GIT_NO_REPLACE_OBJECTS", script)
        self.assertIn("core.hooksPath=NUL", script)
        self.assertIn("$cacheRoot = Join-Path $HOME '.litai'", script)
        self.assertIn("$env:BUILD_DIR = Join-Path $cacheRoot 'sources'", script)
        self.assertIn("$env:OBJ_DIR = Join-Path $cacheRoot '_build'", script)
        self.assertIn("$env:LITAI_REMOTE_LIVE_GATE = '1'", script)
        self.assertIn(
            "$env:LITAI_REMOTE_PYTHON_ENV = Join-Path $cacheRoot 'python'",
            script,
        )
        self.assertIn("$remoteNames = @(& git -C $repo remote)", script)
        self.assertIn("$remoteNames -ccontains 'origin'", script)
        self.assertNotIn("remote get-url origin 2>$null", script)
        self.assertIn("bootstrap_remote_source_guard.py", script)

        selected_windows = compressed_powershell_script(
            _windows_git_command(
                "~/literate-ai",
                "selected-run",
                ("sample",),
                "windows",
                source,
                30,
                coding_cli="cursor-agent",
                model="gpt-5.6-sol",
            )
        )
        self.assertIn(
            "remote_sample_worker.py --platform-flavor windows "
            "--coding-cli 'cursor-agent' --model 'gpt-5.6-sol'",
            selected_windows,
        )

        self.assertIn("[Convert]::FromBase64String", script)
        self.assertNotIn("content=subprocess.run", script)
        bootstrap_match = re.search(r"FromBase64String\('([^']+)'\)", script)
        self.assertIsNotNone(bootstrap_match)
        bootstrap = base64.b64decode(bootstrap_match.group(1)).decode("utf-8")
        self.assertIn(":src/literate_ai/remote_source_guard.py", bootstrap)
        self.assertIn("content=subprocess.run", bootstrap)

        sha256_source = git_source(revision="a" * 64)
        sha256_linux = _linux_git_command(
            "~/literate-ai", "sha256-run", ("sample",), "linux", sha256_source, 30
        )
        self.assertIn("object_format=sha256", sha256_linux)
        self.assertIn('--object-format="$object_format"', sha256_linux)
        sha256_windows = _windows_git_command(
            "~/literate-ai",
            "sha256-run",
            ("sample",),
            "windows",
            sha256_source,
            30,
        )
        sha256_script = compressed_powershell_script(sha256_windows)
        self.assertIn("$objectFormat = 'sha256'", sha256_script)
        self.assertIn('init "--object-format=$objectFormat"', sha256_script)

    def test_fanout_checkpoint_is_plan_bound_compact_and_resumable(self):
        targets = (
            ssh_worker("linux", "user@linux:~/literate-ai", "linux"),
            ssh_worker("windows", "user@windows:~/literate-ai", "windows"),
        )
        identity = _fanout_checkpoint_identity(
            ("sample",),
            targets,
            source_bindings=(
                ("linux", "sha256:" + "a" * 64, None),
                ("windows", "sha256:" + "a" * 64, None),
            ),
            coding_cli="codex",
            model="gpt-5.6-sol",
        )
        passed = {"linux": {"id": "linux", "passed": True}}
        with tempfile.TemporaryDirectory() as temporary:
            checkpoint = Path(temporary) / "checkpoint.json"
            _store_fanout_checkpoint(checkpoint, identity, passed)
            self.assertNotIn("\n ", checkpoint.read_text(encoding="utf-8"))
            self.assertEqual(
                json.loads(checkpoint.read_text(encoding="utf-8"))["v"],
                FANOUT_CHECKPOINT_VERSION,
            )
            self.assertEqual(
                _load_fanout_checkpoint(
                    checkpoint, identity, {target.worker_id for target in targets}
                ),
                passed,
            )
            self.assertEqual(
                _load_fanout_checkpoint(
                    checkpoint,
                    "different-plan",
                    {target.worker_id for target in targets},
                ),
                {},
            )
            legacy = json.loads(checkpoint.read_text(encoding="utf-8"))
            legacy["v"] = 1
            checkpoint.write_text(json.dumps(legacy), encoding="utf-8")
            self.assertEqual(
                _load_fanout_checkpoint(
                    checkpoint, identity, {target.worker_id for target in targets}
                ),
                {},
            )
            checkpoint.write_text("{", encoding="utf-8")
            self.assertEqual(
                _load_fanout_checkpoint(
                    checkpoint, identity, {target.worker_id for target in targets}
                ),
                {},
            )

    @unittest.skipUnless(shutil.which("git"), "requires Git")
    def test_git_visible_archive_captures_dirty_and_untracked_current_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repository"
            repository.mkdir()
            subprocess.run(
                ["git", "init"],
                cwd=repository,
                check=True,
                capture_output=True,
            )
            (repository / ".gitignore").write_text("secret.env\n", encoding="utf-8")
            tracked = repository / "tracked.txt"
            tracked.write_text("indexed bytes\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", ".gitignore", "tracked.txt"],
                cwd=repository,
                check=True,
                capture_output=True,
            )
            tracked.write_text("current dirty bytes\n", encoding="utf-8")
            (repository / "untracked.txt").write_text(
                "current untracked bytes\n", encoding="utf-8"
            )
            (repository / "secret.env").write_text(
                "must not leave this host\n", encoding="utf-8"
            )

            archive = root / "working-tree.tar.gz"
            identity = _archive(repository, archive)
            extracted = root / "extracted"
            extracted.mkdir()
            with tarfile.open(archive, "r:gz") as source:
                source.extractall(extracted)
                names = set(source.getnames())
            materialized = extracted / "literate-ai"

            self.assertEqual(
                (materialized / "tracked.txt").read_text(encoding="utf-8"),
                "current dirty bytes\n",
            )
            self.assertEqual(
                (materialized / "untracked.txt").read_text(encoding="utf-8"),
                "current untracked bytes\n",
            )
            self.assertNotIn("literate-ai/secret.env", names)
            self.assertFalse((materialized / "secret.env").exists())
            self.assertEqual(identity, source_tree_identity(materialized))

    def test_source_guard_rejects_a_repository_escaping_symlink(self):
        with self.assertRaisesRegex(SourceGuardError, "escapes the repository"):
            _symlink_target_identity("link", b"../outside")

    def test_archive_capture_rejects_a_mutating_working_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "specification.md").write_text("behavior\n", encoding="utf-8")
            with mock.patch(
                "scripts.fanout_samples.source_tree_identity",
                side_effect=("sha256:" + "a" * 64, "sha256:" + "b" * 64),
            ):
                with self.assertRaisesRegex(ValueError, "changed while"):
                    _archive(source, root / "source.tar.gz")

    @unittest.skipIf(os.name == "nt", "POSIX process-group behavior")
    def test_supervisor_terminates_the_worker_process_group(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            status = root / "status.json"
            child_stopped = root / "child-stopped"
            child = (
                "import os,pathlib,signal,time; "
                f"marker=pathlib.Path({str(child_stopped)!r}); "
                "signal.signal(signal.SIGTERM, "
                "lambda *_: (marker.write_text('stopped'), os._exit(0))); "
                "time.sleep(30)"
            )
            parent = (
                "import subprocess,sys,time; "
                f"subprocess.Popen([sys.executable,'-c',{child!r}], "
                "env={**__import__('os').environ,'PYTHONUNBUFFERED':'1'}); "
                "time.sleep(30)"
            )
            returncode = supervise(1, status, [sys.executable, "-c", parent])
            # The parent can exit before its child's signal handler writes the
            # marker. Keep the fixture alive for that bounded shutdown interval.
            deadline = time.monotonic() + 5
            while not child_stopped.is_file() and time.monotonic() < deadline:
                time.sleep(0.01)
            child_was_stopped = child_stopped.is_file()

        self.assertEqual(returncode, 124)
        self.assertTrue(child_was_stopped)


if __name__ == "__main__":
    unittest.main()
