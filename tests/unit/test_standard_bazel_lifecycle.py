"""Bazel-native artifact production through the local Standard lifecycle."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.adapters.lifecycle import standard_local as standard_local_module
from literate_ai.adapters.lifecycle.standard_bazel import (
    StandardBazelLifecyclePorts,
    StandardBazelTarget,
)
from literate_ai.adapters.lifecycle.standard_local import (
    LocalComponentToolBinding,
    LocalSourceTreeRegistry,
    LocalStandardLifecycleError,
    local_generated_source_tree_identity,
)
from literate_ai.adapters.shared_cache_config import load_shared_cache
from literate_ai.contracts import (
    ComponentArtifactExportShape,
    ComponentCommandContract,
    ComponentCommandPhase,
    ComponentCommandToolBinding,
    ComponentLifecycleCommand,
    CppLibraryLayout,
    LibraryCapabilityImport,
    LibraryImportSurface,
    StandardBuildEvidence,
    StandardExecutionEvidence,
    StandardGeneratedTestExecutionEvidence,
    canonical_identity,
)
from tests.support.fixtures_test_component_node_generation_preparation import _fixture
from tests.support.fixtures_test_shared_cache import _configuration
from tests.unit.standard_source_evidence_fixture import register_strict_source


def _identity(label: str):
    return canonical_identity({"standard-bazel-test": label})


def _write_fake_bazel(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env python3
import os
from pathlib import Path
import sys

output_base = next(
    Path(arg.split("=", 1)[1])
    for arg in sys.argv[1:]
    if arg.startswith("--output_base=")
)
command = next(arg for arg in sys.argv[1:] if arg in {"build", "info", "mod", "query"})
if "mod" in sys.argv:
    command = "mod"
elif "query" in sys.argv:
    command = "query"
log = output_base.parent / "fake-bazel.log"
with log.open("a", encoding="utf-8") as stream:
    stream.write(" ".join(sys.argv[1:]) + "\\n")
bin_root = output_base / "execroot" / "workspace" / "bazel-out" / "bin"
if command == "mod":
    if "--lockfile_mode=update" in sys.argv:
        Path("MODULE.bazel.lock").write_text(
            '{"facts":{},"factsVersions":{},"lockFileVersion":28,'
            '"moduleExtensions":{},"registryFileHashes":{},'
            '"selectedYankedVersions":{}}\\n', encoding="utf-8"
        )
    if "graph" in sys.argv:
        print(
            '{"key":"<root>","name":"fixture","version":"1.0.0",'
            '"apparentName":"fixture","dependencies":[],'
            '"indirectDependencies":[],"cycles":[],"root":true}'
        )
    elif "show_repo" in sys.argv:
        pass
elif command == "query":
    if any(arg.startswith("buildfiles(") for arg in sys.argv):
        print("//:BUILD.bazel")
    else:
        print("//:input.txt")
elif command == "build" and "--nobuild" not in sys.argv:
    assert "--symlink_prefix=/" in sys.argv
    assert "--lockfile_mode=error" in sys.argv
    assert "--compilation_mode=opt" in sys.argv
    bin_root.mkdir(parents=True, exist_ok=True)
    if "//:cpp_library" in sys.argv:
        output = bin_root / "library"
        (output / "include/sample").mkdir(parents=True)
        (output / "lib").mkdir()
        (output / "tests").mkdir()
        (output / "include/sample/api.hpp").write_text(
            "#pragma once\\nnamespace sample { int add(int, int); }\\n",
            encoding="utf-8",
        )
        (output / "lib/libsample.a").write_bytes(b"compiled-library\\n")
        (output / "tests/run").write_bytes(b"generated-test-artifact\\n")
    else:
        assert "//:app" in sys.argv
        output = bin_root / "app"
        if os.environ.get("FAKE_BAZEL_LINK") == "1":
            escaped = output_base.parent / "escaped"
            escaped.write_text("escaped\\n", encoding="utf-8")
            output.symlink_to(escaped)
        else:
            output.write_bytes(b"compiled-by-bazel\\n")
            output.chmod(output.stat().st_mode | 0o111)
    Path("workspace-was-built").write_text("copy only\\n", encoding="utf-8")
elif command == "info":
    assert sys.argv[-2:] == ["info", "bazel-bin"]
    print(bin_root)
""",
        encoding="utf-8",
    )


def _commands() -> tuple[ComponentLifecycleCommand, ...]:
    return (
        ComponentLifecycleCommand(
            ComponentCommandPhase.BUILD,
            (
                "{tool}",
                "generic-build-must-not-run",
                "{source_root}",
                "{object_root}",
                "{export_path}",
            ),
        ),
        ComponentLifecycleCommand(
            ComponentCommandPhase.TEST,
            (
                "{tool}",
                "-c",
                "from pathlib import Path; import json,sys; "
                "assert (Path(sys.argv[1])/'app').read_bytes() == "
                "b'compiled-by-bazel\\n'; "
                "print(json.dumps(dict(schema="
                "'literate-ai/generated-test-results@1',cases=["
                "dict(case_id='fixture-example',outcome='passed'),"
                "dict(case_id='fixture-boundary',outcome='passed'),"
                "dict(case_id='fixture-invariant',outcome='passed')]),"
                "sort_keys=True,separators=(',',':')))",
                "{artifact_root}",
            ),
        ),
        ComponentLifecycleCommand(
            ComponentCommandPhase.EXECUTE,
            (
                "{tool}",
                "-c",
                "from pathlib import Path; import sys; "
                "print((Path(sys.argv[1])/'app').read_text().strip())",
                "{artifact_root}",
            ),
        ),
    )


