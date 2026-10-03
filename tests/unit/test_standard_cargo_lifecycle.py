"""Cargo-native artifact production through the local Standard lifecycle."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest import mock

from literate_ai.adapters.compiler_cache import CompilerCacheSession
from literate_ai.adapters.lifecycle.standard_cargo import (
    StandardCargoLifecyclePorts,
    StandardCargoTarget,
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
    LibraryCapabilityImport,
    LibraryImportSurface,
    canonical_identity,
)
from tests.unit.standard_source_evidence_fixture import register_strict_source
from tests.unit.test_component_node_generation_preparation import _fixture
from tests.unit.test_shared_cache import _configuration
from tests.unit.test_standard_local_command_adapter import (
    copy_digest_cache_without_sidecars,
    rewrite_self_authenticating_artifact,
)


def _identity(label: str):
    return canonical_identity({"standard-cargo-test": label})


def _commands() -> tuple[ComponentLifecycleCommand, ...]:
    return tuple(
        ComponentLifecycleCommand(
            phase,
            (
                "{tool}",
                "phase",
                "{source_root}" if phase is ComponentCommandPhase.BUILD else "fixed",
                (
                    "{object_root}"
                    if phase is ComponentCommandPhase.BUILD
                    else "{artifact_root}"
                ),
                "{export_path}" if phase is ComponentCommandPhase.BUILD else "fixed2",
            ),
        )
        for phase in ComponentCommandPhase
    )


def _write_fake_cargo(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

command = sys.argv[1]
manifest = Path(sys.argv[sys.argv.index('--manifest-path') + 1])
log = manifest.parent.parent / 'cargo.log'
with log.open('a', encoding='utf-8') as stream:
    stream.write(command + '\\n')
target = Path(os.environ['CARGO_TARGET_DIR'])
if command == 'generate-lockfile':
    manifest.with_name('Cargo.lock').write_text(
        'version = 3\\n\\n'
        '[[package]]\\nname = "fixture"\\nversion = "1.0.0"\\n',
        encoding='utf-8')
elif command == 'metadata':
    assert '--locked' in sys.argv
    print(json.dumps({'packages': [{
        'name': 'fixture',
        'version': '1.0.0',
        'id': 'path+file:///workspace/fixture#1.0.0',
    }]}))
elif command == 'build':
    assert '--locked' in sys.argv
    assert Path(os.environ['RUSTC']).resolve() == Path(sys.executable).resolve()
    target.joinpath('debug').mkdir(parents=True, exist_ok=True)
    name = 'litai_artifact.exe' if os.name == 'nt' else 'litai_artifact'
    output = target / 'debug' / name
    output.write_bytes(b'compiled-by-cargo\\n')
else:
    raise SystemExit(2)
""",
        encoding="utf-8",
    )


