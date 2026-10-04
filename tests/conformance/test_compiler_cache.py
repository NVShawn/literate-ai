"""Real sccache compilation, cache statistics, and private process custody."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from literate_ai.adapters import compiler_cache as module
from literate_ai.adapters.compiler_cache import compiler_cache_session
from literate_ai.adapters.dependencies import HostDependencyObservation
from literate_ai.adapters.multi_entrypoint_build import standalone_driver_source
from literate_ai.adapters.shared_cache_config import load_shared_cache
from literate_ai.adapters.standard_project import (
    _STANDARD_BUILD_DRIVER,
    _encoded_multi_entrypoint_outputs,
    _encoded_toolchain_environment,
)
from literate_ai.contracts.shared_cache import (
    SharedCacheAccessMode,
    SharedCacheNamespace,
)
from tests.support.fixtures_test_shared_cache import _configuration


@unittest.skipUnless(
    os.environ.get("LITAI_SCCACHE") or shutil.which("sccache"),
    "sccache is required for compiler-cache qualification",
)
class CompilerCacheConformanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="cc-proof-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / "shared-cache.json").write_text(
            json.dumps(
                _configuration(endpoint=None, credential_reference=None).to_dict()
            )
        )
        self.environment = dict(os.environ) | {
            "LITAI_CONFIG_DIR": str(self.root),
            "LITAI_CACHE_DIR": str(self.root / "cache"),
        }
        self.environment.pop("LITAI_SHARED_CACHE_CONFIG", None)
        self.binding = load_shared_cache(
            environment=self.environment
        ).bind_compiler_tool()

    def run_compile(self, command, environment=None, *, workspace=None):
        workspace = workspace or self.root
        with compiler_cache_session(
            self.binding,
            environment=environment or self.environment,
            workspace=workspace,
        ) as session:
            self.assertTrue(session.observation["available"])
            session.environment["LITAI_COMPILER_CACHE_TOOL"] = (
                self.binding.compiler_tool.executable
            )
            result = subprocess.run(
                command,
                cwd=workspace,
                env=session.environment,
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(result.returncode, 0, result.stderr[-4000:])
        return session.observation

    @unittest.skipUnless(shutil.which("clang++"), "clang++ is required")
    def test_native_cpp_drivers_reuse_objects_and_link_identical_executables(self):
        source = self.root / "source"
        source.mkdir()
        (source / "shared.cpp").write_text("int answer() { return 42; }\n")
        (source / "main.cpp").write_text(
            "int answer(); int main() { return answer() == 42 ? 0 : 1; }\n"
        )
        for multiple in (False, True):
            if multiple:
                (source / "second.cpp").write_text(
                    "int answer(); int main() { return answer() != 42; }\n"
                )
            hashes, observations = [], []
            for label in ("cold", "warm"):
                stage = self.root / f"{multiple}-{label}"
                objects, artifacts = stage / "objects", stage / "artifacts"
                artifacts.mkdir(parents=True)
                executable = artifacts / ("app.exe" if os.name == "nt" else "app")
                secondary = "second.exe" if os.name == "nt" else "second"
                common = [
                    sys.executable,
                    "-c",
                    standalone_driver_source() if multiple else _STANDARD_BUILD_DRIVER,
                    "cpp-executable",
                    json.dumps([shutil.which("clang++")]),
                    _encoded_toolchain_environment(()),
                    str(self.root),
                ]
                arguments = (
                    [
                        str(objects),
                        str(artifacts),
                        str(executable),
                        _encoded_multi_entrypoint_outputs(
                            [
                                {
                                    "source": "source/main.cpp",
                                    "export_id": executable.name,
                                },
                                {"source": "source/second.cpp", "export_id": secondary},
                            ]
                        ),
                    ]
                    if multiple
                    else ["source/main.cpp", str(objects), str(executable)]
                )
                observations.append(
                    self.run_compile(common + arguments, workspace=artifacts)
                )
                outputs = (
                    [executable, artifacts / secondary] if multiple else [executable]
                )
                hashes.append(
                    [
                        hashlib.sha256(output.read_bytes()).hexdigest()
                        for output in outputs
                    ]
                )
                for output in outputs:
                    self.assertEqual(
                        subprocess.run([str(output)], timeout=10).returncode, 0
                    )
                shutil.rmtree(stage)
            self.assertGreater(observations[1]["cache_hits"], 0, observations)
            self.assertEqual(observations[1]["cache_misses"], 0, observations)
            self.assertEqual(hashes[0], hashes[1])

    @unittest.skipUnless(shutil.which("clang"), "clang is required")
    def test_cold_and_warm_c_compilation_produce_identical_objects(self):
        dependencies = self.binding.compiler_dependencies.include_in(
            HostDependencyObservation(
                ({"bom-ref": "compiler", "type": "application"},),
                (("component", "compiler"),),
            )
        )
        refs = {item["bom-ref"] for item in dependencies.components}
        self.assertGreater(len(refs), 1)
        self.assertEqual(
            {source for source, _ in dependencies.edges if source not in refs},
            {"component"},
        )
        self.assertTrue(all(target in refs for _, target in dependencies.edges))
        source = self.root / "fixture.c"
        source.write_text("int answer(void) { return 42; }\n")
        output = self.root / "fixture.o"
        command = [
            self.binding.compiler_tool.executable,
            shutil.which("clang"),
            "-c",
            str(source),
            "-o",
            str(output),
        ]
        cold = self.run_compile(command)
        digest = hashlib.sha256(output.read_bytes()).hexdigest()
        output.unlink()
        warm = self.run_compile(command)
        self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(), digest)
        self.assertEqual(cold["cache_misses"], 1)
        self.assertEqual(warm["cache_hits"], 1)
        self.assertEqual(warm["compile_requests"], 1)

    @unittest.skipUnless(
        shutil.which("cargo") and shutil.which("rustc"), "Cargo is required"
    )
    def test_cargo_reuses_rust_library_with_a_fresh_target_directory(self):
        (self.root / "Cargo.toml").write_text(
            '[package]\nname="cache_proof"\nversion="0.1.0"\nedition="2021"\n[lib]\npath="lib.rs"\n'
        )
        (self.root / "lib.rs").write_text("pub fn answer() -> i32 { 42 }\n")
        hashes = []
        observations = []
        for _ in ("cold", "warm"):
            target = self.root / "target"
            observations.append(
                self.run_compile(
                    [shutil.which("cargo"), "build", "--offline", "--lib"],
                    self.environment
                    | {"CARGO_TARGET_DIR": str(target), "RUSTC": shutil.which("rustc")},
                )
            )
            hashes.append(
                hashlib.sha256(
                    (target / "debug/libcache_proof.rlib").read_bytes()
                ).hexdigest()
            )
            shutil.rmtree(target)
        self.assertEqual(hashes[0], hashes[1])
        self.assertGreater(observations[0]["cache_misses"], 0)
        self.assertGreater(observations[1]["cache_hits"], 0, observations)

    def test_exception_terminates_the_owned_server_and_removes_private_files(self):
        processes = []
        original_spawn = subprocess.Popen

        def spawn_owned(*args, **kwargs):
            process = original_spawn(*args, **kwargs)
            processes.append(process)
            return process

        with mock.patch.object(
            module.subprocess, "Popen", side_effect=spawn_owned
        ) as spawn:
            with self.assertRaisesRegex(RuntimeError, "build failed"):
                with compiler_cache_session(
                    self.binding, environment=self.environment, workspace=self.root
                ) as session:
                    self.assertTrue(session.observation["available"])
                    private_config = Path(session.environment["SCCACHE_CONF"])
                    raise RuntimeError("build failed")
        server_calls = [
            call
            for call in spawn.call_args_list
            if (call.kwargs.get("env") or {}).get("SCCACHE_NO_DAEMON") == "1"
        ]
        self.assertEqual(len(server_calls), 1)
        self.assertFalse(private_config.parent.exists())
        self.assertTrue(processes)
        self.assertTrue(all(process.poll() is not None for process in processes))

    @unittest.skipUnless(shutil.which("clang"), "clang is required")
    def test_read_only_compiler_cache_preserves_published_objects(self):
        source = self.root / "fixture.c"
        source.write_text("int answer(void) { return 42; }\n")
        command = [
            self.binding.compiler_tool.executable,
            shutil.which("clang"),
            "-c",
            str(source),
            "-o",
            str(self.root / "fixture.o"),
        ]
        self.run_compile(command)
        cache = (
            self.binding.local_root / self.binding.configuration.namespace / "compiler"
        )

        def inventory():
            return {
                str(path.relative_to(cache)): (
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                    path.stat().st_mtime_ns,
                )
                for path in cache.rglob("*")
                if path.is_file()
            }

        before = inventory()
        self.assertTrue(before)
        configuration = replace(
            self.binding.configuration,
            policies=tuple(
                replace(policy, mode=SharedCacheAccessMode.READ_ONLY)
                if policy.namespace is SharedCacheNamespace.COMPILER
                else policy
                for policy in self.binding.configuration.policies
            ),
        )
        (self.root / "shared-cache.json").write_text(
            json.dumps(configuration.to_dict())
        )
        self.binding = load_shared_cache(
            environment=self.environment
        ).bind_compiler_tool()
        warm = self.run_compile(command)
        self.assertEqual(warm["cache_hits"], 1)
        self.assertEqual(before, inventory())
