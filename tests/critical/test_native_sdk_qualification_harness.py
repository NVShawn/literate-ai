"""Keep native qualification failure diagnostics through child failure/timeout."""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class NativeSdkQualificationHarnessTests(unittest.TestCase):
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
