"""Keep native qualification failure diagnostics through child failure/timeout."""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch


class NativeSdkQualificationHarnessTests(unittest.TestCase):
    def test_publication_preflight_requires_complete_unskipped_inventory(self):
        harness = self.harness()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            for summary in (
                {"selected": 4, "failures": 0, "errors": 0, "skipped": 0},
                {"selected": 5, "failures": 0, "errors": 0, "skipped": 1},
                {"selected": 5, "failures": 1, "errors": 0, "skipped": 0},
                {"selected": 5, "failures": 0, "errors": 1, "skipped": 0},
                {
                    "selected": True,
                    "failures": False,
                    "errors": False,
                    "skipped": False,
                },
            ):
                with self.subTest(summary=summary):
                    (output / "publication-preflight.json").write_text(
                        json.dumps(summary)
                    )
                    with (
                        patch.object(harness, "command"),
                        self.assertRaisesRegex(ValueError, "inventory"),
                    ):
                        harness.publication_preflight(output, output)

    def test_publication_preflight_preserves_actual_child_failure(self):
        harness = self.harness()
        original = harness.command
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)

            def failed(name, argv, destination, timeout):
                self.assertIn("tests.critical.test_exclusive_directory", argv[3])
                self.assertIn("publication.__file__", argv[3])
                original(
                    name,
                    [
                        sys.executable,
                        "-c",
                        "print('publication unavailable');raise SystemExit(17)",
                    ],
                    destination,
                    timeout,
                )

            with patch.object(harness, "command", side_effect=failed):
                with self.assertRaisesRegex(RuntimeError, "exit 17"):
                    harness.publication_preflight(output, output)
            terminal = json.loads(
                (output / "publication-preflight.terminal.json").read_text()
            )
            self.assertEqual(terminal["exit_code"], 17)
            self.assertFalse(terminal["timed_out"])
            self.assertIn(
                b"publication unavailable",
                (output / "publication-preflight.stdout").read_bytes(),
            )

    def test_acl_probe_rejects_wrong_owner_platform_and_uid_before_writes(self):
        harness = self.harness()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result"
            for platform, uid, secondary in (
                ("freebsd14", 1001, "1002"),
                ("linux", 0, "1001"),
                ("freebsd14", 0, "0"),
            ):
                with (
                    self.subTest(platform=platform, uid=uid, secondary=secondary),
                    patch.object(harness.sys, "platform", platform),
                    patch.object(harness.os, "geteuid", return_value=uid, create=True),
                    patch.object(harness, "command") as command,
                    self.assertRaises(SystemExit) as refusal,
                ):
                    harness.root_acl_preflight(
                        [
                            "--parent",
                            directory,
                            "--uid",
                            secondary,
                            "--output",
                            str(output),
                        ]
                    )
                self.assertEqual(refusal.exception.code, 2)
                command.assert_not_called()
                self.assertFalse(output.exists())

    def test_root_acl_probe_retains_real_child_failure_for_copyback(self):
        harness = self.harness()
        original_command = harness.command
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result"

            def fail(name, argv, destination, timeout):
                self.assertEqual(argv[-2:], [directory, "1001"])
                self.assertIn("preflight_freebsd_posix_acl", argv[3])
                original_command(
                    name,
                    [
                        sys.executable,
                        "-c",
                        "print('partial ACL probe');raise SystemExit(17)",
                    ],
                    destination,
                    timeout,
                )

            with (
                patch.object(harness.sys, "platform", "freebsd14"),
                patch.object(harness.os, "geteuid", return_value=0, create=True),
                patch.object(harness, "command", side_effect=fail),
                self.assertRaisesRegex(RuntimeError, "exit 17"),
            ):
                harness.root_acl_preflight(
                    ["--parent", directory, "--uid", "1001", "--output", str(output)]
                )
            terminal = json.loads((output / "acl-preflight.terminal.json").read_text())
            self.assertEqual(terminal["exit_code"], 17)
            self.assertFalse(terminal["timed_out"])
            self.assertIn(
                b"partial ACL probe", (output / "acl-preflight.stdout").read_bytes()
            )

    def test_sqlite_preflight_executes_binding_and_refuses_missing_extension(self):
        workflow = (
            self.collector().parents[2] / "workflows/native-sdk-qualification.yml"
        )
        line = next(
            line.strip()
            for line in workflow.read_text(encoding="utf-8").splitlines()
            if " -c " in line and "import sqlite3" in line
        )
        arguments = shlex.split(line)
        code = arguments[arguments.index("-c") + 1]
        healthy = subprocess.run(
            [sys.executable, "-I", "-c", code],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(healthy.returncode, 0, healthy.stderr)
        missing = (
            "import importlib.abc,sys\n"
            "class MissingExtension(importlib.abc.MetaPathFinder):\n"
            " def find_spec(self,fullname,path=None,target=None):\n"
            "  if fullname=='_sqlite3': raise ModuleNotFoundError('_sqlite3 missing')\n"
            "sys.meta_path.insert(0,MissingExtension())\n"
            f"exec({code!r})\n"
        )
        refused = subprocess.run(
            [sys.executable, "-I", "-c", missing],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("_sqlite3 missing", refused.stderr)

    @staticmethod
    def collector():
        return Path(__file__).resolve().parents[2] / (
            ".github/qualifications/native_sdk/collect_results.py"
        )

    def check_collected(self, destination):
        return subprocess.run(
            [
                sys.executable,
                str(self.collector()),
                "--destination",
                str(destination),
                "--check",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )

    @unittest.skipIf(os.name == "nt", "exercise the actual POSIX guest exit trap")
    def test_failed_guest_exports_diagnostics_before_host_refuses_original_exit(self):
        workflow = (
            self.collector().parents[2] / "workflows/native-sdk-qualification.yml"
        )
        content = workflow.read_text(encoding="utf-8")
        marker = "          run: |\n            set -eu\n            finish_guest()"
        start = content.index(marker) + len("          run: |\n")
        finish = content.index("            source_checkout=$(pwd -P)", start)
        trap = textwrap.dedent(content[start:finish])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "result"
            source.mkdir()
            (source / "initialize.stdout").write_bytes(b"partial output")
            (source / "initialize.stderr").write_bytes(b"actual refusal")
            (source / "client").write_bytes(b"private fixture key must stay private")
            trap = trap.replace(
                ".github/qualifications/native_sdk/collect_results.py",
                shlex.quote(str(self.collector())),
            ).replace("/home/litai-qual/qualification/result", shlex.quote(str(source)))
            script = trap + (
                f"\nqualification_python={shlex.quote(sys.executable)}\n"
                f"{shlex.quote(sys.executable)} -c 'raise SystemExit(17)'\n"
            )
            guest = subprocess.run(
                ["sh", "-c", script], cwd=root, capture_output=True, timeout=20
            )
            # Transport succeeds so the action reaches copyback; host still fails.
            self.assertEqual(guest.returncode, 0, guest.stderr)
            destination = root / "qualification-results"
            terminal = json.loads((destination / "guest-terminal.json").read_bytes())
            self.assertEqual(terminal["guest_exit_code"], 17)
            self.assertTrue(terminal["collection_complete"])
            self.assertEqual(
                (destination / "initialize.stdout").read_bytes(), b"partial output"
            )
            self.assertEqual(
                (destination / "initialize.stderr").read_bytes(), b"actual refusal"
            )
            self.assertFalse((destination / "client").exists())
            host = self.check_collected(destination)
            self.assertNotEqual(host.returncode, 0)
            self.assertIn("exit 17", host.stderr)

    def collect_fixture(self, source, destination, status=0):
        return subprocess.run(
            [
                sys.executable,
                str(self.collector()),
                "--source",
                str(source),
                "--destination",
                str(destination),
                "--guest-exit-code",
                str(status),
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )

    def test_copied_acceptance_must_pass_and_remain_byte_identical(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, destination = root / "result", root / "copy"
            source.mkdir()
            (source / "result.json").write_text(
                json.dumps(
                    {"schema": "literate-ai/native-sdk-qualification@1", "passed": True}
                ),
                encoding="utf-8",
            )
            (source / "initialize.stdout").write_bytes(b"complete output")
            collected = self.collect_fixture(source, destination)
            self.assertEqual(collected.returncode, 0, collected.stderr)
            self.assertEqual(self.check_collected(destination).returncode, 0)
            (destination / "initialize.stdout").write_bytes(b"changed output")
            refused = self.check_collected(destination)
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("changed during copyback", refused.stderr)
            # A failed repeated collection must replace an earlier success verdict.
            self.assertNotEqual(self.collect_fixture(source, destination).returncode, 0)
            terminal = json.loads((destination / "guest-terminal.json").read_bytes())
            self.assertFalse(terminal["collection_complete"])
            self.assertNotEqual(self.check_collected(destination).returncode, 0)

    def test_zero_exit_without_acceptance_does_not_pass_host_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, destination = root / "missing-result", root / "copy"
            self.assertEqual(self.collect_fixture(source, destination).returncode, 0)
            self.assertNotEqual(self.check_collected(destination).returncode, 0)

    @unittest.skipIf(os.name == "nt", "actual POSIX directory symlink refusal")
    def test_symlinked_whitelisted_evidence_is_never_exported_or_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, destination, private = (
                root / "result",
                root / "copy",
                root / "private",
            )
            (source / "transport").mkdir(parents=True)
            private.mkdir()
            (private / "client.json").write_bytes(b"private fixture key")
            (source / "transport/qualification").symlink_to(
                private, target_is_directory=True
            )
            refused = self.collect_fixture(source, destination)
            self.assertNotEqual(refused.returncode, 0)
            self.assertFalse(
                (destination / "transport/qualification/client.json").exists()
            )
            terminal = json.loads((destination / "guest-terminal.json").read_bytes())
            self.assertFalse(terminal["collection_complete"])
            self.assertNotEqual(self.check_collected(destination).returncode, 0)

    def test_foreign_owned_framework_staging_trust_is_exact_and_command_scoped(self):
        workflow = Path(__file__).resolve().parents[2] / (
            ".github/workflows/native-sdk-qualification.yml"
        )
        clone = next(
            line.strip()
            for line in workflow.read_text(encoding="utf-8").splitlines()
            if " clone --bare --no-hardlinks " in line
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source, unrelated = root / "source", root / "unrelated"
            work = root / "work"
            work.mkdir()
            global_config = root / "global-config"
            global_config.write_bytes(b"# Existing operator configuration\n")
            original = global_config.read_bytes()
            environment = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("GIT_")
            }
            environment.update(
                GIT_CONFIG_GLOBAL=str(global_config),
                GIT_CONFIG_NOSYSTEM="1",
                GIT_TEST_ASSUME_DIFFERENT_OWNER="1",
                GIT_TERMINAL_PROMPT="0",
            )

            def git(*argv):
                return subprocess.run(
                    ["git", *argv],
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )

            for path in (source, unrelated):
                self.assertEqual(git("init", "--template=", str(path)).returncode, 0)
            (source / "input").write_bytes(b"Exact framework fixture\n")
            trusted = ("-c", f"safe.directory={source.as_posix()}", "-C", str(source))
            self.assertEqual(git(*trusted, "add", "input").returncode, 0)
            committed = git(
                *trusted,
                "-c",
                "user.name=Qualification fixture",
                "-c",
                "user.email=qualification@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-m",
                "Fixture",
            )
            self.assertEqual(committed.returncode, 0, committed.stderr)
            expected = git(*trusted, "rev-parse", "HEAD").stdout.strip()
            # The original checkout-only trust fails in upload-pack, which sees .git.
            refused = git(
                "-c",
                f"safe.directory={source.as_posix()}",
                "clone",
                "--bare",
                "--no-hardlinks",
                str(source),
                str(work / "old"),
            )
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("dubious ownership", refused.stderr)
            argv = [
                part.replace("$source_checkout", source.as_posix()).replace(
                    "$work", work.as_posix()
                )
                for part in shlex.split(clone)
            ]
            accepted = git(*argv[1:])
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            actual = git(
                "-c",
                f"safe.directory={(work / 'framework.git').as_posix()}",
                "--git-dir",
                str(work / "framework.git"),
                "rev-parse",
                "HEAD",
            )
            self.assertEqual(actual.returncode, 0, actual.stderr)
            self.assertEqual(actual.stdout.strip(), expected)
            argv[-2:] = [str(unrelated), str(work / "unrelated.git")]
            refused = git(*argv[1:])
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("dubious ownership", refused.stderr)
            self.assertEqual(global_config.read_bytes(), original)

    @staticmethod
    def harness():
        path = Path(__file__).resolve().parents[2] / (
            ".github/qualifications/native_sdk/run_cell.py"
        )
        spec = importlib.util.spec_from_file_location("native_sdk_cell", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_failed_real_child_retains_both_streams_and_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with self.assertRaisesRegex(RuntimeError, "exit 17"):
                self.harness().command(
                    "failure",
                    [
                        sys.executable,
                        "-c",
                        "import os;os.write(1,b'out');os.write(2,b'err');"
                        "raise SystemExit(17)",
                    ],
                    output,
                    10,
                )
            self.assertEqual((output / "failure.stdout").read_bytes(), b"out")
            self.assertEqual((output / "failure.stderr").read_bytes(), b"err")
            terminal = json.loads((output / "failure.terminal.json").read_bytes())
            self.assertEqual(terminal["exit_code"], 17)
            self.assertFalse(terminal["timed_out"])

    def test_timed_out_real_child_retains_partial_streams_and_failed_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with self.assertRaises(subprocess.TimeoutExpired):
                self.harness().command(
                    "timeout",
                    [
                        sys.executable,
                        "-c",
                        "import os,time;os.write(1,b'partial-out');"
                        "os.write(2,b'partial-err');time.sleep(30)",
                    ],
                    output,
                    3,
                )
            self.assertEqual((output / "timeout.stdout").read_bytes(), b"partial-out")
            self.assertEqual((output / "timeout.stderr").read_bytes(), b"partial-err")
            terminal = json.loads((output / "timeout.terminal.json").read_bytes())
            self.assertIsNone(terminal["exit_code"])
            self.assertTrue(terminal["timed_out"])
