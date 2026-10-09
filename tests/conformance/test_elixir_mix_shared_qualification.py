"""Native refusal of independently compiled shared Hex dependency bytes."""

from __future__ import annotations

import io
import os
import subprocess
import tarfile
import tempfile
import unittest
import urllib.request
from dataclasses import replace
from pathlib import Path

from literate_ai.adapters.builders import (
    BuildError,
    MixProviderApplication,
    MixProviderLibrary,
    discover_elixir_toolchain,
    discover_hex_toolchain,
    discover_mix_toolchain,
)
from literate_ai.adapters.builders.python import canonical_tree_digest
from literate_ai.adapters.dependencies.hex_archive import verify_hex_archive
from literate_ai.adapters.dependencies.mix_lock import MixLockedPackage
from literate_ai.contracts import canonical_identity
from tests.conformance.test_elixir_mix_library_qualification import _build, _source
from tests.unit.test_elixir_mix_providers import _surface


@unittest.skipUnless(
    os.environ.get("LITERATE_AI_ELIXIR_MIX_QUALIFICATION") == "1",
    "set LITERATE_AI_ELIXIR_MIX_QUALIFICATION=1 for authorized native Mix proof",
)
class ElixirMixSharedQualificationTests(unittest.TestCase):
    def test_independently_compiled_shared_hex_bytes_refuse_consumer(self):
        package = MixLockedPackage(
            "decimal",
            "2.3.0",
            "3ad6255aa77b4a3c4f818171b12d237500e63525c2fd056699967a3e7ea20f62",
            "a4d66355cb29cb47c3cf30e71329e58361cfcb37c34235ef3bf1d7bf3773aeac",
            (),
        )
        cached = os.environ.get("LITAI_DECIMAL_QUALIFICATION_ARCHIVE")
        if cached:
            with Path(cached).open("rb") as stream:
                archive = stream.read(1024 * 1024 + 1)
        else:
            with urllib.request.urlopen(
                "https://repo.hex.pm/tarballs/decimal-2.3.0.tar", timeout=60
            ) as stream:
                archive = stream.read(1024 * 1024 + 1)
        evidence = verify_hex_archive(archive, package=package)
        self.assertEqual(evidence.outer_sha256, package.outer_sha256)
        with tarfile.open(fileobj=io.BytesIO(archive)) as outer:
            contents = outer.extractfile("contents.tar.gz").read()
        # Validate the whole inner inventory before writing any source.
        files = []
        total = 0
        with tarfile.open(fileobj=io.BytesIO(contents), mode="r:gz") as inner:
            for member in inner:
                relative = Path(member.name)
                self.assertFalse(relative.is_absolute())
                self.assertNotIn("..", relative.parts)
                self.assertNotIn("\\", member.name)
                self.assertTrue(member.isdir() or member.isfile())
                total += member.size
                self.assertLessEqual(total, 8 * 1024 * 1024)
                if member.isfile():
                    files.append((relative, inner.extractfile(member).read()))
                self.assertLessEqual(len(files), 1024)
        plugin = os.environ.get("LITAI_HEX_EBIN")
        self.assertTrue(plugin)
        tool = discover_hex_toolchain(
            discover_mix_toolchain(discover_elixir_toolchain()), ebin=Path(plugin)
        )
        with tempfile.TemporaryDirectory(prefix="mix-shared-") as directory:
            root = Path(directory).resolve()
            source = root / "decimal"
            source.mkdir()
            for relative, content in files:
                destination = source / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
            sources = sorted(str(path) for path in (source / "lib").rglob("*.ex"))
            self.assertTrue(sources)
            source_digest = canonical_tree_digest(source)
            applications = []
            compiler = (
                "[dest, debug | files] = System.argv(); "
                'Code.compiler_options(debug_info: debug == "true"); '
                "case Kernel.ParallelCompiler.compile_to_path(files, dest) do "
                "{:ok, _, _} -> :ok; other -> raise inspect(other) end"
            )
            for index, debug in enumerate(("true", "false")):
                ebin = root / str(index) / "ebin"
                ebin.mkdir(parents=True)
                tool.require_unchanged()
                compiled = subprocess.run(
                    (
                        *tool.mix.elixir.command,
                        "-e",
                        compiler,
                        "--",
                        str(ebin),
                        debug,
                        *sources,
                    ),
                    capture_output=True,
                    timeout=120,
                )
                self.assertEqual(compiled.returncode, 0, compiled.stderr)
                tool.require_unchanged()
                self.assertEqual(canonical_tree_digest(source), source_digest)
                modules = sorted(path.stem for path in ebin.glob("*.beam"))
                self.assertIn("Elixir.Decimal", modules)
                module_list = ",".join("'" + module + "'" for module in modules)
                (ebin / "decimal.app").write_text(
                    '{application,decimal,[{vsn,"2.3.0"},{modules,['
                    + module_list
                    + "]},{applications,[kernel,stdlib,elixir]}]}.\n"
                )
                observed = subprocess.run(
                    (
                        *tool.mix.elixir.command,
                        "-pa",
                        str(ebin),
                        "-e",
                        "IO.puts(Decimal.to_string(Decimal.add(20,22)))",
                    ),
                    capture_output=True,
                    timeout=60,
                )
                self.assertEqual(observed.returncode, 0, observed.stderr)
                self.assertEqual(observed.stdout.strip(), b"42")
                selected = MixProviderApplication(
                    "decimal", "2.3.0", ebin, canonical_tree_digest(ebin)
                )
                selected.require_unchanged()
                applications.append(selected)
            self.assertNotEqual(
                applications[0].tree_digest, applications[1].tree_digest
            )
            self.assertEqual(
                (applications[0].ebin / "decimal.app").read_bytes(),
                (applications[1].ebin / "decimal.app").read_bytes(),
            )
            providers = []
            for index, dependency in enumerate(applications):
                package_name, module = (
                    ("provider_api", "ProviderApi.Math")
                    if index == 0
                    else ("bridge_api", "BridgeApi.Math")
                )
                own_source = root / f"provider{index}.ex"
                own_source.write_text(
                    f"defmodule {module} do\n"
                    " def add(a,b), do: Decimal.to_integer(Decimal.add(a,b))\nend\n"
                )
                own_ebin = root / f"provider{index}" / "ebin"
                own_ebin.mkdir(parents=True)
                compiled = subprocess.run(
                    (
                        *tool.mix.elixir.command,
                        "-pa",
                        str(dependency.ebin),
                        "-e",
                        compiler,
                        "--",
                        str(own_ebin),
                        "false",
                        str(own_source),
                    ),
                    capture_output=True,
                    timeout=60,
                )
                self.assertEqual(compiled.returncode, 0, compiled.stderr)
                (own_ebin / f"{package_name}.app").write_text(
                    f'{{application,{package_name},[{{vsn,"1.0.0"}},'
                    f"{{modules,['Elixir.{module}']}}]}}.\n"
                )
                own = MixProviderApplication(
                    package_name, "1.0.0", own_ebin, canonical_tree_digest(own_ebin)
                )
                original_surface = _surface()
                surface = replace(
                    original_surface,
                    package=package_name,
                    capabilities=tuple(
                        replace(c, module=module) for c in original_surface.capabilities
                    ),
                )
                provider = MixProviderLibrary(
                    canonical_identity({"independent-compilation": index}),
                    surface,
                    own.ebin,
                    own.tree_digest,
                    (own, dependency),
                )
                provider.require_unchanged()
                providers.append(provider)
            # Independent roots share a version and authenticated source, but
            # the consumer must refuse their different compiled dependency bytes.
            consumer_source = root / "consumer"
            revision = _source(consumer_source, library=False)
            with self.assertRaises(BuildError) as caught:
                _build(
                    tool,
                    consumer_source,
                    root / "consumer-cas",
                    revision,
                    providers=tuple(providers),
                )
            self.assertEqual(caught.exception.code, "builder.mix_provider_conflict")
            tool.require_unchanged()