class StandardCargoLifecycleTests(unittest.TestCase):
    def _system(
        self,
        root: Path,
        *,
        model_lock: bool = False,
        library: bool = False,
        recorder=None,
        shared_cache=None,
    ):
        snapshot, execution = _fixture()
        generation_plan = execution.generation_plans[0]
        fake_cargo = root / "fake-cargo.py"
        _write_fake_cargo(fake_cargo)
        cargo = LocalComponentToolBinding(sys.executable, (str(fake_cargo),))
        python = LocalComponentToolBinding(sys.executable)
        resolver = _identity("cargo-resolver")
        provisional = StandardCargoTarget(
            generation_plan.component_revision,
            resolver,
            cargo.toolchain_identity,
            python.toolchain_identity,
            "source/Cargo.toml",
            "litai_artifact",
            (sys.executable,),
            library=library,
        )
        contract = ComponentCommandContract(
            component_revision=generation_plan.component_revision,
            locked_build_authority_identity=provisional.identity,
            build_system_resolver_identity=resolver,
            build_system_toolchain_identity=cargo.toolchain_identity,
            language_compiler_identity=python.toolchain_identity,
            language_runtime_identity=python.toolchain_identity,
            commands=_commands(),
            tool_bindings=tuple(
                ComponentCommandToolBinding(
                    phase,
                    cargo.toolchain_identity
                    if phase is ComponentCommandPhase.BUILD
                    else python.toolchain_identity,
                )
                for phase in ComponentCommandPhase
            ),
            artifact_export=ComponentArtifactExportShape(
                "app",
                "library" if library else "executable",
                _identity("abi"),
                _identity("platform"),
                "application/octet-stream",
                _identity("producer"),
            ),
            library_import_surface=(
                LibraryImportSurface(
                    "rust",
                    "fixture",
                    (
                        LibraryCapabilityImport(
                            "sample.portable-app",
                            _identity("library-interface"),
                            "fixture::portable_app",
                            ("portable_app",),
                        ),
                    ),
                )
                if library
                else None
            ),
        )
        source = root / "generated"
        package = source / "source"
        package.joinpath("src").mkdir(parents=True)
        package.joinpath("Cargo.toml").write_text(
            '[package]\nname = "fixture"\nversion = "1.0.0"\nedition = "2021"\n'
            '[[bin]]\nname = "litai_artifact"\npath = "src/main.rs"\n',
            encoding="utf-8",
        )
        package.joinpath("src/main.rs").write_text("fn main() {}\n", encoding="utf-8")
        if library:
            package.joinpath("src/lib.rs").write_text(
                "pub mod portable_app { pub const portable_app: u8 = 7; }\n",
                encoding="utf-8",
            )
        if model_lock:
            package.joinpath("Cargo.lock").write_text("model lock\n", encoding="utf-8")
        registry = LocalSourceTreeRegistry()
        candidate = register_strict_source(
            registry,
            source,
            snapshot=snapshot,
            generation_plan=generation_plan,
            identity_namespace="standard-cargo-test",
        )
        ports = StandardCargoLifecyclePorts(
            source_trees=registry,
            object_root=root / "objects",
            contracts=(contract,),
            tool_bindings=(cargo, python),
            cargo_targets=(provisional,),
            shared_cache=shared_cache,
        )
        if recorder is not None:
            ports.retain_evidence_with(recorder)
        intent = ports.create(execution, generation_plan, candidate, (), ())
        index = ports.index(candidate.component_revision, candidate.tree_identity)
        plan = ports.finalize(intent, ports.authorize(intent, index))
        return ports, plan, candidate

    def test_compiler_cache_environment_and_observation_survive_capture(self):
        from literate_ai.adapters.lifecycle import standard_cargo as module
        from literate_ai.adapters.qualification_capture import (
            QualificationEvidenceReader,
            QualificationEvidenceRecorder,
            verify_qualification_build,
        )
        from literate_ai.contracts.identity import ContentIdentity

        recorder = QualificationEvidenceRecorder(max_bytes=5_000_000, max_records=1000)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "shared-cache.json").write_text(
                json.dumps(
                    _configuration(endpoint=None, credential_reference=None).to_dict()
                )
            )
            binding = load_shared_cache(
                environment={
                    "LITAI_CONFIG_DIR": str(root),
                    "LITAI_CACHE_DIR": str(root / "cache"),
                }
            )
            binding = replace(
                binding, compiler_tool=LocalComponentToolBinding(sys.executable)
            )
            observation = {
                "schema": "literate-ai/compiler-cache-observation@1",
                "configuration_identity": binding.identity.uri,
                "tool_identity": binding.compiler_tool.toolchain_identity.uri,
                "available": True,
                "cache_hits": 1,
                "cache_misses": 0,
                "compile_requests": 1,
            }

            @contextmanager
            def cache_session(_binding, *, environment, workspace):
                self.assertEqual(_binding.identity, binding.identity)
                self.assertIn("cargo-work", str(workspace))
                yield CompilerCacheSession(
                    dict(environment) | {"RUSTC_WRAPPER": sys.executable}, observation
                )

            ports, plan, _ = self._system(root, recorder=recorder, shared_cache=binding)
            with (
                mock.patch.object(
                    module, "compiler_cache_session", side_effect=cache_session
                ),
                mock.patch.object(
                    module, "run_bounded_process", wraps=module.run_bounded_process
                ) as invoked,
            ):
                built = ports.build(plan, ())
            build_call = next(
                call for call in invoked.call_args_list if "build" in call.args[0]
            )
            self.assertEqual(
                build_call.kwargs["environment"]["RUSTC_WRAPPER"], sys.executable
            )
            self.assertEqual(list((ports.object_root / "cargo-work").iterdir()), [])
        reader = QualificationEvidenceReader(
            recorder.entries, max_bytes=5_000_000, max_records=1000
        )
        verify_qualification_build(reader, plan=plan, build=built.evidence)
        outer = reader.read_json(built.evidence.build_observation_identity)
        captured = reader.read_json(
            ContentIdentity.parse_uri(outer["process_observation_identity"])
        )
        self.assertEqual(captured["compiler_cache"], observation)

    def test_captured_native_build_reopens_after_all_workspaces_are_removed(self):
        from literate_ai.adapters.qualification_capture import (
            QualificationCaptureError,
            QualificationEvidenceReader,
            QualificationEvidenceRecorder,
            verify_qualification_build,
        )
        from literate_ai.contracts import ContentIdentity

        for library in (False, True):
            with self.subTest(library=library):
                recorder = QualificationEvidenceRecorder(
                    max_bytes=5_000_000, max_records=1000
                )
                with tempfile.TemporaryDirectory() as temporary:
                    ports, plan, _ = self._system(
                        Path(temporary), library=library, recorder=recorder
                    )
                    built = ports.build(plan, ())
                self.assertFalse(Path(temporary).exists())
                entries = recorder.entries
                reader = QualificationEvidenceReader(
                    entries, max_bytes=5_000_000, max_records=1000
                )
                verify_qualification_build(reader, plan=plan, build=built.evidence)
                outer = reader.read_json(built.evidence.build_observation_identity)
                process_id = ContentIdentity.parse_uri(
                    outer["process_observation_identity"]
                )
                observation = reader.read_json(process_id)
                phase_ids = tuple(
                    ContentIdentity.parse_uri(observation[name])
                    for name in ("generate_lockfile", "metadata", "build")
                )
                files = reader.read_json(
                    ContentIdentity.parse_uri(outer["artifact_tree_identity"])
                )["files"]
                for missing in (
                    process_id,
                    ContentIdentity.parse_uri(observation["target_identity"]),
                    *phase_ids,
                    *(
                        ContentIdentity.parse_uri(reader.read_json(phase)[field])
                        for phase in phase_ids
                        for field in ("stdout_identity", "stderr_identity")
                    ),
                    *(
                        ContentIdentity.parse_uri("sha256:" + item["sha256"])
                        for item in files
                        if item["path"].startswith(".literate/cargo/")
                    ),
                ):
                    incomplete = QualificationEvidenceReader(
                        tuple(e for e in entries if e[0] != missing),
                        max_bytes=5_000_000,
                        max_records=1000,
                    )
                    with self.assertRaisesRegex(
                        QualificationCaptureError, "record-missing"
                    ):
                        verify_qualification_build(
                            incomplete, plan=plan, build=built.evidence
                        )

                from literate_ai.adapters.qualification_cargo import (
                    verify_qualification_cargo_build,
                )

                for change in (
                    {"returncode": 1},
                    {"returncode": False},
                    {"phase": "cargo-test"},
                    {"plan_identity": _identity("foreign").uri},
                    {"extra": True},
                ):
                    altered = QualificationEvidenceRecorder(
                        max_bytes=5_000_000, max_records=1000
                    )
                    for _, payload in entries:
                        altered.remember_bytes(payload)
                    replacement = altered.remember_json(
                        {**reader.read_json(phase_ids[2]), **change}
                    )
                    with (
                        self.subTest(change=change),
                        self.assertRaisesRegex(
                            QualificationCaptureError, "cargo-build-mismatch"
                        ),
                    ):
                        verify_qualification_cargo_build(
                            QualificationEvidenceReader(
                                altered.entries, max_bytes=5_000_000, max_records=1000
                            ),
                            plan=plan,
                            observation={**observation, "build": replacement.uri},
                            files=files,
                        )
                target_id = ContentIdentity.parse_uri(observation["target_identity"])
                altered = QualificationEvidenceRecorder(
                    max_bytes=5_000_000, max_records=1000
                )
                for _, payload in entries:
                    altered.remember_bytes(payload)
                foreign_target = altered.remember_json(
                    {
                        **reader.read_json(target_id),
                        "language_compiler_identity": _identity("foreign-compiler").uri,
                    }
                )
                with self.assertRaisesRegex(
                    QualificationCaptureError, "cargo-build-mismatch"
                ):
                    verify_qualification_cargo_build(
                        QualificationEvidenceReader(
                            altered.entries, max_bytes=5_000_000, max_records=1000
                        ),
                        plan=plan,
                        observation={
                            **observation,
                            "target_identity": foreign_target.uri,
                        },
                        files=files,
                    )
                with self.assertRaisesRegex(
                    QualificationCaptureError, "cargo-build-mismatch"
                ):
                    verify_qualification_cargo_build(
                        reader,
                        plan=plan,
                        observation=observation,
                        files=[
                            *files,
                            {
                                "path": ".literate/cargo/unbound.json",
                                "sha256": _identity("unbound").digest,
                            },
                        ],
                    )

    def test_cargo_library_build_exports_immutable_package_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ports, plan, candidate = self._system(root, library=True)

            built = ports.build(plan, ())
            export = ports.artifact_path(built.exports[0])

            self.assertTrue(export.is_dir())
            self.assertTrue((export / "source/Cargo.lock").is_file())
            self.assertTrue((export / "source/src/lib.rs").is_file())
            self.assertEqual(
                local_generated_source_tree_identity(root / "generated"),
                candidate.tree_identity,
            )
            self.assertEqual(
                (export / "cargo.log").read_text().splitlines()[-1], "build"
            )

    def test_cargo_derives_lock_externally_and_retains_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ports, plan, candidate = self._system(root)

            built = ports.build(plan, ())

            self.assertEqual(
                ports.read_artifact_blob(built.exports[0].blob), b"compiled-by-cargo\n"
            )
            self.assertEqual(
                local_generated_source_tree_identity(root / "generated"),
                candidate.tree_identity,
            )
            self.assertFalse((root / "generated/source/Cargo.lock").exists())
            artifact_root = ports._artifact_paths[built.exports[0].identity.uri]
            evidence = artifact_root / ".literate/cargo"
            self.assertTrue((evidence / "Cargo.lock").is_file())
            self.assertTrue((evidence / "metadata.json").is_file())
            manifest = json.loads(
                (evidence / "evidence-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                manifest["authorization_id"], plan.request.authorization_identity.uri
            )
            self.assertEqual(len(manifest["files"]), 2)

    def test_cargo_cache_reuses_validated_retained_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ports, plan, _candidate = self._system(root)

            first = ports.build(plan, ())
            second = ports.build(plan, ())

            self.assertEqual(first, second)
            self.assertEqual(ports.build_cache_misses, 1)
            self.assertEqual(ports.build_cache_hits, 1)

    def test_cache_rejects_coordinated_self_manifest_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ports, plan, _candidate = self._system(Path(temporary))
            first = ports.build(plan, ())
            rewrite_self_authenticating_artifact(
                ports.artifact_path(first.exports[0]).parent,
                "app",
                b"tampered-by-cargo\n",
            )

            with self.assertRaisesRegex(
                LocalStandardLifecycleError, "changed after publication"
            ):
                ports.build(plan, ())

    def test_restarted_runtime_hits_durable_external_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            first, plan, _candidate = self._system(Path(temporary))
            expected = first.build(plan, ())
            second = StandardCargoLifecyclePorts(
                source_trees=first.source_trees,
                object_root=first.object_root,
                contracts=tuple(first.contracts.values()),
                tool_bindings=tuple(first.tool_bindings.values()),
                cargo_targets=tuple(first.cargo_targets.values()),
                dependency_observation=first.dependency_observation,
            )
            cached = second.build(plan, ())

            self.assertEqual(cached, expected)
            self.assertEqual(second.build_cache_misses, 0)
            self.assertEqual(second.build_cache_hits, 1)

    def test_artifact_bytes_without_checkpoint_are_rebuilt_not_trusted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, plan, _candidate = self._system(root)
            expected = first.build(plan, ())
            isolated = root / "isolated-objects"
            copy_digest_cache_without_sidecars(first.object_root, isolated)
            second = StandardCargoLifecyclePorts(
                source_trees=first.source_trees,
                object_root=isolated,
                contracts=tuple(first.contracts.values()),
                tool_bindings=tuple(first.tool_bindings.values()),
                cargo_targets=tuple(first.cargo_targets.values()),
                dependency_observation=first.dependency_observation,
            )
            replayed = second.build(plan, ())
            cached = second.build(plan, ())

            self.assertEqual(replayed, expected)
            self.assertEqual(cached, expected)
            self.assertEqual(second.build_cache_misses, 1)
            self.assertEqual(second.build_cache_hits, 1)

    def test_cargo_evidence_tamper_blocks_cached_artifact_use(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ports, plan, _candidate = self._system(root)
            built = ports.build(plan, ())
            artifact_root = ports._artifact_paths[built.exports[0].identity.uri]
            (artifact_root / ".literate/cargo/metadata.json").write_text(
                "{}\n", encoding="utf-8"
            )

            with self.assertRaisesRegex(
                LocalStandardLifecycleError, "changed after publication"
            ):
                ports.build(plan, ())

    def test_model_written_cargo_lock_is_rejected_before_cargo_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ports, plan, _candidate = self._system(root, model_lock=True)

            with self.assertRaisesRegex(
                LocalStandardLifecycleError, "must not contain Cargo.lock"
            ):
                ports.build(plan, ())
            self.assertFalse((root / "generated/cargo.log").exists())


if __name__ == "__main__":
    unittest.main()
