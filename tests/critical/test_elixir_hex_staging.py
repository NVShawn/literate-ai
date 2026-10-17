"""CI staging must reject untrusted bytes before replacing a plugin."""

from __future__ import annotations

import hashlib
import io
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from scripts import stage_elixir_hex


def _archive(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


class ElixirHexStagingTests(unittest.TestCase):
    def test_source_build_uses_versioned_archive_and_preserves_failed_build(self):
        source = io.BytesIO()
        with tarfile.open(fileobj=source, mode="w:gz") as package:
            member = tarfile.TarInfo(f"hex-{stage_elixir_hex.SOURCE_COMMIT}/mix.exs")
            member.size = 7
            package.addfile(member, io.BytesIO(b"project"))
        archive = source.getvalue()
        tool = mock.Mock(command=("bound-erl", "bound-mix"))

        def compile_archive(command, **kwargs):
            destination = Path(command[-1])
            self.assertEqual(destination.name, "hex-2.5.1.ez")
            self.assertEqual(command[:2], ["bound-erl", "bound-mix"])
            self.assertEqual(kwargs["env"]["MIX_ENV"], "prod")
            self.assertEqual(kwargs["env"]["HEX_OFFLINE"], "1")
            with zipfile.ZipFile(destination, "w") as built:
                built.writestr("hex-2.5.1/ebin/hex.app", b"compiled")

        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(
                stage_elixir_hex, "SOURCE_SHA256", hashlib.sha256(archive).hexdigest()
            ),
            mock.patch(
                "literate_ai.adapters.builders.elixir.discover_elixir_toolchain"
            ),
            mock.patch(
                "literate_ai.adapters.builders.mix.discover_mix_toolchain",
                return_value=tool,
            ),
            mock.patch.object(
                stage_elixir_hex.subprocess, "run", side_effect=compile_archive
            ) as execute,
        ):
            output = Path(directory)
            ebin = stage_elixir_hex.build_source(output, archive)
            self.assertEqual((ebin / "hex.app").read_bytes(), b"compiled")
            self.assertEqual(tool.require_unchanged.call_count, 2)
            execute.side_effect = stage_elixir_hex.subprocess.CalledProcessError(1, [])
            with self.assertRaises(stage_elixir_hex.subprocess.CalledProcessError):
                stage_elixir_hex.build_source(output, archive)
            self.assertEqual((ebin / "hex.app").read_bytes(), b"compiled")

    def test_source_digest_refusal_preserves_plugin_and_never_executes(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            marker = output / "hex-2.5.1/ebin/hex.app"
            marker.parent.mkdir(parents=True)
            marker.write_bytes(b"retained")
            with mock.patch.object(stage_elixir_hex.subprocess, "run") as execute:
                with self.assertRaisesRegex(ValueError, "source digest mismatch"):
                    stage_elixir_hex.build_source(output, b"tampered")
                execute.assert_not_called()
            self.assertEqual(marker.read_bytes(), b"retained")

    def test_source_refuses_unsafe_members_before_execution(self):
        root = f"hex-{stage_elixir_hex.SOURCE_COMMIT}"
        for name, kind in (
            (f"{root}/../escape", tarfile.REGTYPE),
            (f"/{root}/absolute", tarfile.REGTYPE),
            (f"{root}/link", tarfile.SYMTYPE),
            (f"{root}/hardlink", tarfile.LNKTYPE),
            (f"{root}/device", tarfile.CHRTYPE),
        ):
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w:gz") as package:
                member = tarfile.TarInfo(name)
                member.type = kind
                package.addfile(member)
            archive = buffer.getvalue()
            with (
                self.subTest(name=name, kind=kind),
                tempfile.TemporaryDirectory() as directory,
                mock.patch.object(
                    stage_elixir_hex,
                    "SOURCE_SHA256",
                    hashlib.sha256(archive).hexdigest(),
                ),
                mock.patch.object(stage_elixir_hex.subprocess, "run") as execute,
            ):
                output = Path(directory) / "output"
                with self.assertRaisesRegex(ValueError, "Unsafe"):
                    stage_elixir_hex.build_source(output, archive)
                execute.assert_not_called()
                self.assertFalse(output.exists())

    def test_digest_mismatch_preserves_existing_plugin(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            plugin = output / "hex-2.5.1/ebin"
            plugin.mkdir(parents=True)
            marker = plugin / "hex.app"
            marker.write_bytes(b"retained")
            with self.assertRaisesRegex(ValueError, "digest mismatch"):
                stage_elixir_hex.stage(output, b"tampered")
            self.assertEqual(marker.read_bytes(), b"retained")

    def test_restage_removes_stale_modules_without_touching_siblings(self):
        archive = _archive({"hex-2.5.1/ebin/hex.app": b"test app"})
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(
                stage_elixir_hex, "SHA256", hashlib.sha256(archive).hexdigest()
            ),
        ):
            output = Path(directory)
            ebin = stage_elixir_hex.stage(output, archive)
            (ebin / "stale.beam").write_bytes(b"old")
            sibling = output / "other-package"
            sibling.write_bytes(b"keep")
            self.assertEqual(stage_elixir_hex.stage(output, archive), ebin)
            self.assertFalse((ebin / "stale.beam").exists())
            self.assertEqual(sibling.read_bytes(), b"keep")

    def test_unsafe_member_is_rejected_before_any_output(self):
        for name in (
            "hex-2.5.1/../escaped",
            "/hex-2.5.1/absolute",
            "other/ebin/hex.app",
        ):
            archive = _archive({name: b"untrusted"})
            with (
                self.subTest(name=name),
                tempfile.TemporaryDirectory() as directory,
                mock.patch.object(
                    stage_elixir_hex, "SHA256", hashlib.sha256(archive).hexdigest()
                ),
            ):
                output = Path(directory) / "output"
                with self.assertRaisesRegex(ValueError, "Unsafe"):
                    stage_elixir_hex.stage(output, archive)
                self.assertFalse(output.exists())
