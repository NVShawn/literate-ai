"""Hex payload, native compatibility, and installation provenance guards."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.adapters.builders import BuildError, discover_hex_toolchain
from literate_ai.adapters.builders._process import BoundedProcessResult
from literate_ai.adapters.builders.elixir import ElixirToolchain
from literate_ai.adapters.builders.mix import MixToolchain


class HexToolchainTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.ebin = self.root / "ebin"
        self.ebin.mkdir()
        self.modules = tuple(
            self.ebin / f"Elixir.{name}.beam"
            for name in (
                "Hex",
                "Hex.Application",
                "Hex.State",
                "Hex.Registry.Server",
                "Hex.SCM",
                "Hex.Solver",
                "Hex.RemoteConverger",
                "Hex.HTTP",
                "Hex.Tar",
                "Mix.Tasks.Hex.Build",
            )
        )
        for path in (*self.modules, self.ebin / "hex.app"):
            path.write_bytes(b"selected-plugin")
        digest = "sha256:" + "1" * 64
        elixir = ElixirToolchain(
            (str(self.root / "elixir"),),
            "1.18.0",
            (1, 18, 0),
            "27",
            ((str(self.root / "elixir"), digest),),
        )
        self.mix = MixToolchain(
            elixir,
            elixir.version,
            tuple((str(self.root / name), digest) for name in ("mix", "a", "b", "c")),
        )
        self.record = {"version": "2.5.1", "files": list(map(str, self.modules))}
        guard = mock.patch.object(MixToolchain, "require_unchanged")
        self.guard = guard.start()
        self.addCleanup(guard.stop)
        process = mock.patch(
            "literate_ai.adapters.builders.hex.run_bounded_process",
            side_effect=lambda *_args, **_kwargs: BoundedProcessResult(
                0, json.dumps(self.record).encode(), b""
            ),
        )
        self.process = process.start()
        self.addCleanup(process.stop)

    def discover(self):
        return discover_hex_toolchain(self.mix, ebin=self.ebin, environment={})

    def test_explicit_plugin_command_and_complete_payload_binding(self):
        extra = self.ebin / "Elixir.Hex.Resolver.beam"
        extra.write_bytes(b"resolver")
        tool = self.discover()
        self.assertEqual(
            tool.command,
            (*self.mix.elixir.command, "-pa", str(self.ebin), self.mix.command[-1]),
        )
        self.assertEqual(len(tool.file_bindings), 12)
        self.assertEqual(tool.to_dict()["mix_toolchain_identity"], self.mix.identity)
        self.assertTrue(tool.identity.startswith("sha256:"))
        self.assertIsNone(self.process.call_args.kwargs["cwd"])
        self.assertGreaterEqual(self.guard.call_count, 2)

    def test_critical_module_failure_rejects_even_when_version_is_available(self):
        self.process.side_effect = None
        self.process.return_value = BoundedProcessResult(
            1, b"2.5.1\n", b"Hex.State must be recompiled for this OTP"
        )
        with self.assertRaises(BuildError):
            self.discover()

    def test_ambient_module_cannot_replace_retained_module(self):
        other = self.root / "Elixir.Hex.State.beam"
        other.write_bytes(b"ambient")
        self.record["files"][2] = str(other)
        with self.assertRaises(BuildError):
            self.discover()

    def test_absent_payload_does_not_fall_back_to_ambient_archive(self):
        self.modules[2].unlink()
        with self.assertRaises(BuildError):
            self.discover()
        self.process.assert_not_called()

    def test_unprobed_module_change_addition_and_removal_invalidate_binding(self):
        extra = self.ebin / "Elixir.Hex.Resolver.beam"
        extra.write_bytes(b"before")
        tool = self.discover()
        extra.write_bytes(b"after")
        with self.assertRaisesRegex(BuildError, "changed after selection"):
            tool.require_unchanged({})
        extra.write_bytes(b"before")
        extra.unlink()
        with self.assertRaisesRegex(BuildError, "changed after selection"):
            tool.require_unchanged({})
        extra.write_bytes(b"before")
        tool.require_unchanged({})
        (self.ebin / "Elixir.Hex.NewTask.beam").write_bytes(b"added")
        with self.assertRaisesRegex(BuildError, "changed after selection"):
            tool.require_unchanged({})

    def test_payload_change_during_probe_is_rejected(self):
        def mutate(*_args, **_kwargs):
            self.modules[2].write_bytes(b"changed-during-probe")
            return BoundedProcessResult(0, json.dumps(self.record).encode(), b"")

        self.process.side_effect = mutate
        with self.assertRaises(BuildError):
            self.discover()

    def test_symlink_payload_and_directory_are_rejected(self):
        self.modules[2].unlink()
        external = self.root / "external.beam"
        external.write_bytes(b"external")
        try:
            self.modules[2].symlink_to(external)
        except OSError:
            self.skipTest("host does not permit symlink creation")
        with self.assertRaises(BuildError):
            self.discover()
        self.modules[2].unlink()
        self.modules[2].write_bytes(b"selected-plugin")
        alias = self.root / "alias"
        alias.symlink_to(self.ebin, target_is_directory=True)
        with self.assertRaises(BuildError):
            discover_hex_toolchain(self.mix, ebin=alias, environment={})

    def test_selected_mix_drift_propagates_on_recheck(self):
        tool = self.discover()
        self.guard.side_effect = BuildError("builder.mix_toolchain_changed", "drift")
        with self.assertRaisesRegex(BuildError, "became unavailable"):
            tool.require_unchanged({})


if __name__ == "__main__":
    unittest.main()
