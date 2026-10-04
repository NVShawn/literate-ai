"""Real locked command projection with a native SDK and explicit consumer fixtures."""

import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from literate_ai.adapters.native_sdk_commands import project_native_sdk_commands
from literate_ai.adapters.native_sdk_consumer import NativeSdkConsumerInputs
from literate_ai.adapters.native_sdk_execution import prepare_native_sdk_execution
from literate_ai.adapters.native_sdk_generation import native_sdk_generation_identities
from literate_ai.adapters.standard_project import (
    StandardCommandProjectionError,
    project_locked_standard_toolchain_closure,
)
from literate_ai.application.component_execution_planning import (
    plan_component_execution,
)
from literate_ai.contracts.authoring_markdown import (
    parse_authoring_markdown,
    render_authoring_markdown,
)
from literate_ai.contracts.executable_components import ComponentCommandPhase
from literate_ai.contracts.identity import canonical_identity
from tests.support import fixtures_test_native_sdk_source_build as test_native_sdk_source_build
from tests.support.fixtures_test_standard_command_projection import (
    _locked_snapshot,
    _observation,
    _tool,
)


def library_recipe(fixture):
    path = fixture.component / "component.md"
    document, body = parse_authoring_markdown(path.read_bytes(), source=str(path))
    document["kind"] = "library"
    document["entrypoints"] = []
    document["provides"][0]["interface"] = {"uri": "integration.md", "pin": None}
    path.write_bytes(render_authoring_markdown(document, body))


