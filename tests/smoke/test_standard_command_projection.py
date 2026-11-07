"""Locked Component/Flavor projection into complete Standard host commands."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from literate_ai.adapters.component_lock_planning import FilesystemComponentLockPlanner
from literate_ai.adapters.component_locks import ComponentLockStore
from literate_ai.adapters.component_resolution_audits import (
    ComponentResolutionAuditStore,
)
from literate_ai.adapters.locked_generation_authority import (
    FilesystemLockedGenerationAuthorityReader,
)
from literate_ai.adapters.standard_project import (
    _STANDARD_LIBRARY_IMPORT_DRIVER,
    _STANDARD_LIBRARY_TEST_DRIVER,
    _encoded_library_import_surface,
    project_locked_standard_toolchain_closure,
)
from literate_ai.application.component_execution_planning import (
    plan_component_execution,
)
from literate_ai.application.component_lock_resolution import ComponentLockResolver
from literate_ai.contracts import (
    ComponentCommandPhase,
    LibraryCapabilityImport,
    LibraryImportSurface,
    StandardPythonWheelCommandProfile,
    canonical_identity,
)
from tests.support.fixtures_test_component_lock_planning import _fixture


def _tool(name: str, *, environment: tuple[tuple[str, str], ...] = ()):
    return SimpleNamespace(
        command=(str(Path(sys.executable).resolve(strict=True)),),
        environment=environment,
        identity=canonical_identity({"fake-standard-toolchain": name}).uri,
        require_unchanged=lambda: None,
    )


def _observation(commands):
    from literate_ai.adapters.dependencies import HostDependencyObservation

    components = tuple(
        {
            "type": "application",
            "bom-ref": f"toolchain:{index}",
            "name": command[0],
        }
        for index, command in enumerate(commands)
    )
    return HostDependencyObservation(
        components,
        tuple(("component:test-root", item["bom-ref"]) for item in components),
    )


def _locked_snapshot(
    root: Path,
    *,
    build_system: str | None = None,
    language: str = "python",
    platform: str = "macos",
    accelerator: str | None = None,
    package_npm: bool = False,
    package_python: bool = False,
    generation_ready: bool = False,
    no_entrypoint: bool = False,
    cpp_library_kind: str | None = None,
    duplicate_entrypoint: bool = False,
):
    component, flavors = _fixture(root)
    if generation_ready:
        document = component / "component.md"
        document.write_text(
            document.read_text(encoding="utf-8").replace(
                "skills/implement.json",
                "skills/specification-to-source/portable-application-implementation/SKILL.md\n"
                "  - kind: specification-to-source-skill\n"
                "    uri: skills/specification-to-source/"
                "portable-specification-planning/SKILL.md",
            ),
            encoding="utf-8",
        )
        (component / "acceptance/execution.json").write_text(
            json.dumps(
                {
                    "invocations": [{"arguments": [{"component": "greeting"}]}],
                    "result_shape": {"component": "string"},
                }
            ),
            encoding="utf-8",
        )
    if no_entrypoint:
        document = component / "component.md"
        text = document.read_text(encoding="utf-8")
        text = text.replace("version: 1.0.0\n", "version: 1.0.0\nkind: library\n", 1)
        text = text.replace(
            "  - name: sample.portable-app\n    version: 1.0.0\n",
            "  - name: sample.portable-app\n"
            "    version: 1.0.0\n"
            "    interface:\n"
            "      uri: interfaces/library.md\n",
        )
        text = text.replace(
            "entrypoints:\n  - name: run\n    kind: portable-application\n"
            "    path: run\n",
            "entrypoints: []\n",
        )
        document.write_text(text, encoding="utf-8")
        interface = component / "interfaces/library.md"
        interface.parent.mkdir(parents=True, exist_ok=True)
        interface.write_text(
            "# Portable library interface\n\n"
            "The package SHALL expose the sample portable capability in-process.\n",
            encoding="utf-8",
        )
    if no_entrypoint and language == "cpp":
        document = component / "component.md"
        text = document.read_text().replace(
            "entrypoints: []\n",
            "entrypoints: []\n"
            "library_imports:\n  - language: cpp\n    package: sample\n"
            "    capability: sample.portable-app\n    module: sample/api.hpp\n"
            "    symbols:\n      - 'sample::add'\n",
        )
        document.write_text(text)
        (component / "specs/spec.md").write_text(
            "# Integer addition library\n\n"
            "### Requirement: Exact integer addition\n\n"
            "The `sample::add` library function SHALL return the mathematical sum "
            "of its two integer arguments.\n\n"
            "#### Scenario: Add two integers\n\n"
            "- **WHEN** a consumer calls `sample::add` with two integers\n"
            "- **THEN** the result is their exact mathematical sum\n",
            encoding="utf-8",
        )
    if duplicate_entrypoint:
        document = component / "component.md"
        text = document.read_text(encoding="utf-8")
        text = text.replace(
            "entrypoints:\n  - name: run\n    kind: portable-application\n"
            "    path: run\n",
            "entrypoints:\n"
            "  - name: run\n    kind: portable-application\n    path: run\n"
            "  - name: run-again\n    kind: portable-application\n"
            "    path: run-again\n",
        )
        document.write_text(text, encoding="utf-8")
    repository = Path(__file__).resolve().parents[2]
    if language != "python":
        import shutil

        shutil.rmtree(flavors / "lang-python")
        shutil.copytree(
            repository / "flavors" / f"lang-{language}",
            flavors / f"lang-{language}",
        )
        skill_by_language = {
            "elixir": "elixir-portable-application",
            "cpp": "cpp17-portable-json-application",
            "javascript": "javascript-portable-json-application",
            "rust": "rust-portable-json-application",
        }
        shutil.copytree(
            repository / "skills/specification-to-source" / skill_by_language[language],
            root / "skills/specification-to-source" / skill_by_language[language],
        )
    if cpp_library_kind is not None:
        profile = flavors / "lang-cpp/standard-command-profile.json"
        data = json.loads(profile.read_text())
        data["cpp_library_kind"] = cpp_library_kind
        profile.write_text(json.dumps(data))
    if no_entrypoint and language == "cpp":
        document = component / "component.md"
        document.write_text(
            document.read_text().replace(
                "uri: skills/implement.json",
                "uri: skills/specification-to-source/"
                "portable-application-implementation/SKILL.md\n"
                "  - kind: specification-to-source-skill\n"
                "    uri: skills/specification-to-source/"
                "portable-specification-planning/SKILL.md",
            )
        )
    if platform != "macos":
        import shutil

        shutil.rmtree(flavors / "os-macos")
        shutil.copytree(
            repository / "flavors" / f"os-{platform}",
            flavors / f"os-{platform}",
        )
    selectors = [
        f"+flavor://literate-ai/os-{platform}",
        f"+flavor://literate-ai/lang-{language}",
    ]
    if build_system is not None:
        document = component / "component.md"
        text = document.read_text(encoding="utf-8")
        text = text.replace(
            "  - slot_id: os\n",
            "  - slot_id: build-system\n"
            "    axis: build.system\n"
            "    cardinality: exactly-one\n"
            "    capability_contract: sample.portable-app\n"
            "  - slot_id: os\n",
        )
        document.write_text(text, encoding="utf-8")
        import shutil

        shutil.copytree(
            repository / "flavors" / f"build-{build_system}",
            flavors / f"build-{build_system}",
        )
        shutil.copytree(
            repository
            / "skills/specification-to-source"
            / f"{build_system}-build-system",
            root / "skills/specification-to-source" / f"{build_system}-build-system",
        )
        if build_system == "cargo":
            shutil.copytree(
                repository / "skills/specification-to-source/rust-ecosystem",
                root / "skills/specification-to-source/rust-ecosystem",
            )
        selectors.append(f"+flavor://literate-ai/build-{build_system}")
    if accelerator is not None:
        document = component / "component.md"
        text = document.read_text(encoding="utf-8")
        text = text.replace(
            "  - slot_id: os\n",
            "  - slot_id: accelerator\n"
            "    axis: accelerator\n"
            "    cardinality: exactly-one\n"
            "    capability_contract: sample.portable-app\n"
            "  - slot_id: os\n",
        )
        document.write_text(text, encoding="utf-8")
        import shutil

        shutil.copytree(repository / "flavors" / accelerator, flavors / accelerator)
        for skill in (
            "skills/agent/select-nvidia-accelerated-stack",
            "skills/specification-to-source/generate-nvidia-cuda-application",
        ):
            source = repository / skill
            destination = root / skill
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, destination)
        selectors.append("+flavor://literate-ai/accel-nvidia-cuda")
    if package_npm or package_python:
        document = component / "component.md"
        text = document.read_text(encoding="utf-8")
        text = text.replace(
            "  - slot_id: os\n",
            "  - slot_id: packages\n"
            "    axis: packaging\n"
            "    cardinality: zero-or-one\n"
            "    capability_contract: sample.portable-app\n"
            "  - slot_id: os\n",
        )
        document.write_text(text, encoding="utf-8", newline="\n")
        import shutil

        package_name = "package-npm" if package_npm else "package-pip"
        shutil.copytree(repository / "flavors" / package_name, flavors / package_name)
        if package_python:
            flavor_path = flavors / package_name / "flavor.md"
            content = flavor_path.read_text(encoding="utf-8")
            content = content.replace(
                "contributions:\n",
                "contributions:\n"
                "  - contribution_id: python-wheel-command-profile\n"
                "    kind: builder\n"
                "    merge_operator: exact-singleton\n"
                "    slot: standard-package-command\n"
                "    content:\n"
                "      kind: standard-command-profile\n"
                "      uri: standard-command-profile.json\n",
            )
            flavor_path.write_text(content, encoding="utf-8", newline="\n")
            (flavors / package_name / "standard-command-profile.json").write_text(
                json.dumps(
                    {
                        "schema": StandardPythonWheelCommandProfile.SCHEMA,
                        "target": "pip",
                        "toolchain": "python",
                        "manifest": "source/requirements.txt",
                        "lockfile": "source/python-wheel-lock.json",
                    }
                ),
                encoding="utf-8",
            )
        for skill in (
            "skills/agent/package-artifacts",
            "skills/specification-to-source/javascript-ecosystem",
        ):
            source = repository / skill
            destination = root / skill
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, destination)
        selectors.append("+flavor://literate-ai/" + package_name)
    target = "standard-command-test"
    planner = FilesystemComponentLockPlanner()
    plan = planner.plan(
        component,
        target_name=target,
        flavor_selectors=tuple(selectors),
        flavor_roots=(flavors,),
    )
    result = ComponentLockResolver().resolve(
        plan, expected_input_evidence_identity=plan.identity
    )
    lock = result.lock
    ComponentResolutionAuditStore(component, target).update(result.catalog_audit)
    ComponentLockStore(component).update(lock)
    snapshot = FilesystemLockedGenerationAuthorityReader().read(
        component,
        target_name=target,
        flavor_selectors=tuple(selectors),
        flavor_roots=(flavors,),
    )
    execution = plan_component_execution(
        lock,
        model_identities={
            node.revision.identity.uri: canonical_identity(
                {"model": node.revision.identity.uri}
            )
            for node in lock.nodes
        },
    )
    return component, snapshot, execution


class StandardCommandProjectionTests(unittest.TestCase):
    @staticmethod
    def _run_library_driver(
        driver: str,
        language: str,
        tool: str,
        export: Path,
        surface: LibraryImportSurface,
        *,
        test: bool,
    ) -> subprocess.CompletedProcess[str]:
        arguments = (
            [
                sys.executable,
                "-c",
                driver,
                language,
                json.dumps([tool]),
                str(export),
                "source/Cargo.toml" if language == "rust" else "-",
                "litai-test" if language == "rust" else "-",
                str(export.parent),
            ]
            if test
            else [
                sys.executable,
                "-c",
                driver,
                language,
                json.dumps([tool]),
                _encoded_library_import_surface(surface),
                str(export),
                "source/Cargo.toml" if language == "rust" else "-",
                str(export.parent),
            ]
        )
        return subprocess.run(arguments, capture_output=True, text=True)

    def test_cpp_static_and_shared_commands_bind_native_platform_layouts(self):
        for platform in ("linux", "macos", "windows"):
            identities = []
            for kind in ("static", "shared"):
                with (
                    self.subTest(platform=platform, kind=kind),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    _, snapshot, execution = _locked_snapshot(
                        Path(directory),
                        language="cpp",
                        platform=platform,
                        no_entrypoint=True,
                        build_system="bazel",
                        cpp_library_kind=None if kind == "static" else kind,
                    )
                    closure = project_locked_standard_toolchain_closure(
                        snapshot,
                        execution,
                        host_platform=platform,
                        toolchain_discoverer=lambda name, _constraint, _environment: (
                            _tool(name)
                        ),
                        dependency_observer=_observation,
                    )
                    contract = closure.contracts[0]
                    target = closure.bazel_targets[0]
                    self.assertEqual(contract.native_layout, target.cpp_layout)
                    self.assertEqual(contract.native_layout.kind, kind)
                    self.assertEqual(
                        contract.native_layout.headers, ("include/sample/api.hpp",)
                    )
                    self.assertEqual(target.output_path, "library")
                    self.assertEqual(
                        contract.locked_build_authority_identity, target.identity
                    )
                    self.assertNotIn(
                        "--litai-smoke",
                        contract.command(ComponentCommandPhase.EXECUTE).argv,
                    )
                    self.assertEqual(
                        target.cpp_test_output,
                        "tests/run.exe" if platform == "windows" else "tests/run",
                    )
                    from literate_ai.adapters.generation_preparation import (
                        LockedComponentNodePreparationAdapter,
                    )

                    recipe = (
                        LockedComponentNodePreparationAdapter()
                        .project(snapshot, execution.generation_plans[0])
                        .recipe
                    )
                    self.assertEqual(
                        recipe.cpp_library_build.layout, contract.native_layout
                    )
                    self.assertEqual(
                        recipe.cpp_library_build.build_profile.target_label,
                        target.target_label,
                    )
                    self.assertEqual(
                        recipe.cpp_library_build.to_dict()["test_output"],
                        target.cpp_test_output,
                    )
                    self.assertIn(
                        "Required compiled C++ library build", recipe.prompt()
                    )
                    identities.append(contract.artifact_export.abi_identity)
            self.assertNotEqual(*identities)

    def test_python_library_commands_build_test_and_import_exact_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _component, snapshot, execution = _locked_snapshot(
                root, language="python", no_entrypoint=True
            )
            closure = project_locked_standard_toolchain_closure(
                snapshot,
                execution,
                host_platform="macos",
                toolchain_discoverer=(
                    lambda name, _constraint, _environment: _tool(name)
                ),
                dependency_observer=_observation,
            )
            contract = closure.contracts[0]
            surface = contract.library_import_surface
            assert surface is not None
            capability = surface.capabilities[0]
            source = root / "generated"
            module = source / "source" / Path(*capability.module.split("."))
            module.parent.mkdir(parents=True)
            for parent in module.parents:
                if parent == source / "source":
                    break
                parent.joinpath("__init__.py").touch()
            module.with_suffix(".py").write_text(
                "from __future__ import annotations\n"
                "from dataclasses import dataclass\n"
                "from typing import NamedTuple\n"
                "@dataclass\n"
                "class Value:\n    value: int\n"
                "class Record(NamedTuple):\n    value: int\n"
                f"{capability.symbols[0]} = Record(Value(7).value).value\n",
                encoding="utf-8",
            )
            (source / "source" / "main.py").write_text(
                "import json\n"
                "print(json.dumps({'schema':'literate-ai/generated-test-results@1',"
                "'cases':[{'case_id':'fixture-example','outcome':'passed'}]}))\n",
                encoding="utf-8",
            )
            artifact_root = root / "artifact"
            object_root = root / "objects"
            artifact_root.mkdir()
            export = artifact_root / contract.artifact_export.export_id

            def argv(phase: ComponentCommandPhase) -> list[str]:
                replacements = {
                    "{tool}": sys.executable,
                    "{source_root}": str(source),
                    "{object_root}": str(object_root),
                    "{artifact_root}": str(artifact_root),
                    "{export_path}": str(export),
                    "{provider_artifacts}": "[]",
                }
                return [
                    replacements.get(value, value)
                    for value in contract.command(phase).argv
                ]

            subprocess.run(argv(ComponentCommandPhase.BUILD), check=True)
            tested = subprocess.run(
                argv(ComponentCommandPhase.TEST),
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                json.loads(tested.stdout)["cases"][0]["case_id"],
                "fixture-example",
            )
            shadow = root / "shadow" / surface.package
            shadow.mkdir(parents=True)
            shadow.joinpath("__init__.py").write_text("", encoding="utf-8")
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(shadow.parent)
            imported = subprocess.run(
                argv(ComponentCommandPhase.EXECUTE),
                capture_output=True,
                text=True,
                cwd=shadow.parent,
                env=environment,
            )
            self.assertEqual(imported.returncode, 0, imported.stderr)
            self.assertEqual(
                json.loads(imported.stdout)["capabilities"],
                [capability.capability],
            )
            exported_module = export / "source" / Path(*capability.module.split("."))
            # Isolation must still exclude an ambient-only dependency even
            # though interpreter standard-library paths remain available.
            shadow.parent.joinpath("ambient_only_dependency.py").write_text(
                "value = 7\n", encoding="utf-8"
            )
            exported_module.with_suffix(".py").write_text(
                "import ambient_only_dependency\n"
                f"{capability.symbols[0]} = ambient_only_dependency.value\n",
                encoding="utf-8",
            )
            ambient_rejected = subprocess.run(
                argv(ComponentCommandPhase.EXECUTE),
                capture_output=True,
                text=True,
                cwd=shadow.parent,
                env=environment,
            )
            self.assertNotEqual(ambient_rejected.returncode, 0)
            self.assertIn("ambient_only_dependency", ambient_rejected.stderr)
            # A capability cannot report a standard-library file as its own
            # origin, even when the real stdlib can be imported successfully.
            exported_module.with_suffix(".py").write_text(
                "import dataclasses\n"
                "__file__ = dataclasses.__file__\n"
                f"{capability.symbols[0]} = 7\n",
                encoding="utf-8",
            )
            foreign_rejected = subprocess.run(
                argv(ComponentCommandPhase.EXECUTE),
                capture_output=True,
                text=True,
                cwd=shadow.parent,
                env=environment,
            )
            self.assertNotEqual(foreign_rejected.returncode, 0)
            self.assertIn(
                "library import escaped the exact artifact", foreign_rejected.stderr
            )
            exported_module.with_suffix(".py").write_text(
                "other = 7\n", encoding="utf-8"
            )
            rejected = subprocess.run(
                argv(ComponentCommandPhase.EXECUTE), capture_output=True, text=True
            )
            self.assertNotEqual(rejected.returncode, 0)

    def test_javascript_library_verifier_imports_exact_exports(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is unavailable")
        surface = LibraryImportSurface(
            "javascript",
            "fixture_library",
            (
                LibraryCapabilityImport(
                    "fixture.logic",
                    canonical_identity({"interface": "javascript-library"}),
                    "fixture_library/logic",
                    ("logic",),
                ),
            ),
        )
        with tempfile.TemporaryDirectory() as temporary:
            export = Path(temporary) / "artifact" / "library"
            module = export / "source/fixture_library/logic.js"
            module.parent.mkdir(parents=True)
            module.write_text("module.exports = { logic: 7 };\n", encoding="utf-8")
            (module.parent / "package.json").write_text(
                json.dumps(
                    {
                        "name": "fixture_library",
                        "type": "commonjs",
                        "exports": {"./logic": "./logic.js"},
                    }
                ),
                encoding="utf-8",
            )
            (export / "source/main.js").write_text(
                "process.stdout.write(JSON.stringify({schema:"
                "'literate-ai/generated-test-results@1',cases:[{case_id:"
                "'fixture-example',outcome:'passed'}]}));\n",
                encoding="utf-8",
            )
            tested = self._run_library_driver(
                _STANDARD_LIBRARY_TEST_DRIVER,
                "javascript",
                node,
                export,
                surface,
                test=True,
            )
            self.assertEqual(tested.returncode, 0, tested.stderr)
            imported = self._run_library_driver(
                _STANDARD_LIBRARY_IMPORT_DRIVER,
                "javascript",
                node,
                export,
                surface,
                test=False,
            )
            self.assertEqual(imported.returncode, 0, imported.stderr)
            self.assertEqual(
                json.loads(imported.stdout)["capabilities"], ["fixture.logic"]
            )
            module.write_text("module.exports = { other: 7 };\n", encoding="utf-8")
            rejected = self._run_library_driver(
                _STANDARD_LIBRARY_IMPORT_DRIVER,
                "javascript",
                node,
                export,
                surface,
                test=False,
            )
            self.assertNotEqual(rejected.returncode, 0)

    def test_javascript_exports_ignore_order_but_reject_changes(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is unavailable")
        surface = LibraryImportSurface(
            "javascript",
            "fixture_library",
            tuple(
                LibraryCapabilityImport(
                    f"fixture.{name}",
                    canonical_identity({"interface": name}),
                    f"fixture_library/{name}",
                    (name,),
                )
                for name in ("alpha", "beta")
            ),
        )
        with tempfile.TemporaryDirectory() as temporary:
            export = Path(temporary) / "artifact" / "library"
            package = export / "source" / surface.package
            package.mkdir(parents=True)
            for name in ("alpha", "beta"):
                (package / f"{name}.js").write_text(
                    f"module.exports = {{ {name}: 7 }};\n", encoding="utf-8"
                )
            manifest = {
                "name": surface.package,
                "type": "commonjs",
                "exports": {"./beta": "./beta.js", "./alpha": "./alpha.js"},
            }

            def run():
                (package / "package.json").write_text(
                    json.dumps(manifest), encoding="utf-8"
                )
                return self._run_library_driver(
                    _STANDARD_LIBRARY_IMPORT_DRIVER,
                    "javascript",
                    node,
                    export,
                    surface,
                    test=False,
                )

            accepted = run()
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            self.assertEqual(
                json.loads(accepted.stdout)["capabilities"],
                ["fixture.alpha", "fixture.beta"],
            )
            manifest["exports"]["./extra"] = "./extra.js"
            extra = run()
            self.assertNotEqual(extra.returncode, 0)
            self.assertIn("undeclared entries", extra.stderr)
            del manifest["exports"]["./extra"]
            del manifest["exports"]["./alpha"]
            manifest["exports"]["./unexpected"] = "./alpha.js"
            swapped = run()
            self.assertNotEqual(swapped.returncode, 0)
            self.assertIn("exports map differs for ./alpha", swapped.stderr)
            del manifest["exports"]["./unexpected"]
            manifest["exports"]["./alpha"] = "./alpha.js"
            manifest["exports"]["./beta"] = "./wrong.js"
            wrong = run()
            self.assertNotEqual(wrong.returncode, 0)
            self.assertIn("exports map differs", wrong.stderr)

    def test_rust_import_ignores_unrelated_caller_cargo_configuration(self) -> None:
        cargo = shutil.which("cargo")
        rustc = shutil.which("rustc")
        if cargo is None or rustc is None:
            self.skipTest("native Cargo/rustc is unavailable")
        surface = LibraryImportSurface(
            "rust",
            "fixture_library",
            (
                LibraryCapabilityImport(
                    "fixture.logic",
                    canonical_identity({"interface": "rust-consumer-cwd"}),
                    "fixture_library::logic",
                    ("value",),
                ),
            ),
        )
        with (
            tempfile.TemporaryDirectory(prefix="litai-caller-") as caller_raw,
            tempfile.TemporaryDirectory(prefix="litai-consumer-") as consumer_raw,
        ):
            caller = Path(caller_raw)
            consumer = Path(consumer_raw)
            work = caller / "work"
            work.mkdir()
            marker = caller / "wrapper-used"
            wrapper = caller / (
                "caller-tripwire.exe" if os.name == "nt" else "caller-tripwire"
            )
            wrapper_source = caller / "tripwire.rs"
            wrapper_source.write_text(
                'fn main() { let path = std::env::var_os("LITAI_TEST_CARGO_TRIPWIRE")'
                '.expect("fixture marker"); std::fs::write(path, b"called")'
                '.expect("fixture write"); std::process::exit(86); }\n',
                encoding="utf-8",
            )
            subprocess.run(
                [
                    rustc,
                    "--crate-name",
                    "caller_tripwire",
                    str(wrapper_source),
                    "-o",
                    str(wrapper),
                ],
                capture_output=True,
                check=True,
                timeout=60,
            )
            config = caller / ".cargo/config.toml"
            config.parent.mkdir()
            config_text = "[build]\nrustc-wrapper = " + json.dumps(str(wrapper)) + "\n"
            config.write_text(config_text, encoding="utf-8")
            export = consumer / "artifact/library"
            package = export / "source"
            (package / "src").mkdir(parents=True)
            manifest = package / "Cargo.toml"
            manifest.write_text(
                '[package]\nname="fixture-library"\nversion="0.0.0"\nedition="2021"\n',
                encoding="utf-8",
            )
            library = package / "src/lib.rs"
            library.write_text(
                "pub mod logic { pub fn value() -> u8 { 7 } }\n", encoding="utf-8"
            )
            admitted_source = {path: path.read_bytes() for path in (manifest, library)}
            cargo_home = consumer / "cargo-home"
            cargo_home.mkdir()
            environment = dict(os.environ)
            environment.update(
                CARGO_HOME=str(cargo_home),
                TMPDIR=str(consumer),
                TMP=str(consumer),
                TEMP=str(consumer),
                LITAI_TEST_CARGO_TRIPWIRE=str(marker),
            )
            # The fixture chooses its hostile wrapper through Cargo configuration.
            # Do not let an operator's wrapper environment mask that tripwire.
            environment.pop("RUSTC_WRAPPER", None)
            environment.pop("RUSTC_WORKSPACE_WRAPPER", None)
            hostile = subprocess.run(
                [cargo, "check", "--offline", "--manifest-path", str(manifest)],
                cwd=work,
                env=environment,
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertNotEqual(hostile.returncode, 0)
            self.assertEqual(marker.read_bytes(), b"called")
            marker.unlink()
            imported = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    _STANDARD_LIBRARY_IMPORT_DRIVER,
                    "rust",
                    json.dumps([cargo]),
                    _encoded_library_import_surface(surface),
                    str(export),
                    "source/Cargo.toml",
                    str(export.parent),
                ],
                cwd=work,
                env=environment,
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(imported.returncode, 0, imported.stderr)
            self.assertEqual(
                json.loads(imported.stdout)["capabilities"], ["fixture.logic"]
            )
            self.assertFalse(marker.exists(), "unrelated caller wrapper was invoked")
            self.assertEqual(config.read_text(encoding="utf-8"), config_text)
            self.assertEqual(
                {path: path.read_bytes() for path in admitted_source}, admitted_source
            )

    def test_rust_library_verifier_compiles_exact_path_consumer(self) -> None:
        cargo = shutil.which("cargo")
        if cargo is None:
            self.skipTest("Cargo is unavailable")
        surface = LibraryImportSurface(
            "rust",
            "fixture_library",
            (
                LibraryCapabilityImport(
                    "fixture.logic",
                    canonical_identity({"interface": "rust-library"}),
                    "fixture_library::logic",
                    ("value",),
                ),
            ),
        )
        with tempfile.TemporaryDirectory() as temporary:
            export = Path(temporary) / "artifact" / "library"
            package = export / "source"
            (package / "src/bin").mkdir(parents=True)
            (package / "Cargo.toml").write_text(
                '[package]\nname="fixture-library"\nversion="0.0.0"\n'
                'edition="2021"\n[[bin]]\nname="litai-test"\n'
                'path="src/bin/litai-test.rs"\n',
                encoding="utf-8",
            )
            library = package / "src/lib.rs"
            library.write_text(
                "pub mod logic { pub const value: u8 = 7; }\n", encoding="utf-8"
            )
            (package / "src/bin/litai-test.rs").write_text(
                'fn main(){println!(r#"{{"schema":'
                '"literate-ai/generated-test-results@1","cases":[{{'
                '"case_id":"fixture-example","outcome":"passed"}}]}}"#);}\n',
                encoding="utf-8",
            )
            subprocess.run(
                [
                    cargo,
                    "generate-lockfile",
                    "--manifest-path",
                    str(package / "Cargo.toml"),
                ],
                check=True,
                capture_output=True,
            )
            tested = self._run_library_driver(
                _STANDARD_LIBRARY_TEST_DRIVER,
                "rust",
                cargo,
                export,
                surface,
                test=True,
            )
            self.assertEqual(tested.returncode, 0, tested.stderr)
            imported = self._run_library_driver(
                _STANDARD_LIBRARY_IMPORT_DRIVER,
                "rust",
                cargo,
                export,
                surface,
                test=False,
            )
            self.assertEqual(imported.returncode, 0, imported.stderr)
            overlapping = LibraryImportSurface(
                "rust",
                surface.package,
                (
                    *surface.capabilities,
                    LibraryCapabilityImport(
                        "fixture.protocol",
                        canonical_identity({"interface": "rust-protocol"}),
                        "fixture_library::logic",
                        ("value",),
                    ),
                ),
            )
            shared = self._run_library_driver(
                _STANDARD_LIBRARY_IMPORT_DRIVER,
                "rust",
                cargo,
                export,
                overlapping,
                test=False,
            )
            self.assertEqual(shared.returncode, 0, shared.stderr)
            self.assertEqual(
                json.loads(shared.stdout)["capabilities"],
                ["fixture.logic", "fixture.protocol"],
            )
            library.write_text("pub mod logic { pub const other: u8 = 7; }\n")
            rejected = self._run_library_driver(
                _STANDARD_LIBRARY_IMPORT_DRIVER,
                "rust",
                cargo,
                export,
                overlapping,
                test=False,
            )
            self.assertNotEqual(rejected.returncode, 0)


if __name__ == "__main__":
    unittest.main()
