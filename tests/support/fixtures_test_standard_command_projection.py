"""Shared test fixtures extracted from test_standard_command_projection."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from jsonschema import Draft202012Validator

from literate_ai.adapters.component_acceptance import (
    DeclaredLibraryAcceptanceCase,
    LibraryAcceptance,
)
from literate_ai.adapters.component_lock_planning import FilesystemComponentLockPlanner
from literate_ai.adapters.component_locks import ComponentLockStore
from literate_ai.adapters.component_resolution_audits import (
    ComponentResolutionAuditStore,
)
from literate_ai.adapters.lifecycle import (
    LocalSourceTreeRegistry,
    LocalStandardLifecyclePorts,
    local_tree_identity,
)
from literate_ai.adapters.locked_generation_authority import (
    FilesystemLockedGenerationAuthorityReader,
    LockedGenerationAuthorityReaderError,
)
from literate_ai.adapters.standard_project import (
    _STANDARD_LIBRARY_IMPORT_DRIVER,
    _STANDARD_LIBRARY_TEST_DRIVER,
    StandardCommandProjectionError,
    _encoded_library_import_surface,
    assemble_filesystem_standard_project_runtime,
    project_locked_standard_toolchain_closure,
)
from literate_ai.application.component_execution_planning import (
    plan_component_execution,
)
from literate_ai.application.component_lock_resolution import ComponentLockResolver
from literate_ai.application.library_artifacts import project_library_import_surface
from literate_ai.contracts import (
    ComponentCommandPhase,
    ContentIdentity,
    ContractValidationError,
    LibraryCapabilityImport,
    LibraryImportSurface,
    StandardPythonWheelCommandProfile,
    StandardRepoManCommandProfile,
    canonical_identity,
    parse_standard_command_profile,
)
from tests.support.fixtures_test_component_lock_planning import _fixture
from tests.support.fixtures_test_schema_catalog import SchemaCatalog


def _tool(name: str, *, environment: tuple[tuple[str, str], ...] = ()):
    return SimpleNamespace(
        command=(str(Path(sys.executable).resolve(strict=True)),),
        environment=environment,
        identity=canonical_identity({"fake-standard-toolchain": name}).uri,
        require_unchanged=lambda: None,
    )


def _npm_tool(node, *, selected_node=None):
    return SimpleNamespace(
        node=node if selected_node is None else selected_node,
        command=(str(Path(sys.executable).resolve(strict=True)), "npm-cli.js"),
        identity=canonical_identity(
            {
                "fake-standard-toolchain": "npm",
                "node": node.identity,
            }
        ).uri,
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
    def test_python_wheels_bind_profile_interpreter_and_closure_on_each_os(self):
        for platform in ("linux", "windows", "macos"):
            with (
                self.subTest(platform=platform),
                tempfile.TemporaryDirectory() as temporary,
            ):
                _component, snapshot, execution = _locked_snapshot(
                    Path(temporary), platform=platform, package_python=True
                )
                closure = project_locked_standard_toolchain_closure(
                    snapshot,
                    execution,
                    host_platform=platform,
                    toolchain_discoverer=lambda name, *_: _tool(name),
                    dependency_observer=_observation,
                )
                closure.require_unchanged()
                self.assertEqual(len(closure.python_targets), 1)
                target = closure.python_targets[0]
                contract = closure.contracts[0]
                self.assertEqual(target.manifest, "source/requirements.txt")
                self.assertEqual(target.lockfile, "source/python-wheel-lock.json")
                self.assertEqual(
                    contract.locked_build_authority_identity, target.identity
                )
                self.assertEqual(
                    contract.language_runtime_identity, target.python_toolchain_identity
                )
                self.assertEqual(target.python_command, _tool("python").command)
                self.assertEqual(closure.npm_targets, ())
                self.assertEqual(
                    contract.command(ComponentCommandPhase.BUILD).argv[:5],
                    ("{tool}", "-I", "-S", "-B", "-c"),
                )
                for phase in (
                    ComponentCommandPhase.TEST,
                    ComponentCommandPhase.EXECUTE,
                ):
                    self.assertEqual(
                        contract.command(phase).argv[:5],
                        ("{tool}", "-I", "-S", "-B", "-c"),
                    )
                with self.assertRaises(ValueError):
                    replace(closure, python_targets=()).require_unchanged()
                object_root = Path(temporary) / "must-not-create"
                with self.assertRaises(StandardCommandProjectionError) as caught:
                    assemble_filesystem_standard_project_runtime(
                        generator=mock.Mock(),
                        object_root=object_root,
                        toolchain_closure=closure,
                    )
                self.assertEqual(
                    caught.exception.code,
                    "standard_command.python_wheelhouse_missing",
                )
                self.assertFalse(object_root.exists())

    def test_python_wheel_conflicts_fail_before_tool_discovery(self):
        for options in (
            {"build_system": "make"},
            {"no_entrypoint": True},
            {"duplicate_entrypoint": True},
        ):
            with (
                self.subTest(options=options),
                tempfile.TemporaryDirectory() as temporary,
            ):
                _component, snapshot, execution = _locked_snapshot(
                    Path(temporary), package_python=True, **options
                )
                discover = mock.Mock(
                    side_effect=AssertionError("must not discover tools")
                )
                with self.assertRaises(StandardCommandProjectionError) as caught:
                    project_locked_standard_toolchain_closure(
                        snapshot,
                        execution,
                        host_platform="macos",
                        toolchain_discoverer=discover,
                        dependency_observer=_observation,
                    )
                self.assertEqual(
                    caught.exception.code,
                    "standard_command.python_wheel_profile_unsupported",
                )
                discover.assert_not_called()

    def test_python_wheel_profile_roundtrip_and_path_validation(self):
        catalog = SchemaCatalog()
        for name in ("requirements.txt", "requirements.in", "pyproject.toml"):
            profile = StandardPythonWheelCommandProfile(
                "pip", "python", "source/" + name, "source/python-wheel-lock.json"
            )
            catalog.validate(profile.SCHEMA, profile.to_dict())
            self.assertEqual(parse_standard_command_profile(profile.to_dict()), profile)
            for change in (
                {"target": "npm"},
                {"toolchain": "pip"},
                {"manifest": "../requirements.txt"},
                {"manifest": "setup.py"},
                {"lockfile": "source/uv.lock"},
            ):
                with (
                    self.subTest(change=change),
                    self.assertRaises(ContractValidationError),
                ):
                    parse_standard_command_profile({**profile.to_dict(), **change})
            with self.assertRaises(ContractValidationError):
                replace(profile, lockfile="other/python-wheel-lock.json")

    def test_rust_library_capability_projects_reviewed_module_and_symbol(self) -> None:
        interface = canonical_identity({"interface": "trickle-config"})
        surface = project_library_import_surface(
            "trickle-common",
            "rust",
            (
                ("trickle-common.config.type-trickle-config", interface),
                ("trickle-common.logging.fn-init-logging", interface),
            ),
        )

        self.assertEqual(surface.package, "trickle_common")
        self.assertEqual(surface.capabilities[0].module, "trickle_common::config")
        self.assertEqual(surface.capabilities[0].symbols, ("TrickleConfig",))
        self.assertEqual(surface.capabilities[1].module, "trickle_common::logging")
        self.assertEqual(surface.capabilities[1].symbols, ("init_logging",))

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

    def test_cuda_profile_selects_nvcc_only_for_cpp(self) -> None:
        for language, expected in (("cpp", "nvcc"), ("python", "python")):
            with (
                self.subTest(language=language),
                tempfile.TemporaryDirectory() as temporary,
            ):
                _component, snapshot, execution = _locked_snapshot(
                    Path(temporary),
                    build_system="make",
                    language=language,
                    accelerator="accel-nvidia-cuda",
                )
                discovered = []

                def discover(name, _constraint, _environment, *, seen=discovered):
                    seen.append(name)
                    return _tool(name)

                project_locked_standard_toolchain_closure(
                    snapshot,
                    execution,
                    host_platform="macos",
                    toolchain_discoverer=discover,
                    dependency_observer=_observation,
                )
                self.assertIn(expected, discovered)
                self.assertEqual("nvcc" in discovered, language == "cpp")

    def test_selected_language_and_platform_profiles_derive_native_commands(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _component, snapshot, execution = _locked_snapshot(Path(temporary))
            closure = project_locked_standard_toolchain_closure(
                snapshot,
                execution,
                host_platform="macos",
                toolchain_discoverer=lambda name, _constraint, _environment: _tool(
                    name
                ),
                dependency_observer=_observation,
            )

        self.assertEqual(len(closure.contracts), 1)
        self.assertEqual(closure.bazel_targets, ())
        contract = closure.contracts[0]
        self.assertEqual(
            tuple(item.phase for item in contract.commands),
            tuple(ComponentCommandPhase),
        )
        self.assertIn("python-tree", contract.command(ComponentCommandPhase.BUILD).argv)
        self.assertIn("--litai-test", contract.command(ComponentCommandPhase.TEST).argv)
        self.assertIn(
            "--litai-smoke", contract.command(ComponentCommandPhase.EXECUTE).argv
        )
        self.assertEqual(
            closure.record.component_authorities[0].flavor_selection_identity,
            execution.generation_plans[0].generation_key.flavor_selection_identity,
        )
        closure.require_unchanged()

    def test_library_only_standard_commands_project_exact_import_surface(self) -> None:
        for language, build_system in (
            ("python", None),
            ("javascript", None),
            ("rust", "cargo"),
        ):
            with (
                self.subTest(language=language),
                tempfile.TemporaryDirectory() as temporary,
            ):
                _component, snapshot, execution = _locked_snapshot(
                    Path(temporary),
                    language=language,
                    build_system=build_system,
                    no_entrypoint=True,
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
                self.assertTrue(contract.is_library)
                self.assertEqual(contract.artifact_export.role, "library")
                self.assertEqual(contract.library_import_surface.language, language)
                self.assertEqual(
                    contract.library_import_surface.capabilities[0].capability,
                    "sample.portable-app",
                )
                self.assertEqual(
                    contract.artifact_export_shapes(), (contract.artifact_export,)
                )
                self.assertNotIn(
                    "--litai-smoke",
                    contract.command(ComponentCommandPhase.EXECUTE).argv,
                )
                if language == "rust":
                    self.assertIn(
                        "--lib", contract.command(ComponentCommandPhase.BUILD).argv
                    )
                    self.assertIn(
                        _STANDARD_LIBRARY_TEST_DRIVER,
                        contract.command(ComponentCommandPhase.TEST).argv,
                    )

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
                f"{capability.symbols[0]} = 7\n", encoding="utf-8"
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

    def test_python_library_runs_verifier_owned_harness_against_sealed_export(
        self,
    ) -> None:
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
            custody_root = root / "sealed-package"
            export = custody_root / contract.artifact_export.export_id
            module = export / "source" / Path(*capability.module.split("."))
            module.parent.mkdir(parents=True)
            for parent in module.parents:
                if parent == export / "source":
                    break
                parent.joinpath("__init__.py").touch()
            module.with_suffix(".py").write_text(
                f"def {capability.symbols[0]}(left, right):\n    return left + right\n",
                encoding="utf-8",
            )
            harness = (
                b"import importlib,json,pathlib,sys\n"
                b"artifact,surface,cases=sys.argv[1],json.loads(sys.argv[2]),"
                b"json.loads(sys.argv[3])\n"
                b"sys.path[:]=[str(pathlib.Path(artifact)/'source')]\n"
                b"capabilities={item['capability']:item for item in "
                b"surface['capabilities']}\n"
                b"results=[]\n"
                b"for case in cases:\n"
                b" item=capabilities[case['capability']]\n"
                b" value=getattr(importlib.import_module(item['module']),"
                b"item['symbols'][0])(*case['arguments'])\n"
                b" results.append({'case_id':case['case_id'],'capability':"
                b"case['capability'],'result':value})\n"
                b"print(json.dumps({'schema':'literate-ai/library-acceptance-results@1',"
                b"'cases':results}))\n"
            )
            root_node = next(
                item
                for item in snapshot.authority.lock.nodes
                if item.revision.identity == snapshot.authority.lock.root_revision
            )
            oracle = LibraryAcceptance(
                root_node.revision.coordinate.name,
                root_node.revision.specification_set_identity,
                tuple(
                    sorted(
                        (
                            item.identity
                            for item in root_node.revision.public_interfaces
                        ),
                        key=lambda item: item.uri,
                    )
                ),
                surface.identity,
                "python",
                ContentIdentity.parse_uri(
                    "sha256:" + hashlib.sha256(harness).hexdigest()
                ),
                harness,
                (
                    DeclaredLibraryAcceptanceCase(
                        "adds-values", capability.capability, [2, 3], 5
                    ),
                ),
            )
            ports = LocalStandardLifecyclePorts(
                source_trees=LocalSourceTreeRegistry(),
                object_root=root / "objects",
                contracts=closure.contracts,
                tool_bindings=closure.tool_bindings,
                independent_acceptance_oracle=oracle,
            )
            artifact_identity = canonical_identity({"sealed-library": True})
            ports._planned_exports[snapshot.authority.lock.root_revision.uri] = (
                SimpleNamespace(identity=artifact_identity)
            )
            custody = SimpleNamespace(
                root=custody_root,
                artifact_paths={artifact_identity.uri: export},
                tree_identity=local_tree_identity(custody_root),
            )
            package_plan = SimpleNamespace(
                identity=canonical_identity({"library-package-plan": True}),
                entrypoints=(),
            )
            package_result = SimpleNamespace(
                identity=canonical_identity({"library-package-result": True})
            )
            with mock.patch.object(
                ports, "project_package_custody", return_value=custody
            ):
                evidence = ports.accept_project_independently(
                    snapshot.authority.lock,
                    execution,
                    None,
                    package_plan,
                    package_result,
                    canonical_identity({"root-test": True}),
                    canonical_identity({"packaged-execution": True}),
                )
            self.assertTrue(evidence.uri.startswith("sha256:"))

    def test_javascript_library_verifier_rejects_unlocked_export_map(self) -> None:
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
                        "exports": {
                            "./logic": "./logic.js",
                            "./ambient": "./ambient.js",
                        },
                    }
                ),
                encoding="utf-8",
            )
            rejected = self._run_library_driver(
                _STANDARD_LIBRARY_IMPORT_DRIVER,
                "javascript",
                node,
                export,
                surface,
                test=False,
            )
            self.assertNotEqual(rejected.returncode, 0)

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
            library.write_text("pub mod logic { pub const other: u8 = 7; }\n")
            rejected = self._run_library_driver(
                _STANDARD_LIBRARY_IMPORT_DRIVER,
                "rust",
                cargo,
                export,
                surface,
                test=False,
            )
            self.assertNotEqual(rejected.returncode, 0)

    def test_npm_library_fails_before_node_or_npm_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _component, snapshot, execution = _locked_snapshot(
                Path(temporary),
                language="javascript",
                package_npm=True,
                no_entrypoint=True,
            )
            discover = mock.Mock()
            discover_npm = mock.Mock()
            with self.assertRaises(StandardCommandProjectionError) as raised:
                project_locked_standard_toolchain_closure(
                    snapshot,
                    execution,
                    host_platform="macos",
                    toolchain_discoverer=discover,
                    npm_toolchain_discoverer=discover_npm,
                    dependency_observer=_observation,
                )

        self.assertEqual(
            raised.exception.code,
            "standard_command.library_npm_unsupported",
        )
        self.assertIn("dependency-free", str(raised.exception))
        discover.assert_not_called()
        discover_npm.assert_not_called()

    def test_multiple_entrypoints_project_per_entrypoint_commands(self) -> None:
        """ADR 0026: a multi-entrypoint Component builds once and fans out."""

        with tempfile.TemporaryDirectory() as temporary:
            _component, snapshot, execution = _locked_snapshot(
                Path(temporary), duplicate_entrypoint=True
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

        self.assertEqual(len(closure.contracts), 1)
        contract = closure.contracts[0]
        # One per-Component BUILD command is unchanged.
        self.assertEqual(
            tuple(item.phase for item in contract.commands),
            tuple(ComponentCommandPhase),
        )
        # The additive per-entrypoint map is present and covers both surfaces.
        self.assertTrue(contract.is_multi_entrypoint)
        self.assertIsNotNone(contract.entrypoint_contracts)
        self.assertEqual(len(contract.entrypoint_contracts), 2)
        # Each entrypoint has its own TEST + EXECUTE and its own export shape,
        # keyed by a distinct entrypoint identity and its deployment unit.
        export_ids = {
            item.artifact_export.export_id for item in contract.entrypoint_contracts
        }
        self.assertEqual(len(export_ids), 2)
        deployment_units = {
            item.deployment_unit for item in contract.entrypoint_contracts
        }
        self.assertEqual(deployment_units, {"run", "run-again"})
        for entry in contract.entrypoint_contracts:
            self.assertEqual(
                tuple(item.phase for item in entry.commands),
                (ComponentCommandPhase.TEST, ComponentCommandPhase.EXECUTE),
            )
            self.assertIn(
                "--litai-test",
                entry.command(ComponentCommandPhase.TEST).argv,
            )
            self.assertIn(
                "--litai-smoke",
                entry.command(ComponentCommandPhase.EXECUTE).argv,
            )
        # The second entrypoint's runtime argv names its own module path.
        second = contract.entrypoint_command_contract(
            contract.entrypoint_contracts[1].entrypoint_identity
        )
        self.assertIn(
            "source/run-again.py",
            second.command(ComponentCommandPhase.EXECUTE).argv,
        )
        build_argv = contract.command(ComponentCommandPhase.BUILD).argv
        self.assertEqual(build_argv[:2], ("{tool}", "-c"))
        self.assertIn("<literate-ai-multi-entrypoint-build>", build_argv[2])
        self.assertNotIn("literate_ai.adapters", build_argv)
        closure.require_unchanged()

    def test_multiple_entrypoints_fail_closed_for_singular_build_profiles(self) -> None:
        for build_system, language in (
            ("bazel", "python"),
            ("make", "python"),
            ("cmake", "python"),
            ("cargo", "rust"),
        ):
            with (
                self.subTest(build_system=build_system),
                tempfile.TemporaryDirectory() as temporary,
            ):
                _component, snapshot, execution = _locked_snapshot(
                    Path(temporary),
                    build_system=build_system,
                    language=language,
                    duplicate_entrypoint=True,
                )
                with self.assertRaises(StandardCommandProjectionError) as raised:
                    project_locked_standard_toolchain_closure(
                        snapshot,
                        execution,
                        host_platform="macos",
                        toolchain_discoverer=(
                            lambda name, _constraint, _environment: _tool(name)
                        ),
                        dependency_observer=_observation,
                    )
                self.assertEqual(
                    raised.exception.code,
                    "standard_command.multi_entrypoint_build_profile_unsupported",
                )

    def test_each_supported_language_derives_its_exact_build_strategy(self) -> None:
        expected = {
            "cpp": "cpp-executable",
            "javascript": "javascript-tree",
            "python": "python-tree",
            "rust": "rust-executable",
        }
        for language, strategy in expected.items():
            with (
                self.subTest(language=language),
                tempfile.TemporaryDirectory() as temporary,
            ):
                _component, snapshot, execution = _locked_snapshot(
                    Path(temporary), language=language
                )
                closure = project_locked_standard_toolchain_closure(
                    snapshot,
                    execution,
                    host_platform="macos",
                    toolchain_discoverer=lambda name, _constraint, _environment: _tool(
                        name
                    ),
                    dependency_observer=_observation,
                )
                build = closure.contracts[0].command(ComponentCommandPhase.BUILD)
                self.assertIn(strategy, build.argv)
                self.assertIn(
                    "--litai-test",
                    closure.contracts[0].command(ComponentCommandPhase.TEST).argv,
                )

    def test_windows_native_export_has_the_locked_executable_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _component, snapshot, execution = _locked_snapshot(
                Path(temporary), language="rust", platform="windows"
            )
            closure = project_locked_standard_toolchain_closure(
                snapshot,
                execution,
                host_platform="windows",
                toolchain_discoverer=lambda name, _constraint, _environment: _tool(
                    name
                ),
                dependency_observer=_observation,
            )

        contract = closure.contracts[0]
        self.assertTrue(contract.artifact_export.export_id.endswith(".exe"))
        self.assertIn(
            "{export_path}", contract.command(ComponentCommandPhase.BUILD).argv
        )

    def test_native_build_driver_carries_the_compiler_environment(self) -> None:
        captured_environment = (
            (
                "INCLUDE",
                ";".join(f"C:/SDK/{index:04d}/include" for index in range(400)),
            ),
            ("LIB", ";".join(f"C:/SDK/{index:04d}/lib" for index in range(400))),
        )
        uncompressed = json.dumps(list(captured_environment), separators=(",", ":"))
        self.assertGreater(len(uncompressed), 4096)
        with tempfile.TemporaryDirectory() as temporary:
            _component, snapshot, execution = _locked_snapshot(
                Path(temporary), language="cpp", platform="windows"
            )

            def discover(name, _constraint, _environment):
                return _tool(
                    name,
                    environment=captured_environment if name == "cpp" else (),
                )

            closure = project_locked_standard_toolchain_closure(
                snapshot,
                execution,
                host_platform="windows",
                toolchain_discoverer=discover,
                dependency_observer=_observation,
            )

        build = closure.contracts[0].command(ComponentCommandPhase.BUILD)
        encoded = build.argv[5]
        self.assertLessEqual(len(encoded), 4096)
        self.assertEqual(
            json.loads(zlib.decompress(base64.urlsafe_b64decode(encoded))),
            [list(item) for item in captured_environment],
        )

    def test_package_npm_projects_exact_node_bound_target_and_commands(self) -> None:
        discovered: list[str] = []

        def discover(name, _constraint, _environment):
            discovered.append(name)
            return _tool(name)

        npm_nodes = []

        def discover_npm(node, _environment):
            npm_nodes.append(node)
            return _npm_tool(node)

        with tempfile.TemporaryDirectory() as temporary:
            _component, snapshot, execution = _locked_snapshot(
                Path(temporary), language="javascript", package_npm=True
            )
            closure = project_locked_standard_toolchain_closure(
                snapshot,
                execution,
                host_platform="macos",
                toolchain_discoverer=discover,
                npm_toolchain_discoverer=discover_npm,
                dependency_observer=_observation,
            )

        self.assertEqual(discovered, ["node"])
        self.assertEqual(len(npm_nodes), 1)
        self.assertEqual(
            npm_nodes[0].identity,
            canonical_identity({"fake-standard-toolchain": "node"}).uri,
        )
        self.assertEqual(closure.bazel_targets, ())
        self.assertEqual(closure.cargo_targets, ())
        self.assertEqual(len(closure.npm_targets), 1)
        target = closure.npm_targets[0]
        self.assertEqual(target.manifest, "source/package.json")
        self.assertEqual(target.lockfile, "source/package-lock.json")
        self.assertEqual(
            target.node_toolchain_identity,
            canonical_identity({"fake-standard-toolchain": "node"}),
        )
        self.assertNotEqual(
            target.packaging_flavor_revision_identity,
            target.packaging_profile_identity,
        )
        contract = closure.contracts[0]
        self.assertEqual(
            contract.command(ComponentCommandPhase.BUILD).argv,
            (
                "{tool}",
                "ci",
                "--ignore-scripts",
                "--no-audit",
                "--no-fund",
                "--no-bin-links",
                "{source_root}",
                "{object_root}",
                "{export_path}",
            ),
        )
        self.assertEqual(
            contract.tool_binding(ComponentCommandPhase.BUILD).toolchain_identity,
            target.build_system_toolchain_identity,
        )
        self.assertEqual(
            contract.tool_binding(ComponentCommandPhase.TEST).toolchain_identity,
            target.node_toolchain_identity,
        )
        self.assertEqual(contract.locked_build_authority_identity, target.identity)
        self.assertEqual(
            closure.record.component_authorities[0].build_target_identity,
            target.identity,
        )
        closure.require_unchanged()

    def test_package_npm_rejects_an_npm_cli_bound_to_another_node(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _component, snapshot, execution = _locked_snapshot(
                Path(temporary), language="javascript", package_npm=True
            )
            with self.assertRaises(StandardCommandProjectionError) as raised:
                project_locked_standard_toolchain_closure(
                    snapshot,
                    execution,
                    host_platform="macos",
                    toolchain_discoverer=(
                        lambda name, _constraint, _environment: _tool(name)
                    ),
                    npm_toolchain_discoverer=(
                        lambda node, _environment: _npm_tool(
                            node, selected_node=_tool("different-node")
                        )
                    ),
                    dependency_observer=_observation,
                )
        self.assertEqual(raised.exception.code, "standard_command.npm_node_mismatch")

    def test_package_npm_rejects_an_explicit_build_system_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _component, snapshot, execution = _locked_snapshot(
                Path(temporary),
                language="javascript",
                package_npm=True,
                build_system="bazel",
            )
            with self.assertRaises(StandardCommandProjectionError) as raised:
                project_locked_standard_toolchain_closure(
                    snapshot,
                    execution,
                    host_platform="macos",
                    toolchain_discoverer=(
                        lambda name, _constraint, _environment: _tool(name)
                    ),
                    npm_toolchain_discoverer=lambda node, _environment: _npm_tool(node),
                    dependency_observer=_observation,
                )
        self.assertEqual(
            raised.exception.code,
            "standard_command.npm_build_system_unsupported",
        )

    def test_selected_bazel_profile_derives_exact_target_without_fixture_commands(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _component, snapshot, execution = _locked_snapshot(
                Path(temporary), build_system="bazel"
            )
            closure = project_locked_standard_toolchain_closure(
                snapshot,
                execution,
                host_platform="macos",
                toolchain_discoverer=lambda name, _constraint, _environment: _tool(
                    name
                ),
                dependency_observer=_observation,
            )

        self.assertEqual(len(closure.bazel_targets), 1)
        target = closure.bazel_targets[0]
        self.assertEqual(target.target_label, "//:litai_artifact")
        self.assertEqual(target.output_path, "run.pyz")
        self.assertEqual(
            closure.contracts[0].locked_build_authority_identity, target.identity
        )
        self.assertEqual(
            closure.record.component_authorities[0].build_target_identity,
            target.identity,
        )

    def test_selected_make_profile_derives_locked_portable_make_command(self) -> None:
        discovered: list[str] = []

        def discover(name, _constraint, _environment):
            discovered.append(name)
            return _tool(name)

        with tempfile.TemporaryDirectory() as temporary:
            _component, snapshot, execution = _locked_snapshot(
                Path(temporary), build_system="make"
            )
            closure = project_locked_standard_toolchain_closure(
                snapshot,
                execution,
                host_platform="macos",
                toolchain_discoverer=discover,
                dependency_observer=_observation,
            )

        self.assertEqual(closure.bazel_targets, ())
        self.assertEqual(discovered, ["make", "python"])
        self.assertEqual(
            {item.uri for item in closure.record.toolchain_identities},
            {
                canonical_identity({"fake-standard-toolchain": "make"}).uri,
                canonical_identity({"fake-standard-toolchain": "python"}).uri,
            },
        )
        contract = closure.contracts[0]
        argv = contract.command(ComponentCommandPhase.BUILD).argv
        self.assertEqual(argv[:2], ("{tool}", "-c"))
        self.assertEqual(
            json.loads(argv[3]), [str(Path(sys.executable).resolve(strict=True))]
        )
        self.assertEqual(argv[4], str(Path(sys.executable).resolve(strict=True)))
        self.assertEqual(json.loads(argv[5]), [])
        self.assertEqual(
            argv[6:],
            (
                "{source_root}",
                "{object_root}",
                "{artifact_root}",
                "{export_path}",
                "source/Makefile",
                "all",
            ),
        )
        self.assertIn("file", contract.command(ComponentCommandPhase.TEST).argv)
        self.assertIsNone(closure.record.component_authorities[0].build_target_identity)

    def test_repo_man_profile_records_retained_authority_and_builds_disposable_source(
        self,
    ) -> None:
        discovered: list[str] = []

        def discover(name, _constraint, _environment):
            discovered.append(name)
            return _tool(name)

        with tempfile.TemporaryDirectory() as temporary:
            _component, snapshot, execution = _locked_snapshot(
                Path(temporary), build_system="repo-man"
            )
            closure = project_locked_standard_toolchain_closure(
                snapshot,
                execution,
                host_platform="macos",
                toolchain_discoverer=discover,
                dependency_observer=_observation,
            )

        self.assertEqual(discovered, ["python"])
        contract = closure.contracts[0]
        self.assertEqual(
            contract.command(ComponentCommandPhase.BUILD).argv[:2], ("{tool}", "-c")
        )
        self.assertEqual(
            contract.command(ComponentCommandPhase.BUILD).argv[3], "python-tree"
        )
        self.assertIn("tree", contract.command(ComponentCommandPhase.TEST).argv)
        self.assertIsNone(closure.record.component_authorities[0].build_target_identity)
        profile = parse_standard_command_profile(
            {
                "schema": StandardRepoManCommandProfile.SCHEMA,
                "target": "repo-man",
                "toolchain": "repo.sh",
                "entrypoint": "repo.sh",
                "build_target": "build",
            }
        )
        self.assertIsInstance(profile, StandardRepoManCommandProfile)

    def test_repo_man_profile_rejects_mismatched_target_and_driver(self) -> None:
        valid = {
            "schema": StandardRepoManCommandProfile.SCHEMA,
            "target": "repo-man",
            "toolchain": "repo.sh",
            "entrypoint": "repo.sh",
            "build_target": "build",
        }
        for changed in (
            {"target": "make"},
            {"toolchain": "make"},
            {"entrypoint": "build.sh"},
            {"entrypoint": "../repo.sh"},
        ):
            with self.subTest(changed=changed):
                with self.assertRaises(ContractValidationError):
                    parse_standard_command_profile({**valid, **changed})

    def test_make_cannot_reuse_the_bazel_target_label_profile(self) -> None:
        with self.assertRaisesRegex(ContractValidationError, "reserved for Bazel"):
            parse_standard_command_profile(
                {
                    "schema": (
                        "urn:literate-ai:schema:v2:standard-build-system-command-profile"
                    ),
                    "target": "make",
                    "toolchain": "make",
                    "target_label": "//:all",
                }
            )

    def test_javascript_bazel_closure_does_not_admit_an_unused_python_tool(
        self,
    ) -> None:
        discovered: list[str] = []

        def discover(name, _constraint, _environment):
            discovered.append(name)
            return _tool(name)

        with tempfile.TemporaryDirectory() as temporary:
            _component, snapshot, execution = _locked_snapshot(
                Path(temporary), build_system="bazel", language="javascript"
            )
            closure = project_locked_standard_toolchain_closure(
                snapshot,
                execution,
                host_platform="macos",
                toolchain_discoverer=discover,
                dependency_observer=_observation,
            )

        self.assertEqual(discovered, ["bazel", "node"])
        self.assertEqual(
            {item.uri for item in closure.record.toolchain_identities},
            {
                canonical_identity({"fake-standard-toolchain": "bazel"}).uri,
                canonical_identity({"fake-standard-toolchain": "node"}).uri,
            },
        )
        closure.require_unchanged()

    def test_selected_cargo_profile_projects_locked_external_target_build(self) -> None:
        discovered: list[str] = []

        def discover(name, _constraint, _environment):
            discovered.append(name)
            return _tool(name)

        with tempfile.TemporaryDirectory() as temporary:
            _component, snapshot, execution = _locked_snapshot(
                Path(temporary), build_system="cargo", language="rust"
            )
            closure = project_locked_standard_toolchain_closure(
                snapshot,
                execution,
                host_platform="macos",
                toolchain_discoverer=discover,
                dependency_observer=_observation,
            )

        self.assertEqual(discovered, ["cargo", "python", "rust"])
        self.assertEqual(closure.bazel_targets, ())
        self.assertEqual(len(closure.cargo_targets), 1)
        target = closure.cargo_targets[0]
        self.assertEqual(target.manifest, "source/Cargo.toml")
        self.assertEqual(target.binary, "litai_artifact")
        contract = closure.contracts[0]
        build = contract.command(ComponentCommandPhase.BUILD).argv
        self.assertEqual(build[:3], ("{tool}", "build", "--locked"))
        self.assertEqual(
            build[3:7],
            ("--manifest-path", "source/Cargo.toml", "--bin", "litai_artifact"),
        )
        self.assertIn("--litai-test", contract.command(ComponentCommandPhase.TEST).argv)
        self.assertEqual(
            contract.tool_binding(ComponentCommandPhase.BUILD).toolchain_identity,
            canonical_identity({"fake-standard-toolchain": "cargo"}),
        )

    def test_platform_and_profile_bytes_are_fail_closed_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            component, snapshot, execution = _locked_snapshot(Path(temporary))
            with self.assertRaisesRegex(
                StandardCommandProjectionError, "does not match host"
            ):
                project_locked_standard_toolchain_closure(
                    snapshot,
                    execution,
                    host_platform="windows",
                    toolchain_discoverer=lambda name, _constraint, _environment: _tool(
                        name
                    ),
                    dependency_observer=_observation,
                )
            profile = (
                component.parent / "flavors/lang-python/standard-command-profile.json"
            )
            value = json.loads(profile.read_text(encoding="utf-8"))
            value["source_entrypoint"] = "source/changed.py"
            profile.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(LockedGenerationAuthorityReaderError):
                project_locked_standard_toolchain_closure(
                    snapshot,
                    execution,
                    host_platform="macos",
                    toolchain_discoverer=lambda name, _constraint, _environment: _tool(
                        name
                    ),
                    dependency_observer=_observation,
                )

    def test_profile_contract_round_trips_through_public_schema(self) -> None:
        root = Path(__file__).resolve().parents[2]
        catalog = SchemaCatalog()
        for name in (
            "build-bazel",
            "build-cargo",
            "accel-nvidia-cuda",
            "lang-cpp",
            "lang-javascript",
            "package-npm",
            "os-linux",
            "os-macos",
            "build-make",
            "build-repo-man",
            "lang-python",
            "lang-rust",
            "os-windows",
        ):
            with self.subTest(flavor=name):
                profile_name = "standard-command-profile.json"
                value = json.loads(
                    (root / "flavors" / name / profile_name).read_text(encoding="utf-8")
                )
                profile = parse_standard_command_profile(value)
                catalog.validate(profile.SCHEMA, profile.to_dict())
                self.assertEqual(
                    parse_standard_command_profile(profile.to_dict()), profile
                )

    def test_npm_profile_schema_and_runtime_reject_path_traversal_equally(
        self,
    ) -> None:
        root = Path(__file__).resolve().parents[2]
        schema = json.loads(
            (root / "schemas/v2/standard-command-profiles.schema.json").read_text(
                encoding="utf-8"
            )
        )
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        valid = {
            "schema": "urn:literate-ai:schema:v2:standard-npm-command-profile",
            "target": "npm",
            "toolchain": "npm",
            "manifest": "source/package.json",
            "lockfile": "source/package-lock.json",
        }
        self.assertTrue(validator.is_valid(valid))
        self.assertEqual(parse_standard_command_profile(valid).to_dict(), valid)

        for prefix in ("../", "source/../", "./", "source//"):
            invalid = {
                **valid,
                "manifest": prefix + "package.json",
                "lockfile": prefix + "package-lock.json",
            }
            with self.subTest(path=invalid["manifest"]):
                self.assertFalse(validator.is_valid(invalid))
                with self.assertRaises(ContractValidationError):
                    parse_standard_command_profile(invalid)


if __name__ == "__main__":
    unittest.main()
