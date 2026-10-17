"""Mix installation custody and inert native discovery qualification."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import chdir
from pathlib import Path
from unittest import mock

from literate_ai.adapters.builders import (
    BuildError,
    discover_elixir_toolchain,
    discover_mix_toolchain,
)
from literate_ai.adapters.builders._process import BoundedProcessResult
from literate_ai.adapters.builders.elixir import ElixirToolchain


class MixToolchainTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / "bin").mkdir()
        (self.root / "bin/elixir").write_bytes(b"selected-elixir")
        self.script = self.root / "bin/mix"
        self.script.write_bytes(b"Mix.CLI.main()")
        self.ebin = self.root / "lib/mix/ebin"
        self.ebin.mkdir(parents=True)
        self.modules = tuple(
            self.ebin / name
            for name in (
                "Elixir.Mix.CLI.beam",
                "Elixir.Mix.Project.beam",
                "Elixir.Mix.Dep.Lock.beam",
            )
        )
        for path in (*self.modules, self.ebin / "mix.app"):
            path.write_bytes(b"installation-payload")
        self.elixir = ElixirToolchain(
            (str(self.root / "bin/elixir"),),
            "1.18.0",
            (1, 18, 0),
            "27",
            ((str(self.root / "bin/elixir"), "sha256:" + "1" * 64),),
        )
        self.record = {"version": "1.18.0", "files": list(map(str, self.modules))}
        guard = mock.patch.object(ElixirToolchain, "require_unchanged")
        guard.start()
        self.addCleanup(guard.stop)
        process = mock.patch(
            "literate_ai.adapters.builders.mix.run_bounded_process",
            side_effect=lambda *_args, **_kwargs: BoundedProcessResult(
                0, json.dumps(self.record).encode(), b""
            ),
        )
        self.process = process.start()
        self.addCleanup(process.stop)

    def test_command_uses_selected_elixir_and_complete_mix_payload(self):
        tool = discover_mix_toolchain(self.elixir, {})
        self.assertEqual(tool.command, (*self.elixir.command, str(self.script)))
        self.assertEqual(len(tool.file_bindings), 5)
        self.assertEqual(
            tool.to_dict()["elixir_toolchain_identity"], self.elixir.identity
        )
        self.assertTrue(tool.identity.startswith("sha256:"))
        self.assertTrue(
            all(call.kwargs["cwd"] is None for call in self.process.call_args_list)
        )

    def test_missing_explicit_pin_never_falls_back(self):
        with self.assertRaisesRegex(BuildError, "explicit Mix pin"):
            discover_mix_toolchain(self.elixir, {"MIX": str(self.root / "missing")})
        self.process.assert_not_called()

    def test_another_installation_pin_is_rejected_even_with_matching_bytes(self):
        other = self.root / "other-mix"
        other.write_bytes(self.script.read_bytes())
        with self.assertRaisesRegex(BuildError, "explicit Mix pin"):
            discover_mix_toolchain(self.elixir, {}, pinned_command=(str(other),))

    def test_another_installation_module_origin_is_rejected(self):
        other = self.root / "other-module.beam"
        other.write_bytes(b"ambient")
        self.record["files"][0] = str(other)
        with self.assertRaisesRegex(BuildError, "selected Elixir installation"):
            discover_mix_toolchain(self.elixir, {})

    def test_version_mismatch_is_rejected(self):
        self.record["version"] = "1.18.1"
        with self.assertRaises(BuildError):
            discover_mix_toolchain(self.elixir, {})

    def test_compiler_task_drift_and_payload_additions_are_rejected(self):
        task = self.ebin / "Elixir.Mix.Tasks.Compile.beam"
        task.write_bytes(b"before")
        tool = discover_mix_toolchain(self.elixir, {})
        task.write_bytes(b"after")
        with self.assertRaisesRegex(BuildError, "changed after selection"):
            tool.require_unchanged({})
        task.write_bytes(b"before")
        tool.require_unchanged({})
        (self.ebin / "Elixir.Mix.Tasks.Other.beam").write_bytes(b"added")
        with self.assertRaisesRegex(BuildError, "changed after selection"):
            tool.require_unchanged({})

    def test_missing_mix_application_fails_with_typed_error(self):
        shutil.rmtree(self.ebin)
        with self.assertRaisesRegex(BuildError, "absent"):
            discover_mix_toolchain(self.elixir, {})


@unittest.skipUnless(
    os.environ.get("LITERATE_AI_ELIXIR_QUALIFICATION") == "1",
    "set LITERATE_AI_ELIXIR_QUALIFICATION=1 for native Mix proof",
)
class MixNativeQualificationTests(unittest.TestCase):
    def test_bound_mix_does_not_evaluate_project(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "mix.exs").write_text(
                'File.write!("unexpected-project-evaluation", "executed")\n'
                'raise "must not run"\n',
                encoding="utf-8",
            )
            with chdir(root):
                tool = discover_mix_toolchain(discover_elixir_toolchain())
            expected = os.environ.get("LITAI_ELIXIR_EXPECT_VERSION")
            otp = os.environ.get("LITAI_ELIXIR_EXPECT_OTP")
            if expected:
                self.assertEqual(tool.version, expected)
            if otp:
                self.assertEqual(tool.elixir.otp_version, otp)
            result = subprocess.run(
                (*tool.command, "--version"),
                cwd=root,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Mix " + tool.version, result.stdout)
            self.assertFalse((root / "unexpected-project-evaluation").exists())
        tool.require_unchanged()