class StandardBazelLifecycleTests(unittest.TestCase):
    def _system(
        self,
        root: Path,
        *,
        cpp_library: bool = False,
        bazel_cache_arguments: tuple[str, ...] = (),
        shared_cache=None,
    ):
        snapshot, execution = _fixture()
        generation_plan = execution.generation_plans[0]
        fake_bazel = root / "fake-bazel.py"
        _write_fake_bazel(fake_bazel)
        bazel = LocalComponentToolBinding(sys.executable, (str(fake_bazel),))
        python = LocalComponentToolBinding(sys.executable)
        layout = (
            CppLibraryLayout(
                "static",
                ("include/sample/api.hpp",),
                ("lib/libsample.a",),
            )
            if cpp_library
            else None
        )
        target = StandardBazelTarget(
            generation_plan.component_revision,
            _identity("bzlmod-resolver"),
            bazel.toolchain_identity,
            "//:cpp_library" if cpp_library else "//:app",
            "library" if cpp_library else "app",
            ("--compilation_mode=opt",),
            cpp_layout=layout,
            cpp_test_output="tests/run" if cpp_library else None,
        )
        surface = (
            LibraryImportSurface(
                "cpp",
                "sample",
                (
                    LibraryCapabilityImport(
                        "fixture-example",
                        _identity("cpp-interface"),
                        "sample/api.hpp",
                        ("sample::add",),
                    ),
                ),
            )
            if cpp_library
            else None
        )
        contract = ComponentCommandContract(
            component_revision=generation_plan.component_revision,
            locked_build_authority_identity=target.identity,
            build_system_resolver_identity=target.build_system_resolver_identity,
            build_system_toolchain_identity=target.build_system_toolchain_identity,
            language_compiler_identity=(
                python.toolchain_identity
                if cpp_library
                else _identity("language-compiler")
            ),
            language_runtime_identity=python.toolchain_identity,
            commands=_commands(),
            tool_bindings=(
                ComponentCommandToolBinding(
                    ComponentCommandPhase.BUILD, bazel.toolchain_identity
                ),
                ComponentCommandToolBinding(
                    ComponentCommandPhase.TEST, python.toolchain_identity
                ),
                ComponentCommandToolBinding(
                    ComponentCommandPhase.EXECUTE, python.toolchain_identity
                ),
            ),
            artifact_export=ComponentArtifactExportShape(
                "app",
                "library" if cpp_library else "executable",
                _identity("abi"),
                _identity("target-platform"),
                "application/octet-stream",
                _identity("producer"),
            ),
            library_import_surface=surface,
            native_layout=layout,
        )
        source = root / "generated"
        workspace = source / "source"
        workspace.mkdir(parents=True)
        (workspace / "MODULE.bazel").write_text(
            'module(name = "fixture", version = "1.0.0")\n', encoding="utf-8"
        )
        (workspace / "BUILD.bazel").write_text(
            'exports_files(["input.txt"])\n', encoding="utf-8"
        )
        (workspace / "input.txt").write_text("source-authority\n", encoding="utf-8")
        registry = LocalSourceTreeRegistry()
        candidate = register_strict_source(
            registry,
            source,
            snapshot=snapshot,
            generation_plan=generation_plan,
            identity_namespace="standard-bazel-test",
        )
        ports = StandardBazelLifecyclePorts(
            source_trees=registry,
            object_root=root / "objects",
            contracts=(contract,),
            tool_bindings=(bazel, python),
            bazel_targets=(target,),
            bazel_cache_arguments=bazel_cache_arguments,
            shared_cache=shared_cache,
        )
        intent = ports.create(execution, generation_plan, candidate, (), ())
        index = ports.index(candidate.component_revision, candidate.tree_identity)
        plan = ports.finalize(intent, ports.authorize(intent, index))
        return ports, plan, candidate, target, contract

    def test_bazel_produces_exact_export_and_cache_avoids_second_build(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ports, plan, candidate, target, contract = self._system(root)

            with (
                mock.patch.object(ports, "_run", wraps=ports._run) as run,
                mock.patch.object(
                    standard_local_module,
                    "run_with_tree_kill",
                    wraps=standard_local_module.run_with_tree_kill,
                ) as process,
            ):
                first = ports.build(plan, ())
                test_identity = ports.test(plan, first.exports)
                execution_identity = ports.execute(plan, first.exports)
                acceptance = ports.accept(
                    plan, test_identity.identity, execution_identity.identity
                )
                second = ports.build(plan, ())

            self.assertEqual(first, second)
            self.assertIsInstance(first.evidence, StandardBuildEvidence)
            self.assertIsInstance(test_identity, StandardGeneratedTestExecutionEvidence)
            self.assertIsInstance(execution_identity, StandardExecutionEvidence)
            self.assertEqual(acceptance.build.identity, first.evidence.identity)
            self.assertEqual(
                ports.read_artifact_blob(first.exports[0].blob),
                b"compiled-by-bazel\n",
            )
            self.assertEqual(ports.build_cache_misses, 1)
            self.assertEqual(ports.build_cache_hits, 1)
            self.assertNotEqual(test_identity.identity, execution_identity.identity)
            self.assertEqual(
                ports.execution_stdout[candidate.component_revision.uri],
                "compiled-by-bazel",
            )
            self.assertEqual(
                local_generated_source_tree_identity(root / "generated"),
                candidate.tree_identity,
            )
            self.assertFalse(
                (root / "generated" / "source" / "workspace-was-built").exists()
            )
            # Inspect the real process boundary, including generated tests/execution:
            # build tools need the Bazel budget; other lifecycle commands stay short.
            for call in process.call_args_list:
                is_bazel = str(root / "fake-bazel.py") in call.args[0]
                self.assertEqual(call.kwargs["timeout"], 1800.0 if is_bazel else 60.0)
            commands = [item.args[0] for item in run.call_args_list]
            bazel_commands = [
                command
                for command in commands
                if str(root / "fake-bazel.py") in command
            ]
            phases = [
                next(
                    item
                    for item in command
                    if item in {"mod", "query", "build", "info"}
                )
                for command in bazel_commands
            ]
            self.assertEqual(
                phases,
                [
                    "mod",
                    "mod",
                    "mod",
                    "mod",
                    "build",
                    "query",
                    "query",
                    "build",
                    "info",
                ],
            )
            actual_build = next(
                command
                for command in bazel_commands
                if "build" in command and "--nobuild" not in command
            )
            self.assertIn("--lockfile_mode=error", actual_build)
            self.assertIn("--symlink_prefix=/", actual_build)
            self.assertIn("--compilation_mode=opt", actual_build)
            self.assertEqual(actual_build[-1], "//:app")
            self.assertEqual(contract.locked_build_authority_identity, target.identity)

    def test_private_cache_configuration_reaches_build_and_cleans_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            config = root / "shared-cache.json"
            config.write_text(json.dumps(_configuration().to_dict()))
            binding = load_shared_cache(
                environment={
                    "LITAI_CONFIG_DIR": str(root),
                    "LITAI_CACHE_DIR": str(root / "cache"),
                    "CACHE_TOKEN": "private-token",
                }
            )
            ports, plan, *_ = self._system(root, shared_cache=binding)
            credential_files = []
            observed_logs = []
            original_run = ports._run

            def run(command, **kwargs):
                private_rc = [
                    Path(arg.split("=", 1)[1])
                    for arg in command
                    if arg.startswith("--bazelrc=") and "cache-auth-" in arg
                ]
                self.assertEqual(len(private_rc), 1)
                self.assertIn("private-token", private_rc[0].read_text())
                self.assertNotIn("private-token", str(command))
                credential_files.extend(private_rc)
                result = original_run(command, **kwargs)
                output_base = next(
                    Path(arg.split("=", 1)[1])
                    for arg in command
                    if arg.startswith("--output_base=")
                )
                observed_logs.append(
                    (output_base.parent / "fake-bazel.log").read_text()
                )
                return result

            with mock.patch.object(ports, "_run", side_effect=run) as invoked:
                built = ports.build(plan, ())
            self.assertTrue(built.exports)
            self.assertTrue(credential_files)
            self.assertTrue(all(not path.exists() for path in credential_files))
            actual_build = next(
                call.args[0]
                for call in invoked.call_args_list
                if "build" in call.args[0] and "--nobuild" not in call.args[0]
            )
            for argument in binding.bazel_arguments():
                if argument.startswith("--disk_cache="):
                    continue
                self.assertIn(argument, actual_build)
            disk_cache = next(
                Path(argument.split("=", 1)[1])
                for argument in actual_build
                if argument.startswith("--disk_cache=")
            )
            self.assertEqual(disk_cache.name, "cache-view")
            self.assertFalse(disk_cache.exists())
            self.assertTrue(observed_logs)
            self.assertTrue(all("private-token" not in log for log in observed_logs))

    def test_link_output_is_rejected_and_never_enters_artifact_custody(self) -> None:
        if os.name == "nt":
            self.skipTest("vanilla Windows does not grant symlink creation")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ports, plan, _candidate, _target, _contract = self._system(root)
            with mock.patch.dict(os.environ, {"FAKE_BAZEL_LINK": "1"}):
                with self.assertRaisesRegex(
                    LocalStandardLifecycleError, "cannot be a link"
                ):
                    ports.build(plan, ())
            self.assertEqual(ports.build_cache_misses, 0)
            self.assertFalse(any(ports._artifact_blob_paths.values()))


if __name__ == "__main__":
    unittest.main()
