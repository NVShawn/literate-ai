from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from literate_ai.contracts import (
    ExecutionRequirements,
    ExecutionWorker,
    ExecutionWorkerCatalog,
    ExecutionWorkerKind,
    canonical_json_bytes,
)
from literate_ai.remote_source_guard import (
    SourceGuardError,
    _symlink_target_identity,
    git_tree_identity,
    source_tree_identity,
    supervise,
    verify_materialized_git_tree,
)
from scripts.fanout_samples import (
    FANOUT_CHECKPOINT_VERSION,
    SCHEMA,
    GitSource,
    Worker,
    _archive,
    _archive_identity,
    _compressed_powershell_command,
    _configuration,
    _execute_workers,
    _fanout_checkpoint_identity,
    _fanout_report,
    _git_source,
    _linux_command,
    _linux_git_command,
    _load_fanout_checkpoint,
    _parser,
    _posix_python_discovery,
    _powershell_command,
    _record_worker,
    _remaining_timeout,
    _run_worker,
    _ssh_arguments,
    _store_fanout_checkpoint,
    _windows_command,
    _windows_git_command,
    _worker_log,
    main,
)
from scripts.remote_sample_worker import (
    _forwarded_live_arguments,
    _managed_python,
    _run_bootstrap,
    _usable_environment,
    _windows_bazel_shell,
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


def write_workers(path: Path, *workers: Worker) -> None:
    path.write_bytes(
        canonical_json_bytes(
            ExecutionWorkerCatalog(tuple(item.definition for item in workers)).to_dict()
        )
    )


def write_matrix(
    path: Path, *workers: Worker, default_samples: tuple[str, ...] = ("sample",)
) -> None:
    path.write_text(
        json.dumps(
            {
                "schema": SCHEMA,
                "default_samples": list(default_samples),
                "workers": [
                    {
                        "worker_id": worker.worker_id,
                        "platform_flavor": worker.platform_flavor,
                        "source_mode": worker.source_mode,
                    }
                    for worker in workers
                ],
            }
        ),
        encoding="utf-8",
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

    def test_ssh_arguments_preserve_worker_transport_and_native_shell(self) -> None:
        windows = ssh_worker("windows", "user@windows:~/matrix", "windows", "git")
        windows = Worker(
            replace(windows.definition, transport="s"),
            windows.platform_flavor,
            windows.source_mode,
        )
        powershell = (
            "powershell.exe -NoProfile -NonInteractive -EncodedCommand ZQB4AGkAdAA="
        )
        windows_argv = _ssh_arguments(windows, powershell, 30)

        self.assertEqual(windows_argv[0], "s")
        self.assertEqual(windows_argv[-1], powershell)
        self.assertNotIn("bash -lic", windows_argv[-1])

        linux = ssh_worker("linux", "user@linux:~/matrix", "linux", "git")
        linux_argv = _ssh_arguments(linux, "make release-check", 30)
        self.assertEqual(linux_argv[0], "ssh")
        self.assertEqual(linux_argv[-1], "bash -lic 'make release-check'")

    def test_partial_managed_python_environment_is_not_reusable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            environment = Path(temporary) / "python"
            python = _managed_python(environment)
            python.parent.mkdir(parents=True)
            python.write_bytes(b"partial")
            with mock.patch(
                "scripts.remote_sample_worker.subprocess.run",
                return_value=subprocess.CompletedProcess((), 1),
            ) as run:
                self.assertFalse(_usable_environment(environment))

        self.assertIn("import pip", run.call_args.args[0][2])

    def test_complete_managed_python_environment_is_reusable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            environment = Path(temporary) / "python"
            python = _managed_python(environment)
            python.parent.mkdir(parents=True)
            python.write_bytes(b"complete")
            with mock.patch(
                "scripts.remote_sample_worker.subprocess.run",
                return_value=subprocess.CompletedProcess((), 0),
            ):
                self.assertTrue(_usable_environment(environment))

    def test_worker_bootstrap_forwards_exact_flavor_closure_and_path(self) -> None:
        report = {
            "schema": "literate-ai/host-bootstrap-report@2",
            "passed": True,
            "path_entries": ["/managed/node/bin", "/managed/bazel/bin"],
        }
        completed = subprocess.CompletedProcess(
            (),
            0,
            "native package-manager progress\n" + json.dumps(report) + "\n",
            "bootstrap warning\n",
        )
        environment = {"PATH": "/native/bin"}
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch(
                "scripts.remote_sample_worker.subprocess.run",
                return_value=completed,
            ) as run,
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            observed = _run_bootstrap(
                python=Path("/python"),
                bootstrap=Path("/bootstrap.py"),
                platform="linux",
                coding_cli="codex",
                flavors=("lang-javascript", "build-bazel"),
            )
            updated_path = os.environ["PATH"]

        self.assertEqual(observed, report)
        command = run.call_args.args[0]
        self.assertIn("--coding-cli", command)
        self.assertEqual(command.count("--flavor"), 2)
        self.assertIn("lang-javascript", command)
        self.assertIn("build-bazel", command)
        self.assertEqual(
            updated_path,
            os.pathsep.join(("/managed/node/bin", "/managed/bazel/bin", "/native/bin")),
        )
        self.assertEqual(
            stderr.getvalue(),
            "bootstrap warning\nnative package-manager progress\n",
        )

    def test_worker_bootstrap_rejects_output_without_a_typed_report(self) -> None:
        completed = subprocess.CompletedProcess((), 0, "installation complete\n", "")
        with mock.patch(
            "scripts.remote_sample_worker.subprocess.run", return_value=completed
        ):
            with self.assertRaisesRegex(SystemExit, "required JSON report"):
                _run_bootstrap(
                    python=Path("/python"),
                    bootstrap=Path("/bootstrap.py"),
                    platform="windows",
                    coding_cli="codex",
                )

    def test_worker_bootstrap_rejects_unproven_flavor_closure(self) -> None:
        completed = subprocess.CompletedProcess(
            (),
            1,
            json.dumps(
                {
                    "schema": "literate-ai/host-bootstrap-report@2",
                    "passed": False,
                    "path_entries": [],
                }
            ),
            "",
        )
        with mock.patch(
            "scripts.remote_sample_worker.subprocess.run", return_value=completed
        ):
            with self.assertRaisesRegex(SystemExit, "selected Flavor closure"):
                _run_bootstrap(
                    python=Path("/python"),
                    bootstrap=Path("/bootstrap.py"),
                    platform="windows",
                    coding_cli="cursor-agent",
                    flavors=("lang-cpp",),
                )

    def test_flavor_matrix_changes_checkpoint_identity(self) -> None:
        workers = (
            ssh_worker(
                "host",
                "user@localhost:~/matrix",
                "flavor://literate-ai/os-macos",
            ),
        )
        arguments = {
            "source_bindings": (("host", "sha256:" + "a" * 64, None),),
            "coding_cli": "codex",
            "model": "gpt-5.6-sol",
        }
        default = _fanout_checkpoint_identity(("sample",), workers, **arguments)
        expanded = _fanout_checkpoint_identity(
            ("sample",),
            workers,
            (
                "flavor://literate-ai/os-*",
                "flavor://literate-ai/lang-*",
            ),
            **arguments,
        )
        self.assertNotEqual(default, expanded)

    def test_source_and_live_selection_change_checkpoint_identity(self) -> None:
        workers = (
            ssh_worker(
                "host",
                "user@localhost:~/matrix",
                "flavor://literate-ai/os-linux",
                "git",
            ),
        )

        def identity(
            *,
            source_identity: str = "sha256:" + "a" * 64,
            revision: str = "b" * 40,
            coding_cli: str = "codex",
            model: str = "gpt-5.6-sol",
        ) -> str:
            return _fanout_checkpoint_identity(
                ("sample",),
                workers,
                source_bindings=(("host", source_identity, revision),),
                coding_cli=coding_cli,
                model=model,
            )

        baseline = identity()
        self.assertNotEqual(baseline, identity(source_identity="sha256:" + "c" * 64))
        self.assertNotEqual(baseline, identity(revision="d" * 40))
        self.assertNotEqual(baseline, identity(coding_cli="cursor-agent"))
        self.assertNotEqual(baseline, identity(model="gpt-5.6-terra"))

    def test_remote_worker_forwards_only_complete_live_selection(self) -> None:
        self.assertEqual(_forwarded_live_arguments(None, None), ())
        self.assertEqual(
            _forwarded_live_arguments("cursor-agent", "gpt-5.6-sol"),
            ("--coding-cli", "cursor-agent", "--model", "gpt-5.6-sol"),
        )
        with self.assertRaisesRegex(SystemExit, "both coding CLI and model"):
            _forwarded_live_arguments("codex", None)

    @unittest.skipUnless(os.name == "posix" and shutil.which("sh"), "requires sh")
    def test_posix_python_discovery_prefers_path_then_bounded_fallback(self):
        shell = shutil.which("sh")
        assert shell is not None
        with tempfile.TemporaryDirectory() as temporary:
            path_root = Path(temporary) / "path"
            path_root.mkdir()
            path_python = path_root / "python3"
            path_python.symlink_to(sys.executable)
            discovery = _posix_python_discovery((sys.executable,))

            preferred = subprocess.run(
                [shell, "-c", discovery + '; printf "%s" "$python_command"'],
                check=True,
                capture_output=True,
                text=True,
                env={"PATH": str(path_root)},
            )
            self.assertEqual(preferred.stdout, str(path_python))

            path_python.unlink()
            path_python.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            path_python.chmod(0o755)
            fallback = subprocess.run(
                [shell, "-c", discovery + '; printf "%s" "$python_command"'],
                check=True,
                capture_output=True,
                text=True,
                env={"PATH": str(path_root)},
            )
            self.assertEqual(fallback.stdout, sys.executable)

    @unittest.skipUnless(os.name == "posix" and shutil.which("sh"), "requires sh")
    def test_posix_python_discovery_preserves_outer_fail_fast_chain(self):
        shell = shutil.which("sh")
        assert shell is not None
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "suffix-ran"
            discovery = _posix_python_discovery((sys.executable,))
            completed = subprocess.run(
                [
                    shell,
                    "-c",
                    f'false && {discovery} && printf reached > "$1"',
                    "discovery-regression",
                    str(marker),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertFalse(marker.exists())

    def test_windows_worker_discovers_or_validates_bazel_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            git_bash = root / "Git" / "bin" / "bash.exe"
            git_bash.parent.mkdir(parents=True)
            git_bash.write_bytes(b"fixture")

            self.assertEqual(
                _windows_bazel_shell({"PROGRAMFILES": str(root), "Path": ""}),
                str(git_bash.resolve()),
            )
            self.assertEqual(
                _windows_bazel_shell({"BAZEL_SH": str(git_bash)}),
                str(git_bash.resolve()),
            )
            with self.assertRaisesRegex(SystemExit, "existing absolute bash.exe"):
                _windows_bazel_shell({"BAZEL_SH": str(root / "missing.exe")})

    def test_example_matrix_uses_placeholder_destinations_and_platform_flavors(self):
        patterns, targets = _configuration(
            Path("literate.test.example.json").resolve(),
            Path("literate.workers.example.json").resolve(),
        )
        self.assertEqual(patterns, ("regenerative-roundtrip",))
        self.assertEqual(
            {item.platform_flavor for item in targets},
            {
                "flavor://literate-ai/os-linux",
                "flavor://literate-ai/os-macos",
                "flavor://literate-ai/os-windows",
            },
        )
        for target in targets:
            host, base = target.host_and_base
            self.assertIn("@", host)
            self.assertTrue(base.startswith("~/"))
        self.assertTrue(
            all(".example.invalid:" in item.destination for item in targets)
        )
        self.assertTrue(all(item.source_mode == "git" for item in targets))

    def test_matrix_path_can_be_supplied_by_the_user_session(self):
        with mock.patch.dict(
            os.environ,
            {
                "LITAI_TEST_CONFIG": "/private/session/matrix.json",
                "LITAI_WORKER_CONFIG": "/private/session/workers.json",
            },
        ):
            arguments = _parser().parse_args([])
        self.assertEqual(arguments.config, Path("/private/session/matrix.json"))
        self.assertEqual(arguments.worker_config, Path("/private/session/workers.json"))

    def test_destination_accepts_home_relative_or_absolute_but_not_traversal(self):
        self.assertEqual(
            ssh_worker("absolute", "user@host:/srv/litai", "linux").host_and_base,
            ("user@host", "/srv/litai"),
        )
        with self.assertRaisesRegex(ValueError, "traversal|destination must be"):
            _ = ssh_worker("escape", "user@host:~/../escape", "linux").host_and_base
        with self.assertRaisesRegex(ValueError, "workspace|destination must be"):
            _ = ssh_worker("drive", "user@host:C:/literate-ai", "windows").host_and_base

    def test_windows_commands_use_encoded_powershell(self):
        command = _powershell_command("Write-Output 'hello'")
        self.assertRegex(
            command,
            r"^powershell\.exe -NoProfile -NonInteractive -EncodedCommand "
            r"[A-Za-z0-9+/=]+$",
        )
        compressed = _compressed_powershell_command("Write-Output 'hello'")
        self.assertEqual(
            compressed_powershell_script(compressed), "Write-Output 'hello'"
        )
        self.assertNotIn("$", compressed)
        self.assertLess(len(compressed), len(command) + 400)

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

    def test_git_targets_make_history_depth_and_full_history_explicit(self):
        bounded = git_source(history_depth=32)
        linux = _linux_git_command(
            "~/literate-ai", "run-id", ("sample",), "linux", bounded, 30
        )
        windows = compressed_powershell_script(
            _windows_git_command(
                "~/literate-ai", "run-id", ("sample",), "windows", bounded, 30
            )
        )
        self.assertIn("--depth=32", linux)
        self.assertIn("--depth=32", windows)
        self.assertIn("--full-repository-history", linux)
        self.assertIn("--full-repository-history", windows)

        full = git_source(history_depth=None)
        linux_full = _linux_git_command(
            "~/literate-ai", "run-id", ("sample",), "linux", full, 30
        )
        windows_full = compressed_powershell_script(
            _windows_git_command(
                "~/literate-ai", "run-id", ("sample",), "windows", full, 30
            )
        )
        self.assertIn("--unshallow", linux_full)
        self.assertIn("--unshallow", windows_full)

    def test_windows_working_tree_checks_hash_and_native_failures_before_cleanup(self):
        identity = "sha256:" + "c" * 64
        command = _windows_command(
            "~/matrix/runs/run-id",
            ("sample'quoted",),
            "windows",
            identity,
            identity,
            "d" * 64,
            30,
        )
        script = base64.b64decode(command.rsplit(" ", 1)[1]).decode("utf-16le")
        self.assertIn("$ErrorActionPreference = 'Stop'", script)
        self.assertIn("source archive SHA-256 mismatch", script)
        self.assertNotIn("Get-FileHash", script)
        self.assertIn("hashlib.file_digest", script)
        self.assertIn("extract-archive", script)
        self.assertIn("$env:BUILD_DIR = Join-Path $cacheRoot 'sources'", script)
        self.assertIn("$env:OBJ_DIR = Join-Path $cacheRoot '_build'", script)
        self.assertIn("$env:LITAI_REMOTE_LIVE_GATE = '1'", script)
        self.assertIn(
            "$env:LITAI_REMOTE_PYTHON_ENV = Join-Path $cacheRoot 'python'",
            script,
        )
        self.assertNotIn("& tar ", script)
        self.assertIn("--sample 'sample''quoted'", script)
        self.assertLess(
            script.index("$workerExit"),
            script.index("Remove-Item -LiteralPath $root"),
        )

        retained = _windows_command(
            "~/matrix/runs/run-id",
            ("sample",),
            "windows",
            identity,
            identity,
            "d" * 64,
            30,
            retain_workspace=True,
        )
        retained_script = base64.b64decode(retained.rsplit(" ", 1)[1]).decode(
            "utf-16le"
        )
        self.assertNotIn("Remove-Item -LiteralPath $root", retained_script)

    def test_git_source_requires_clean_head_at_an_origin_ref(self):
        revision = "b" * 40
        responses = iter(
            (
                "",
                "git@example.invalid:org/repo.git\n",
                revision + "\n",
                revision + "\trefs/heads/main\n",
                b"guard-content\n",
            )
        )

        def run(command, **_kwargs):
            return subprocess.CompletedProcess(command, 0, stdout=next(responses))

        with (
            mock.patch("scripts.fanout_samples.subprocess.run", side_effect=run),
            mock.patch(
                "scripts.fanout_samples.git_tree_identity",
                return_value="sha256:" + "a" * 64,
            ),
        ):
            source = _git_source(Path("repository"))
        self.assertEqual(source.revision, revision)
        self.assertEqual(source.advertised_ref, "refs/heads/main")

        dirty = subprocess.CompletedProcess(["git"], 0, stdout=" M specification.md\n")
        with mock.patch("scripts.fanout_samples.subprocess.run", return_value=dirty):
            with self.assertRaisesRegex(ValueError, "clean working tree"):
                _git_source(Path("repository"))

    @unittest.skipUnless(shutil.which("git") and shutil.which("sh"), "requires Git/sh")
    def test_posix_git_target_uses_persistent_checkout_and_detached_worktree(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote = root / "remote.git"
            authority = root / "authority"
            checkout = root / "persistent" / "literate-ai"
            checkout.parent.mkdir()
            checkout.mkdir()
            sentinel = checkout / "preexisting-worker-state.txt"
            sentinel.write_text("preserve me\n", encoding="utf-8")
            subprocess.run(
                ["git", "init", "--bare", "--object-format=sha256", str(remote)],
                check=True,
                capture_output=True,
            )
            authority.mkdir()
            subprocess.run(
                ["git", "init", "--object-format=sha256"],
                cwd=authority,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "matrix@example.invalid"],
                cwd=authority,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Matrix Test"],
                cwd=authority,
                check=True,
                capture_output=True,
            )
            worker = authority / "scripts" / "remote_sample_worker.py"
            worker.parent.mkdir()
            worker.write_text("print('worker-ran')\n", encoding="utf-8")
            guard = authority / "src" / "literate_ai" / "remote_source_guard.py"
            guard.parent.mkdir(parents=True)
            shutil.copy2(Path("src/literate_ai/remote_source_guard.py"), guard)
            subprocess.run(
                ["git", "add", "."],
                cwd=authority,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "fixture"],
                cwd=authority,
                check=True,
                capture_output=True,
            )
            revision = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=authority,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(len(revision), 64)
            subprocess.run(
                ["git", "push", str(remote), f"{revision}:refs/heads/main"],
                cwd=authority,
                check=True,
                capture_output=True,
            )

            command = _linux_git_command(
                str(checkout),
                "functional-run",
                ("sample",),
                "linux",
                git_source(
                    str(remote),
                    revision,
                    source_identity=git_tree_identity(authority, revision),
                    guard_digest=hashlib.sha256(guard.read_bytes()).hexdigest(),
                ),
                30,
            )
            completed = subprocess.run(
                ["sh", "-c", command], check=True, capture_output=True, text=True
            )

            worktree = Path(str(checkout) + ".litai-runs") / "functional-run"
            self.assertIn("worker-ran", completed.stdout)
            self.assertFalse(worktree.exists())
            self.assertTrue((checkout / ".git").is_dir())
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve me\n")
            registrations = subprocess.run(
                ["git", "-C", str(checkout), "worktree", "list", "--porcelain"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertNotIn("functional-run", registrations)

            subprocess.run(
                ["git", "tag", "v1", revision],
                cwd=authority,
                check=True,
                capture_output=True,
            )
            (authority / "later.txt").write_text("later\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "later.txt"],
                cwd=authority,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "later"],
                cwd=authority,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "push", str(remote), "HEAD:refs/heads/main", "v1"],
                cwd=authority,
                check=True,
                capture_output=True,
            )
            exact_tree = git_tree_identity(authority, revision)
            guard_digest = hashlib.sha256(guard.read_bytes()).hexdigest()
            for label, source_ref in (
                ("tag-run", "refs/tags/v1"),
                ("sha-run", revision),
            ):
                with self.subTest(source_ref=source_ref):
                    isolated_checkout = root / label / "literate-ai"
                    isolated_checkout.parent.mkdir()
                    command = _linux_git_command(
                        str(isolated_checkout),
                        label,
                        ("sample",),
                        "linux",
                        GitSource(
                            str(remote),
                            revision,
                            source_ref,
                            exact_tree,
                            guard_digest,
                        ),
                        30,
                    )
                    completed = subprocess.run(
                        ["sh", "-c", command],
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                    self.assertIn("worker-ran", completed.stdout)

    @unittest.skipUnless(shutil.which("sh") and shutil.which("tar"), "requires sh/tar")
    def test_posix_working_tree_verifies_archive_and_cleans_only_after_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            worker = source / "scripts" / "remote_sample_worker.py"
            worker.parent.mkdir(parents=True)
            worker.write_text("print('archive-worker-ran')\n", encoding="utf-8")
            guard = source / "src" / "literate_ai" / "remote_source_guard.py"
            guard.parent.mkdir(parents=True)
            shutil.copy2(Path("src/literate_ai/remote_source_guard.py"), guard)
            archive = root / "repo.tar.gz"
            source_identity = _archive(source, archive)
            transport_identity = _archive_identity(archive)
            guard_digest = hashlib.sha256(guard.read_bytes()).hexdigest()

            successful = root / "successful-run"
            successful_incoming = Path(str(successful) + ".incoming")
            successful_incoming.mkdir()
            shutil.copy2(archive, successful_incoming / "repo.tar.gz")
            shutil.copy2(guard, successful_incoming / "remote_source_guard.py")
            command = _linux_command(
                str(successful),
                ("sample",),
                "linux",
                transport_identity,
                source_identity,
                guard_digest,
                30,
            )
            completed = subprocess.run(
                ["sh", "-c", command], check=True, capture_output=True, text=True
            )
            self.assertIn("archive-worker-ran", completed.stdout)
            self.assertIn('export BUILD_DIR="$cache_root/generated"', command)
            self.assertIn('export OBJ_DIR="$cache_root/_build"', command)
            self.assertIn("export LITAI_REMOTE_LIVE_GATE=1", command)
            self.assertFalse(successful.exists())

            failed = root / "failed-run"
            failed_incoming = Path(str(failed) + ".incoming")
            failed_incoming.mkdir()
            shutil.copy2(archive, failed_incoming / "repo.tar.gz")
            shutil.copy2(guard, failed_incoming / "remote_source_guard.py")
            with (failed_incoming / "repo.tar.gz").open("ab") as stream:
                stream.write(b"changed")
            failed_command = _linux_command(
                str(failed),
                ("sample",),
                "linux",
                transport_identity,
                source_identity,
                guard_digest,
                30,
            )
            failed_result = subprocess.run(
                ["sh", "-c", failed_command],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(failed_result.returncode, 0)
            self.assertIn("source archive SHA-256 mismatch", failed_result.stderr)
            self.assertTrue(failed_incoming.exists())

    def test_configuration_fails_closed_on_unknown_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "matrix.json"
            path.write_text(
                json.dumps(
                    {
                        "schema": SCHEMA,
                        "default_samples": ["*"],
                        "workers": [],
                        "surprise": True,
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "missing or unknown"):
                _configuration(path, root / "workers.json")

    def test_configuration_allows_missing_worker_fleet(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "matrix.json"
            path.write_text(
                json.dumps(
                    {
                        "schema": SCHEMA,
                        "default_samples": ["*"],
                        "workers": [
                            {
                                "worker_id": "linux-local",
                                "platform_flavor": "linux",
                                "source_mode": "working-tree",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            patterns, workers = _configuration(path, root / "workers.json")
            self.assertEqual(patterns, ("*",))
            self.assertEqual(workers, ())

    def test_no_worker_fleet_report_is_non_authoritative(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            matrix = root / "matrix.json"
            matrix.write_text(
                json.dumps(
                    {
                        "schema": SCHEMA,
                        "default_samples": ["sample"],
                        "workers": [
                            {
                                "worker_id": "placeholder",
                                "platform_flavor": "linux",
                                "source_mode": "git",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            output = io.StringIO()
            with (
                mock.patch.object(
                    sys,
                    "argv",
                    [
                        "fanout_samples.py",
                        "--config",
                        str(matrix),
                        "--worker-config",
                        str(root / "missing-workers.json"),
                    ],
                ),
                mock.patch("sys.stdout", output),
            ):
                self.assertEqual(main(), 0)
            report = json.loads(output.getvalue())
            self.assertFalse(report["authoritative"])
            self.assertEqual(
                report["unavailable_reason"], "no worker fleet is configured"
            )

    def test_worker_ids_are_safe_report_basenames(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            matrix_path = root / "matrix.json"
            worker_path = root / "workers.json"
            valid = ssh_worker(
                "safe", "user@host:~/literate-ai", "linux"
            ).definition.to_dict()
            valid["worker_id"] = "../escaped"
            worker_path.write_text(
                json.dumps(
                    {
                        "schema": ExecutionWorkerCatalog.SCHEMA,
                        "workers": [valid],
                    }
                ),
                encoding="utf-8",
            )
            matrix_path.write_text(
                json.dumps(
                    {
                        "schema": SCHEMA,
                        "default_samples": ["sample"],
                        "workers": [
                            {
                                "worker_id": "../escaped",
                                "platform_flavor": "linux",
                                "source_mode": "working-tree",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "worker_catalog_invalid"):
                _configuration(matrix_path, worker_path)
            with self.assertRaisesRegex(ValueError, "safe report basename"):
                _worker_log(root / "reports", "../escaped")
            self.assertFalse((root / "escaped.log").exists())

    def test_configuration_rejects_duplicate_canonical_destinations(self):
        aliases = (
            ("user@HOST:~/literate-ai", "user@host:~/literate-ai", "linux"),
            ("User@HOST:~/Literate-AI", "user@host:~/literate-ai", "windows"),
        )
        for first, second, platform_flavor in aliases:
            with self.subTest(platform_flavor=platform_flavor):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    matrix_path = root / "matrix.json"
                    worker_path = root / "workers.json"
                    first_worker = ssh_worker("first", first, platform_flavor, "git")
                    second_worker = ssh_worker(
                        "second", second, platform_flavor, "working-tree"
                    )
                    write_workers(worker_path, first_worker, second_worker)
                    write_matrix(matrix_path, first_worker, second_worker)
                    with self.assertRaisesRegex(
                        ValueError, "destinations must be unique"
                    ):
                        _configuration(matrix_path, worker_path)

    def test_failed_working_tree_result_never_claims_git_revision(self):
        identity = "sha256:" + "d" * 64
        target = ssh_worker(
            "working", "user@localhost:~/matrix", "macos", "working-tree"
        )
        with tempfile.TemporaryDirectory() as temporary:
            report_root = Path(temporary)
            with mock.patch(
                "scripts.fanout_samples._run_worker", side_effect=ValueError("boom")
            ):
                result = _record_worker(
                    target,
                    None,
                    identity,
                    "sha256:" + "e" * 64,
                    "f" * 64,
                    "run",
                    ("sample",),
                    report_root,
                    git_source(revision="e" * 40),
                    False,
                    30,
                )
        self.assertEqual(result["source_identity"], identity)
        self.assertIsNone(result["source_revision"])

    def test_report_keeps_mixed_source_authority_per_target(self):
        targets = (
            {
                "id": "local",
                "passed": True,
                "source_identity": "sha256:" + "a" * 64,
                "source_revision": None,
            },
            {
                "id": "remote",
                "passed": True,
                "source_identity": "sha256:" + "b" * 64,
                "source_revision": "c" * 40,
            },
        )
        report = _fanout_report(
            run_id="run",
            patterns=("sample",),
            results=targets,
            retain_workspace=False,
            timeout_seconds=3600,
        )
        self.assertEqual(report["schema"], "literate-ai/remote-sample-fanout-report@5")
        self.assertEqual(report["source_authority"], "per-worker")
        self.assertTrue(report["authoritative"])
        self.assertEqual(report["resumed_workers"], [])
        self.assertNotIn("source_revision", report)
        self.assertEqual(
            [worker["source_identity"] for worker in report["workers"]],
            ["sha256:" + "a" * 64, "sha256:" + "b" * 64],
        )

    def test_target_timeout_is_a_redacted_failed_result(self):
        target = ssh_worker("timed-out", "user@host:~/matrix", "linux", "git")
        secret = "credential-bearing-command"
        with tempfile.TemporaryDirectory() as temporary:
            report_root = Path(temporary)
            with mock.patch(
                "scripts.fanout_samples._run_worker",
                side_effect=subprocess.TimeoutExpired(["ssh", secret], 7),
            ):
                result = _record_worker(
                    target,
                    None,
                    None,
                    None,
                    "e" * 64,
                    "run",
                    ("sample",),
                    report_root,
                    git_source(revision="f" * 40),
                    False,
                    7,
                )
            log = Path(result["log"]).read_text(encoding="utf-8")
        self.assertFalse(result["passed"])
        self.assertEqual(result["failure_kind"], "timeout")
        self.assertIn("7-second wall-time limit", log)
        self.assertNotIn(secret, log)

    def test_remote_subprocesses_receive_the_remaining_target_deadline(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report_root = root / "reports"
            report_root.mkdir()
            archive = root / "repo.tar.gz"
            archive.write_bytes(b"archive")
            transport_identity = _archive_identity(archive)
            source_identity = "sha256:" + "a" * 64
            completed = subprocess.CompletedProcess(["remote"], 0, "", "")
            worker_completed = subprocess.CompletedProcess(
                ["ssh"],
                0,
                (
                    f"LITAI_SOURCE_TREE_IDENTITY={source_identity}\n"
                    'LITAI_REMOTE_STATUS={"returncode":0,'
                    '"schema":"literate-ai/remote-worker-status@1",'
                    '"state":"passed"}\n'
                ),
                "",
            )
            probe_completed = subprocess.CompletedProcess(
                ["ssh"], 0, "LITAI_WORKSPACE_STATE=absent\n", ""
            )
            with mock.patch(
                "scripts.fanout_samples.subprocess.run",
                side_effect=(
                    completed,
                    completed,
                    completed,
                    worker_completed,
                    probe_completed,
                ),
            ) as run:
                guard_digest = hashlib.sha256(
                    Path("src/literate_ai/remote_source_guard.py").read_bytes()
                ).hexdigest()
                result = _run_worker(
                    ssh_worker(
                        "bounded",
                        "user@host:~/matrix",
                        "linux",
                        "working-tree",
                    ),
                    archive,
                    source_identity,
                    transport_identity,
                    guard_digest,
                    "run",
                    ("sample",),
                    report_root,
                    None,
                    True,
                    19,
                )
        self.assertTrue(result["passed"])
        self.assertEqual(result["workspace_state"], "absent")
        self.assertFalse(result["workspace_retained"])
        self.assertEqual(result["remote_status"]["state"], "passed")
        self.assertEqual(len(run.call_args_list), 5)
        for call in run.call_args_list:
            self.assertIsInstance(call.kwargs["timeout"], int)
            self.assertGreater(call.kwargs["timeout"], 0)
            self.assertLessEqual(call.kwargs["timeout"], 19)
            self.assertIn("BatchMode=yes", call.args[0])
        self.assertIn("test ! -e", run.call_args_list[0].args[0][-1])
        self.assertTrue(
            run.call_args_list[1].args[0][-1].endswith(".incoming/repo.tar.gz")
        )

    def test_remaining_timeout_never_exceeds_the_original_budget(self):
        # A deadline struck from a large monotonic() base (e.g. long system uptime)
        # can, after floating-point subtraction, round a hair past the original
        # timeout_seconds even though no real time has elapsed -- this reproduces
        # that overshoot deterministically instead of depending on real clock skew.
        base = 123_456.000000000003
        deadline = base + 19
        with mock.patch("scripts.fanout_samples.time.monotonic", return_value=base):
            remaining = _remaining_timeout(deadline, 19)
        self.assertIsInstance(remaining, int)
        self.assertLessEqual(remaining, 19)
        self.assertGreaterEqual(remaining, 1)

    def test_remaining_timeout_truncates_to_whole_seconds(self):
        with mock.patch("scripts.fanout_samples.time.monotonic", return_value=100.0):
            remaining = _remaining_timeout(100.0 + 4.9, 19)
        self.assertEqual(remaining, 4)

    def test_remaining_timeout_expires_at_or_before_the_deadline(self):
        with mock.patch("scripts.fanout_samples.time.monotonic", return_value=100.0):
            with self.assertRaises(subprocess.TimeoutExpired):
                _remaining_timeout(100.0, 19)

    def test_configuration_rejects_unknown_source_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            matrix_path = root / "matrix.json"
            worker_path = root / "workers.json"
            worker = ssh_worker(
                "invalid", "user@host:~/literate-ai", "linux", "ambient"
            )
            write_workers(worker_path, worker)
            write_matrix(matrix_path, worker)
            with self.assertRaisesRegex(ValueError, "source mode"):
                _configuration(matrix_path, worker_path)

    def test_os_flavor_workers_start_in_parallel_and_return_config_order(self):
        targets = (
            ssh_worker("linux", "user@linux:~/literate-ai", "linux"),
            ssh_worker("windows", "user@windows:~/literate-ai", "windows"),
        )
        barrier = threading.Barrier(len(targets))

        def record(target, *_arguments):
            barrier.wait(timeout=10)
            return {"id": target.worker_id, "passed": True}

        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch("scripts.fanout_samples._record_worker", new=record):
                results = _execute_workers(
                    targets,
                    archive=Path(temporary) / "unused.tar.gz",
                    archive_source_identity="sha256:" + "a" * 64,
                    archive_transport_identity="sha256:" + "b" * 64,
                    guard_digest="c" * 64,
                    run_id="parallel-proof",
                    patterns=("sample",),
                    report_root=Path(temporary),
                    git_source=None,
                    jobs=None,
                    retain_workspace=False,
                    timeout_seconds=30,
                )
        self.assertEqual([result["id"] for result in results], ["linux", "windows"])

    def test_parallel_workers_stop_scheduling_after_failure_and_drain_in_flight(self):
        targets = tuple(
            ssh_worker(name, f"user@{name}:~/literate-ai", "linux")
            for name in ("fail", "pass", "unscheduled-a", "unscheduled-b")
        )
        started: list[str] = []
        checkpointed: list[str] = []
        initial_workers = threading.Barrier(2)
        failure_returning = threading.Event()

        def record(target, *_arguments):
            started.append(target.worker_id)
            initial_workers.wait(timeout=10)
            if target.worker_id == "fail":
                failure_returning.set()
            else:
                self.assertTrue(failure_returning.wait(timeout=10))
                # Let the failed future become observable before the in-flight success
                # returns. Wall-clock sleep ordering alone is scheduler-dependent.
                time.sleep(0.05)
            return {"id": target.worker_id, "passed": target.worker_id != "fail"}

        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch("scripts.fanout_samples._record_worker", new=record):
                results = _execute_workers(
                    targets,
                    archive=Path(temporary) / "unused.tar.gz",
                    archive_source_identity="sha256:" + "a" * 64,
                    archive_transport_identity="sha256:" + "b" * 64,
                    guard_digest="c" * 64,
                    run_id="fail-fast-proof",
                    patterns=("sample",),
                    report_root=Path(temporary),
                    git_source=None,
                    jobs=2,
                    retain_workspace=False,
                    timeout_seconds=30,
                    record_success=lambda result: checkpointed.append(
                        str(result["id"])
                    ),
                )
        self.assertCountEqual(started, ["fail", "pass"])
        self.assertEqual(checkpointed, ["pass"])
        self.assertEqual(
            [result.get("failure_kind") for result in results[2:]],
            ["not-run-after-failure", "not-run-after-failure"],
        )

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

    def test_resumed_target_is_not_reexecuted(self):
        targets = (
            ssh_worker("linux", "user@linux:~/literate-ai", "linux"),
            ssh_worker("windows", "user@windows:~/literate-ai", "windows"),
        )
        calls: list[str] = []

        def record(target, *_arguments):
            calls.append(target.worker_id)
            return {"id": target.worker_id, "passed": True}

        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch("scripts.fanout_samples._record_worker", new=record):
                results = _execute_workers(
                    targets,
                    archive=Path(temporary) / "unused.tar.gz",
                    archive_source_identity="sha256:" + "a" * 64,
                    archive_transport_identity="sha256:" + "b" * 64,
                    guard_digest="c" * 64,
                    run_id="resume-proof",
                    patterns=("sample",),
                    report_root=Path(temporary),
                    git_source=None,
                    jobs=2,
                    retain_workspace=False,
                    timeout_seconds=30,
                    resumed_results={"linux": {"id": "linux", "passed": True}},
                )
        self.assertEqual(calls, ["windows"])
        self.assertEqual([result["id"] for result in results], ["linux", "windows"])

    def test_working_tree_archive_has_one_literate_ai_root_and_excludes_caches(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "checkout"
            root.mkdir()
            (root / "tracked.txt").write_text(
                "current working tree\n", encoding="utf-8"
            )
            (root / "_build").mkdir()
            (root / "_build" / "ignored.txt").write_text("cache\n", encoding="utf-8")
            (root / "specs" / "build").mkdir(parents=True)
            (root / "specs" / "build" / "retained.md").write_text(
                "authored build policy\n", encoding="utf-8"
            )
            archive = Path(temporary) / "checkout.tar.gz"
            _archive(root, archive)
            identity = _archive_identity(archive)
            with tarfile.open(archive, "r:gz") as source:
                names = source.getnames()
        self.assertIn("literate-ai/tracked.txt", names)
        self.assertIn("literate-ai/specs/build/retained.md", names)
        self.assertNotIn("literate-ai/_build/ignored.txt", names)
        self.assertRegex(identity, r"^sha256:[0-9a-f]{64}$")

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

    @unittest.skipUnless(shutil.which("git"), "requires Git")
    def test_git_visible_archive_omits_an_ignored_absolute_venv_symlink(self):
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
            (repository / ".gitignore").write_text(".venv\n", encoding="utf-8")
            external_environment = root / "external-environment"
            external_environment.mkdir()
            (external_environment / "credential.txt").write_text(
                "host-only secret\n", encoding="utf-8"
            )
            environment_link = repository / ".venv"
            try:
                environment_link.symlink_to(
                    external_environment.resolve(), target_is_directory=True
                )
            except OSError as exc:
                self.skipTest(f"absolute directory symlinks unavailable: {exc}")
            self.assertTrue(Path(os.readlink(environment_link)).is_absolute())

            archive = root / "working-tree.tar.gz"
            identity = _archive(repository, archive)
            extracted = root / "extracted"
            extracted.mkdir()
            with tarfile.open(archive, "r:gz") as source:
                source.extractall(extracted)
                names = set(source.getnames())
            materialized = extracted / "literate-ai"

            self.assertFalse(
                any(name.startswith("literate-ai/.venv") for name in names)
            )
            self.assertFalse((materialized / ".venv").exists())
            self.assertNotIn("credential.txt", "\n".join(names))
            self.assertEqual(identity, source_tree_identity(materialized))

    def test_git_visible_archive_rejects_membership_changes_during_capture(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "tracked.txt").write_text("stable bytes\n", encoding="utf-8")
            archive = root / "working-tree.tar.gz"
            with mock.patch(
                "scripts.fanout_samples._git_visible_paths",
                side_effect=(("tracked.txt",), ("newly-visible.txt", "tracked.txt")),
            ):
                with self.assertRaisesRegex(ValueError, "changed while"):
                    _archive(root, archive)

    @unittest.skipUnless(shutil.which("git"), "requires Git")
    def test_working_tree_archive_rejects_a_nested_git_repository_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repository"
            nested = repository / "nested"
            nested.mkdir(parents=True)
            subprocess.run(
                ["git", "init"],
                cwd=repository,
                check=True,
                capture_output=True,
            )
            (nested / "specification.md").write_text(
                "nested source\n", encoding="utf-8"
            )
            archive = root / "nested.tar.gz"

            with self.assertRaisesRegex(ValueError, "must be the Git repository root"):
                _archive(nested, archive)

            self.assertFalse(archive.exists())

    def test_source_guard_rejects_nonportable_symlink_targets(self):
        cases = {
            "absolute-posix": ("link", b"/outside/repository"),
            "drive-qualified-forward-slash": ("link", b"C:/outside/repository"),
            "drive-qualified-backslash": ("link", b"C:\\outside\\repository"),
            "backslash-relative": ("nested/link", b"..\\outside"),
        }
        for label, (path, target) in cases.items():
            with self.subTest(label=label):
                with self.assertRaisesRegex(
                    SourceGuardError,
                    "not portable and repository-relative",
                ):
                    _symlink_target_identity(path, target)

    def test_source_guard_rejects_a_repository_escaping_symlink(self):
        with self.assertRaisesRegex(SourceGuardError, "escapes the repository"):
            _symlink_target_identity("link", b"../outside")

    def test_source_guard_accepts_parent_relative_symlink_within_repository(self):
        identity = _symlink_target_identity("nested/link", b"../shared.txt")

        self.assertEqual(
            identity,
            "sha256:" + hashlib.sha256(b"../shared.txt").hexdigest(),
        )

    def test_working_tree_identity_is_stable_and_transport_is_separate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            file = source / "specification.md"
            file.write_text("stable behavior\n", encoding="utf-8")
            first = root / "first.tar.gz"
            second = root / "second.tar.gz"
            first_source = _archive(source, first)
            self.assertEqual(first_source, source_tree_identity(source))
            os.utime(file, (file.stat().st_atime, file.stat().st_mtime + 10))
            second_source = _archive(source, second)
            first_transport = _archive_identity(first)
            second_transport = _archive_identity(second)
        self.assertEqual(first_source, second_source)
        self.assertNotEqual(first_transport, second_transport)

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

    @unittest.skipUnless(shutil.which("git"), "requires Git")
    def test_git_guard_rejects_materialized_bytes_outside_the_commit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            subprocess.run(
                ["git", "config", "user.email", "guard@example.invalid"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Guard Test"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "config", "core.autocrlf", "false"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            tracked = root / "tracked.txt"
            tracked.write_bytes(b"authority\n")
            subprocess.run(
                ["git", "add", "."], cwd=root, check=True, capture_output=True
            )
            subprocess.run(
                ["git", "commit", "-m", "authority"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            revision = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=root, text=True
            ).strip()
            identity = git_tree_identity(root, revision)
            self.assertEqual(
                verify_materialized_git_tree(root, revision, identity), identity
            )
            tracked.write_bytes(b"filtered-or-tampered\n")
            with self.assertRaisesRegex(
                SourceGuardError, "materialized source content"
            ):
                verify_materialized_git_tree(root, revision, identity)

    def test_supervisor_bounds_worker_and_records_timeout(self):
        with tempfile.TemporaryDirectory() as temporary:
            status = Path(temporary) / "status.json"
            returncode = supervise(
                1,
                status,
                [sys.executable, "-c", "import time; time.sleep(30)"],
            )
            record = json.loads(status.read_text(encoding="utf-8"))
        self.assertEqual(returncode, 124)
        self.assertEqual(record["state"], "timed-out")
        self.assertTrue(record["termination_confirmed"])

    def test_supervisor_timeout_kill_uses_validated_system32_helper_not_path(self):
        # Regression for #74: `supervise()` used to run
        # `["taskkill", "/PID", ..., "/T", "/F"]` unqualified, which resolves
        # from PATH on Windows -- a project-local `taskkill.exe` placed
        # earlier on PATH (the decoy fixture below, matching
        # test_process_tree.py's helper test) would no-op the timeout kill.
        # Drive `supervise()` itself (forcing the Windows branch via
        # `platform_name="nt"`) and prove it only ever invokes the resolved,
        # validated `%SystemRoot%\\System32\\taskkill.exe`, never the decoy.
        import literate_ai.remote_source_guard as remote_source_guard_module

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            windows = root / "Windows"
            system32 = windows / "System32"
            system32.mkdir(parents=True)
            taskkill = system32 / "taskkill.exe"
            taskkill.write_bytes(b"system taskkill")
            taskkill.chmod(0o755)
            decoy = root / "project-bin"
            decoy.mkdir()
            (decoy / "taskkill.exe").write_bytes(b"project decoy")
            environment = {"SystemRoot": str(windows), "PATH": str(decoy)}

            status = root / "status.json"
            calls = []

            def _fake_run(command, **kwargs):
                calls.append((command, kwargs))
                return subprocess.CompletedProcess(command, 0)

            with mock.patch.object(
                remote_source_guard_module.subprocess, "run", side_effect=_fake_run
            ):
                returncode = supervise(
                    1,
                    status,
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    platform_name="nt",
                    environment=environment,
                )
            record = json.loads(status.read_text(encoding="utf-8"))

        self.assertEqual(returncode, 124)
        self.assertEqual(record["state"], "timed-out")
        self.assertTrue(record["termination_confirmed"])
        self.assertEqual(len(calls), 1)
        invoked_command, invoked_options = calls[0]
        resolved_system32 = system32.resolve()
        resolved_taskkill = resolved_system32 / "taskkill.exe"
        self.assertEqual(invoked_command[0], str(resolved_taskkill))
        self.assertNotEqual(invoked_command[0], str(decoy / "taskkill.exe"))
        self.assertNotIn("PATH", invoked_options["env"])

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
            child_was_stopped = child_stopped.is_file()

        self.assertEqual(returncode, 124)
        self.assertTrue(child_was_stopped)

    def test_ci_checkouts_retain_the_pinned_historical_commit(self):
        workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
        # Check each checkout's own step, independently of its version spelling.
        # A global count could let another step conceal one shallow checkout.
        steps = re.split(r"(?m)^      - ", workflow)
        checkouts = [step for step in steps if "uses: actions/checkout@" in step]
        self.assertTrue(checkouts, "the workflow must contain actual checkouts")
        for step in checkouts:
            with self.subTest(checkout=step.splitlines()[0]):
                self.assertRegex(step, r"(?m)^          fetch-depth: 0\s*$")

    @unittest.skipUnless(
        sys.platform == "win32" and shutil.which("powershell.exe"),
        "requires native Windows PowerShell",
    )
    def test_encoded_powershell_command_executes_natively(self):
        command = _powershell_command("Write-Output 'native-command-ok'")
        completed = subprocess.run(
            command.split(), check=True, capture_output=True, text=True
        )
        self.assertIn("native-command-ok", completed.stdout)

    @unittest.skipUnless(
        sys.platform == "win32" and shutil.which("powershell.exe"),
        "requires native Windows PowerShell",
    )
    def test_windows_working_tree_command_executes_end_to_end(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            scripts = source / "scripts"
            framework = source / "src" / "literate_ai"
            scripts.mkdir(parents=True)
            framework.mkdir(parents=True)
            repository_guard = (
                Path(__file__).resolve().parents[2]
                / "src"
                / "literate_ai"
                / "remote_source_guard.py"
            )
            shutil.copy2(
                repository_guard,
                framework / "remote_source_guard.py",
            )
            (scripts / "remote_sample_worker.py").write_text(
                "print('windows-worker-ok')\n", encoding="utf-8"
            )
            archive = root / "repo.tar.gz"
            source_identity = _archive(source, archive)
            transport_identity = _archive_identity(archive)
            guard_digest = hashlib.sha256(
                (framework / "remote_source_guard.py").read_bytes()
            ).hexdigest()
            run_root = root / "runs" / "run-id"
            incoming = Path(str(run_root) + ".incoming")
            incoming.mkdir(parents=True)
            shutil.copy2(archive, incoming / "repo.tar.gz")
            shutil.copy2(
                framework / "remote_source_guard.py",
                incoming / "remote_source_guard.py",
            )

            command = _windows_command(
                str(run_root),
                ("sample",),
                "windows",
                transport_identity,
                source_identity,
                guard_digest,
                30,
            )
            completed = subprocess.run(
                command.split(), check=False, capture_output=True, text=True
            )
            self.assertEqual(
                completed.returncode,
                0,
                f"stdout={completed.stdout}\nstderr={completed.stderr}",
            )
            self.assertFalse(run_root.exists())

        self.assertIn("windows-worker-ok", completed.stdout)
        self.assertIn("LITAI_SOURCE_TREE_IDENTITY=", completed.stdout)
        self.assertIn("LITAI_REMOTE_STATUS=", completed.stdout)


if __name__ == "__main__":
    unittest.main()
