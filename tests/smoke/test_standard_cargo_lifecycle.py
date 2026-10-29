"""Cargo-native artifact production through the local Standard lifecycle."""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.lifecycle.cargo_workspace import sync_projection
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
        native: bool = False,
        main_source: str = "fn main() {}\n",
        platform: str = "platform",
    ):
        snapshot, execution = _fixture()
        generation_plan = execution.generation_plans[0]
        fake_cargo = root / "fake-cargo.py"
        _write_fake_cargo(fake_cargo)
        cargo = (
            LocalComponentToolBinding(shutil.which("cargo"))
            if native
            else LocalComponentToolBinding(sys.executable, (str(fake_cargo),))
        )
        python = LocalComponentToolBinding(
            shutil.which("rustc") if native else sys.executable
        )
        resolver = _identity("cargo-resolver")
        provisional = StandardCargoTarget(
            generation_plan.component_revision,
            resolver,
            cargo.toolchain_identity,
            python.toolchain_identity,
            "source/Cargo.toml",
            "litai_artifact",
            python.command,
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
                _identity(platform),
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
        package.joinpath("src").mkdir(parents=True, exist_ok=True)
        package.joinpath("Cargo.toml").write_text(
            '[package]\nname = "fixture"\nversion = "1.0.0"\nedition = "2021"\n'
            '[[bin]]\nname = "litai_artifact"\npath = "src/main.rs"\n',
            encoding="utf-8",
        )
        package.joinpath("src/main.rs").write_text(main_source, encoding="utf-8")
        if native:
            package.joinpath("src/lib.rs").write_text(
                "pub fn value() -> u8 { 7 }\n", encoding="utf-8"
            )
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

    def test_workspace_tampering_is_refused_before_cargo_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ports, plan, _ = self._system(root)
            ports.build(plan, ())
            workspace = next((ports.object_root / "cargo-work").glob("*/projection"))
            (workspace / "source/src/main.rs").write_text("tampered")
            with self.assertRaisesRegex(
                LocalStandardLifecycleError, "workspace changed"
            ):
                ports.build(plan, ())

    def test_failed_build_discards_persistent_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ports, plan, _ = self._system(root)
            built = ports.build(plan, ())
            shutil.rmtree(ports.artifact_path(built.exports[0]).parent)
            with patch(
                "literate_ai.adapters.lifecycle.standard_cargo.run_bounded_process",
                side_effect=RuntimeError("failed build"),
            ):
                with self.assertRaisesRegex(RuntimeError, "failed build"):
                    ports.build(plan, ())
            self.assertEqual(list((ports.object_root / "cargo-work").iterdir()), [])
            ports.build(plan, ())

    def test_workspace_link_is_refused_without_touching_its_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ports, plan, _ = self._system(root)
            ports.build(plan, ())
            workspace = next((ports.object_root / "cargo-work").glob("*/objects"))
            outside = root / "outside"
            outside.write_text("preserve me")
            try:
                (workspace / "foreign").symlink_to(outside)
            except OSError as exc:
                self.skipTest(f"host cannot create test symlink: {exc}")
            with self.assertRaisesRegex(LocalStandardLifecycleError, "unsafe node"):
                ports.build(plan, ())
            self.assertEqual(outside.read_text(), "preserve me")

    def test_interrupted_unsealed_workspace_is_discarded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ports, plan, _ = self._system(root)
            built = ports.build(plan, ())
            shutil.rmtree(ports.artifact_path(built.exports[0]).parent)
            workspace = next((ports.object_root / "cargo-work").glob("*/objects"))
            (workspace / "partial-output").write_text("unfinished")
            workspace.parent.with_suffix(".seal").unlink()
            ports.build(plan, ())
            self.assertFalse((workspace / "partial-output").exists())

    def test_target_namespaces_do_not_share_intermediates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ports, plan, _ = self._system(root)
            ports.build(plan, ())
            ports, plan, _ = self._system(root, platform="another-target")
            ports.build(plan, ())
            self.assertEqual(
                len(list((ports.object_root / "cargo-work").glob("*/objects"))), 2
            )

    def test_projection_sync_removes_stale_inputs_and_preserves_unchanged_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            projection = root / "projection"
            source.mkdir()
            (source / "same.rs").write_text("same")
            (source / "changed.rs").write_text("before")
            (source / "removed.rs").write_text("removed")
            sync_projection(source, projection)
            unchanged_mtime = (projection / "same.rs").stat().st_mtime_ns
            (source / "changed.rs").write_text("after")
            (source / "removed.rs").unlink()
            (source / "new.rs").write_text("new")
            (projection / "Cargo.lock").write_text("old resolution")
            sync_projection(source, projection)
            self.assertEqual(
                (projection / "same.rs").stat().st_mtime_ns, unchanged_mtime
            )
            self.assertEqual((projection / "changed.rs").read_text(), "after")
            self.assertEqual((projection / "new.rs").read_text(), "new")
            self.assertFalse((projection / "removed.rs").exists())
            self.assertFalse((projection / "Cargo.lock").exists())

    @unittest.skipUnless(
        shutil.which("cargo") and shutil.which("rustc"), "Rust required"
    )
    def test_native_cargo_reuses_target_and_rebuilds_edited_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ports, plan, _ = self._system(root, native=True)
            built = ports.build(plan, ())
            target = next(
                (ports.object_root / "cargo-work").glob("*/objects/cargo-target")
            )
            executable = (
                target
                / "debug"
                / (
                    "litai_artifact.exe"
                    if sys.platform == "win32"
                    else "litai_artifact"
                )
            )
            original_mtime = executable.stat().st_mtime_ns
            original_bytes = executable.read_bytes()
            library = next((target / "debug/deps").glob("libfixture-*.rlib"))
            library_mtime = library.stat().st_mtime_ns
            shutil.rmtree(ports.artifact_path(built.exports[0]).parent)

            # A fresh source materialization has new timestamps. Its identical
            # files must retain the old projection timestamps and remain Fresh.
            ports, plan, _ = self._system(root, native=True)
            built = ports.build(plan, ())
            self.assertEqual(executable.stat().st_mtime_ns, original_mtime)
            self.assertEqual(executable.read_bytes(), original_bytes)

            ports, plan, _ = self._system(
                root, native=True, main_source='fn main() { println!("edited"); }\n'
            )
            ports.build(plan, ())
            self.assertEqual(
                list((ports.object_root / "cargo-work").glob("*/objects/cargo-target")),
                [target],
            )
            self.assertNotEqual(executable.read_bytes(), original_bytes)
            self.assertEqual(library.stat().st_mtime_ns, library_mtime)


if __name__ == "__main__":
    unittest.main()
