"""Cargo-native artifact production through the local Standard lifecycle."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

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
from tests.support.fixtures_test_component_node_generation_preparation import _fixture
from tests.support.standard_source_evidence_fixture import register_strict_source


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