class NativeSdkCommandTests(unittest.TestCase):
    def test_every_projected_entrypoint_retains_its_runtime_path_and_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, snapshot, execution = _locked_snapshot(
                Path(temporary), duplicate_entrypoint=True
            )
            closure = project_locked_standard_toolchain_closure(
                snapshot,
                execution,
                host_platform="macos",
                toolchain_discoverer=lambda name, *_: _tool(name),
                dependency_observer=_observation,
            )
            (original,) = closure.contracts
            target = canonical_identity("exact SDK consumer target")
            projected = project_native_sdk_commands(original, target_identity=target)
            self.assertEqual(
                projected.command(ComponentCommandPhase.BUILD),
                original.command(ComponentCommandPhase.BUILD),
            )
            self.assertEqual(len(projected.entrypoint_contracts), 2)
            for before, after in zip(
                original.entrypoint_contracts,
                projected.entrypoint_contracts,
                strict=True,
            ):
                self.assertEqual(after.artifact_export.target_identity, target)
                self.assertEqual(
                    after.entrypoint_identity, after.artifact_export.identity
                )
                self.assertEqual(after.deployment_unit, before.deployment_unit)
                for phase in (
                    ComponentCommandPhase.TEST,
                    ComponentCommandPhase.EXECUTE,
                ):
                    self.assertEqual(
                        after.command(phase).argv[6:], before.command(phase).argv[3:]
                    )

    def _run(self, library):
        fixture = test_native_sdk_source_build.NativeSdkSourceBuildTests()
        self.addCleanup(fixture.doCleanups)
        if library:
            fixture.configure_recipe_fixture = library_recipe
        fixture.setUp()
        service = fixture.service()
        service.build()
        inputs = NativeSdkConsumerInputs(snapshot=fixture.snapshot, services=(service,))
        snapshot = fixture.snapshot
        lock = snapshot.authority.lock
        ids = native_sdk_generation_identities(inputs, snapshot)
        execution = plan_component_execution(
            lock,
            model_identities={
                node.revision.identity.uri: canonical_identity("model")
                for node in lock.nodes
            },
            native_sdk_input_identities=ids,
        )
        with self.assertRaises(StandardCommandProjectionError):
            project_locked_standard_toolchain_closure(snapshot, execution)
        stripped = replace(
            execution,
            generation_plans=tuple(
                replace(
                    plan,
                    generation_key=replace(
                        plan.generation_key, native_sdk_input_identities=()
                    ),
                )
                for plan in execution.generation_plans
            ),
        )
        with self.assertRaises(StandardCommandProjectionError):
            project_locked_standard_toolchain_closure(snapshot, stripped)
        with self.assertRaises(StandardCommandProjectionError):
            project_locked_standard_toolchain_closure(
                snapshot, stripped, native_sdk_inputs=inputs
            )
        closure = project_locked_standard_toolchain_closure(
            snapshot,
            execution,
            native_sdk_inputs=inputs,
            dependency_observer=_observation,
        )
        (contract,) = closure.contracts
        self.assertEqual(
            contract.artifact_export.target_identity, fixture.selection.target_identity
        )
        for phase in (ComponentCommandPhase.TEST, ComponentCommandPhase.EXECUTE):
            self.assertIn("{native_sdk_inputs}", contract.command(phase).argv)
            self.assertEqual(
                contract.command(phase).argv[:4], ("{tool}", "-I", "-B", "-c")
            )
        root = fixture.fixture.root / "consumer"
        source = root / "source-tree"
        (source / "source").mkdir(parents=True)
        if library:
            (capability,) = contract.library_import_surface.capabilities
            module_path = source / "source" / Path(*capability.module.split("."))
            module_path.mkdir(parents=True)
            module_path.joinpath("__init__.py").write_text(
                "import vendor_math\n"
                + "\n".join(
                    f"def {symbol}(value, factor):\n"
                    "    return vendor_math.scale(value, factor)"
                    for symbol in capability.symbols
                )
                + "\n"
            )
            program = (
                f"from {capability.module} import {capability.symbols[0]} as scale\n"
            )
        else:
            program = "from vendor_math import scale\n"
        (source / "source/main.py").write_text(
            program + "import json\nvalue=scale(1.5,3)\nassert value==4.5\n"
            "print(json.dumps(dict(value=value)))\n"
        )
        artifact = root / "artifact"
        artifact.mkdir()
        export = artifact / contract.artifact_export.export_id
        replacements = {
            "{tool}": sys.executable,
            "{source_root}": str(source),
            "{object_root}": str(root / "objects"),
            "{artifact_root}": str(artifact),
            "{export_path}": str(export),
            "{provider_artifacts}": "[]",
        }

        def command(phase):
            return [
                replacements.get(value, value) for value in contract.command(phase).argv
            ]

        subprocess.run(
            command(ComponentCommandPhase.BUILD), check=True, capture_output=True
        )
        with prepare_native_sdk_execution(
            inputs,
            lock.root_revision,
            target=fixture.selection.target_identity,
            parent=root,
        ) as materialized:
            replacements["{native_sdk_inputs}"] = str(materialized.manifest)
            tested = subprocess.run(
                command(ComponentCommandPhase.TEST), check=True, capture_output=True
            )
            self.assertEqual(json.loads(tested.stdout), {"value": 4.5})
            executed = subprocess.run(
                command(ComponentCommandPhase.EXECUTE), check=True, capture_output=True
            )
            observed = json.loads(executed.stdout)
            if library:
                self.assertEqual(observed["capabilities"], [capability.capability])
                module = (
                    export
                    / "source"
                    / Path(*capability.module.split("."))
                    / "__init__.py"
                )
                module.write_text("# Locked public symbol deliberately absent.\n")
                refused = subprocess.run(
                    command(ComponentCommandPhase.EXECUTE), capture_output=True
                )
                self.assertNotEqual(refused.returncode, 0)
            else:
                self.assertEqual(observed, {"value": 4.5})
            materialized.manifest.write_bytes(materialized.manifest.read_bytes() + b" ")
            refused = subprocess.run(
                command(ComponentCommandPhase.TEST), capture_output=True
            )
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn(
                b"native SDK runtime input verification failed", refused.stderr
            )
            self.assertNotIn(b'"value": 4.5', refused.stdout)

    def test_projected_application_commands_use_verified_native_imports(self):
        self._run(False)

    def test_projected_library_commands_test_and_verify_native_imports(self):
        self._run(True)
