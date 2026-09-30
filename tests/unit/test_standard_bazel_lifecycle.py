"""Bazel-native artifact production through the local Standard lifecycle."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.adapters.dependencies import DependencyObservationError
from literate_ai.adapters.lifecycle import standard_bazel as standard_bazel_module
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
from tests.unit.standard_source_evidence_fixture import register_strict_source
from tests.unit.test_component_node_generation_preparation import _fixture
from tests.unit.test_standard_local_command_adapter import (
    copy_digest_cache_without_sidecars,
    rewrite_self_authenticating_artifact,
)


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
    def test_target_contract_rejects_ambiguous_paths_and_custody_options(self) -> None:
        values = dict(
            component_revision=_identity("component"),
            build_system_resolver_identity=_identity("resolver"),
            build_system_toolchain_identity=_identity("toolchain"),
            target_label="//:app",
            output_path="app",
        )
        for label in ("@remote//:app", "//", "//pkg/", "//pkg//nested:app"):
            with self.subTest(label=label):
                with self.assertRaisesRegex(ValueError, "canonical local label"):
                    StandardBazelTarget(**(values | {"target_label": label}))
        for output in ("/app", "../app", "nested//app"):
            with self.subTest(output=output):
                with self.assertRaisesRegex(ValueError, "canonical relative path"):
                    StandardBazelTarget(**(values | {"output_path": output}))
        with self.assertRaisesRegex(ValueError, "framework-owned"):
            StandardBazelTarget(**values, build_options=("--output_base=/untrusted",))

    def _system(
        self,
        root: Path,
        *,
        cpp_library: bool = False,
        bazel_cache_arguments: tuple[str, ...] = (),
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
        )
        intent = ports.create(execution, generation_plan, candidate, (), ())
        index = ports.index(candidate.component_revision, candidate.tree_identity)
        plan = ports.finalize(intent, ports.authorize(intent, index))
        return ports, plan, candidate, target, contract

    def test_cpp_library_build_separates_consumer_and_generated_test_outputs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ports, plan, _candidate, target, contract = self._system(
                root, cpp_library=True
            )

            built = ports.build(plan, ())

            export = ports.artifact_path(built.exports[0])
            artifact_root = export.parent
            self.assertEqual(contract.native_layout, target.cpp_layout)
            self.assertEqual(
                {
                    path.relative_to(export).as_posix()
                    for path in export.rglob("*")
                    if path.is_file()
                },
                set(target.cpp_layout.files),
            )
            self.assertEqual(
                (artifact_root / "generated-tests/run").read_bytes(),
                b"generated-test-artifact\n",
            )
            self.assertFalse((export / "tests").exists())

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

    def test_framework_cache_arguments_reach_real_bazel_build_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ports, plan, _candidate, _target, _contract = self._system(
                root,
                bazel_cache_arguments=(
                    "--disk_cache=/private/cache",
                    "--remote_upload_local_results=false",
                ),
            )
            with mock.patch.object(ports, "_run", wraps=ports._run) as invoked:
                ports.build(plan, ())
            commands = [call.args[0] for call in invoked.call_args_list]
            actual_build = next(
                command
                for command in commands
                if "build" in command and "--nobuild" not in command
            )
            self.assertIn("--disk_cache=/private/cache", actual_build)
            self.assertIn("--remote_upload_local_results=false", actual_build)
            info = next(command for command in commands if "info" in command)
            self.assertNotIn("--disk_cache=/private/cache", info)

    def test_bazel_cache_arguments_cannot_change_output_custody(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "output custody"):
                self._system(
                    Path(temporary),
                    bazel_cache_arguments=("--output_base=/private/cache",),
                )

    def test_bazel_analysis_timeout_publishes_no_artifact_or_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ports, plan, candidate, _target, _contract = self._system(root)
            process = standard_local_module.run_with_tree_kill

            def expire_analysis(command, **kwargs):
                if "--nobuild" in command:
                    raise subprocess.TimeoutExpired(command, kwargs["timeout"])
                return process(command, **kwargs)

            with mock.patch.object(
                standard_local_module, "run_with_tree_kill", side_effect=expire_analysis
            ):
                with self.assertRaises(subprocess.TimeoutExpired) as raised:
                    ports.build(plan, ())
            self.assertEqual(raised.exception.timeout, 1800.0)
            self.assertEqual(ports.build_cache_misses, 0)
            self.assertFalse(ports._artifact_blob_paths)
            self.assertFalse(ports._bazel_resolution_builds)
            self.assertFalse(tuple((root / "objects").iterdir()))
            self.assertEqual(
                local_generated_source_tree_identity(root / "generated"),
                candidate.tree_identity,
            )

    def test_nonfinite_or_nonpositive_command_deadline_refuses_before_launch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ports, _plan, _candidate, _target, _contract = self._system(root)
            with mock.patch.object(standard_local_module, "run_with_tree_kill") as run:
                for deadline in (0.0, -1.0, float("inf"), float("nan")):
                    with self.subTest(deadline=deadline), self.assertRaises(ValueError):
                        ports._run(
                            (sys.executable, "-c", "pass"),
                            cwd=root,
                            providers=(),
                            timeout_seconds=deadline,
                        )
                run.assert_not_called()

    def test_cache_rejects_coordinated_self_manifest_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ports, plan, _candidate, _target, _contract = self._system(Path(temporary))
            first = ports.build(plan, ())
            rewrite_self_authenticating_artifact(
                ports.artifact_path(first.exports[0]).parent,
                "app",
                b"tampered-by-bazel\n",
            )

            with self.assertRaisesRegex(
                LocalStandardLifecycleError, "changed after publication"
            ):
                ports.build(plan, ())

    def test_restarted_runtime_hits_durable_external_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            first, plan, _candidate, _target, _contract = self._system(Path(temporary))
            expected = first.build(plan, ())
            second = StandardBazelLifecyclePorts(
                source_trees=first.source_trees,
                object_root=first.object_root,
                contracts=tuple(first.contracts.values()),
                tool_bindings=tuple(first.tool_bindings.values()),
                bazel_targets=tuple(first.bazel_targets.values()),
                dependency_observation=first.dependency_observation,
            )
            with mock.patch.object(second, "_run") as run:
                cached = second.build(plan, ())

            self.assertEqual(cached, expected)
            run.assert_not_called()
            self.assertEqual(second.build_cache_misses, 0)
            self.assertEqual(second.build_cache_hits, 1)

    def test_artifact_bytes_without_checkpoint_are_rebuilt_not_trusted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, plan, _candidate, _target, _contract = self._system(root)
            first.build(plan, ())
            isolated = root / "isolated-objects"
            copy_digest_cache_without_sidecars(first.object_root, isolated)
            second = StandardBazelLifecyclePorts(
                source_trees=first.source_trees,
                object_root=isolated,
                contracts=tuple(first.contracts.values()),
                tool_bindings=tuple(first.tool_bindings.values()),
                bazel_targets=tuple(first.bazel_targets.values()),
                dependency_observation=first.dependency_observation,
            )
            replayed = second.build(plan, ())
            cached = second.build(plan, ())

            self.assertEqual(cached, replayed)
            self.assertEqual(second.build_cache_misses, 1)
            self.assertEqual(second.build_cache_hits, 1)

    def test_bazel_output_admission_does_not_open_leaf_for_canonicalization(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ports, plan, _candidate, _target, _contract = self._system(root)
            actual_resolve = Path.resolve

            def deny_fresh_output(path: Path, *args, **kwargs):
                if path.name == "app" and "bazel-out" in path.parts:
                    raise PermissionError("simulated Windows executable sharing policy")
                return actual_resolve(path, *args, **kwargs)

            with mock.patch.object(Path, "resolve", deny_fresh_output):
                built = ports.build(plan, ())

            self.assertEqual(
                ports.read_artifact_blob(built.exports[0].blob),
                b"compiled-by-bazel\n",
            )

    def test_windows_fresh_bazel_output_copy_has_a_bounded_sharing_retry(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "run.pyz"
            destination = root / "admitted.pyz"
            source.write_bytes(b"fresh-bazel-output")
            actual_copy = standard_bazel_module.shutil.copy2
            attempts = 0

            def transient_copy(source_path: Path, destination_path: Path):
                nonlocal attempts
                attempts += 1
                if attempts < 3:
                    error = PermissionError("fresh output is temporarily locked")
                    error.winerror = 5
                    raise error
                return actual_copy(source_path, destination_path)

            with (
                mock.patch.object(standard_bazel_module.os, "name", "nt"),
                mock.patch.object(
                    standard_bazel_module.shutil,
                    "copy2",
                    side_effect=transient_copy,
                ),
                mock.patch.object(standard_bazel_module.time, "sleep") as sleep,
            ):
                standard_bazel_module._copy_regular_tree(source, destination)

            self.assertEqual(destination.read_bytes(), b"fresh-bazel-output")
            self.assertEqual(attempts, 3)
            self.assertEqual(
                [call.args[0] for call in sleep.call_args_list], [0.05, 0.1]
            )

    def test_windows_fresh_output_copy_does_not_suppress_a_persistent_lock(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "run.pyz"
            source.write_bytes(b"fresh-bazel-output")
            error = PermissionError("output remains locked")
            error.winerror = 32
            with (
                mock.patch.object(standard_bazel_module.os, "name", "nt"),
                mock.patch.object(
                    standard_bazel_module.shutil,
                    "copy2",
                    side_effect=error,
                ) as copy,
                mock.patch.object(standard_bazel_module.time, "sleep") as sleep,
                self.assertRaises(PermissionError),
            ):
                standard_bazel_module._copy_regular_tree(source, root / "admitted.pyz")

            self.assertEqual(
                copy.call_count,
                len(standard_bazel_module._WINDOWS_FRESH_OUTPUT_COPY_DELAYS) + 1,
            )
            self.assertEqual(
                sleep.call_count,
                len(standard_bazel_module._WINDOWS_FRESH_OUTPUT_COPY_DELAYS),
            )

    def test_generated_test_evidence_rejects_missing_case_attribution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ports, plan, _candidate, _target, _contract = self._system(Path(temporary))
            built = ports.build(plan, ())
            incomplete = subprocess.CompletedProcess(
                args=(sys.executable,),
                returncode=0,
                stdout=json.dumps(
                    {
                        "schema": "literate-ai/generated-test-results@1",
                        "cases": [{"case_id": "fixture-example", "outcome": "passed"}],
                    }
                ),
                stderr="",
            )
            with mock.patch.object(ports, "_run_locked", return_value=incomplete):
                with self.assertRaisesRegex(
                    LocalStandardLifecycleError,
                    "every and only selected case",
                ):
                    ports.test(plan, built.exports)

    def test_bazel_dependency_evidence_tamper_blocks_resolved_sbom(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ports, plan, _candidate, _target, _contract = self._system(root)
            write_evidence = ports._write_bazel_dependency_evidence

            def tamper(plan, artifact_root, **arguments):
                result = write_evidence(plan, artifact_root, **arguments)
                (artifact_root / ".literate/bazel/module-graph.json").write_text(
                    "{}\n", encoding="utf-8"
                )
                return result

            with (
                mock.patch.object(
                    ports, "_write_bazel_dependency_evidence", side_effect=tamper
                ),
                self.assertRaisesRegex(
                    DependencyObservationError,
                    "does not cover its exact evidence files",
                ),
            ):
                ports.build(plan, ())

    def test_framework_evidence_never_overwrites_producer_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ports, plan, _candidate, _target, _contract = self._system(root)
            for relative in (
                "artifact-manifest.json",
                ".literate/resolved-sbom.cdx.json",
            ):
                with self.subTest(relative=relative):
                    artifact = root / ("reserved-" + relative.replace("/", "-"))
                    artifact.mkdir()
                    occupied = artifact.joinpath(*Path(relative).parts)
                    occupied.parent.mkdir(parents=True, exist_ok=True)
                    occupied.write_text("producer-owned", encoding="utf-8")
                    with self.assertRaisesRegex(
                        LocalStandardLifecycleError, "reserved"
                    ):
                        ports._write_artifact_manifest(
                            plan,
                            artifact,
                            (),
                            canonical_identity({"process": relative}),
                        )
                    self.assertEqual(
                        occupied.read_text(encoding="utf-8"), "producer-owned"
                    )
            if os.name != "nt":
                artifact = root / "reserved-link"
                outside = root / "outside"
                artifact.mkdir()
                outside.mkdir()
                (artifact / ".literate").symlink_to(outside, target_is_directory=True)
                with self.assertRaisesRegex(LocalStandardLifecycleError, "reserved"):
                    ports._write_artifact_manifest(
                        plan,
                        artifact,
                        (),
                        canonical_identity({"process": "linked-evidence-directory"}),
                    )
                self.assertFalse((outside / "resolved-sbom.cdx.json").exists())

    def test_target_authority_mismatch_fails_before_a_process_can_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ports, _plan, _candidate, target, contract = self._system(root)
            mismatched = StandardBazelTarget(
                target.component_revision,
                target.build_system_resolver_identity,
                target.build_system_toolchain_identity,
                "//:different",
                target.output_path,
                target.build_options,
            )
            with self.assertRaisesRegex(ValueError, "locked build authority"):
                StandardBazelLifecyclePorts(
                    source_trees=ports.source_trees,
                    object_root=root / "other-objects",
                    contracts=(contract,),
                    tool_bindings=tuple(ports.tool_bindings.values()),
                    bazel_targets=(mismatched,),
                )

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
