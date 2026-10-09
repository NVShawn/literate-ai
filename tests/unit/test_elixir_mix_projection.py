"""Locked test-owned Mix Flavor projection and native lifecycle authority."""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from jsonschema import Draft202012Validator

from literate_ai.adapters.builders.elixir import ElixirToolchain
from literate_ai.adapters.builders.hex import HexToolchain
from literate_ai.adapters.standard_project import (
    StandardCommandProjectionError,
    project_locked_standard_toolchain_closure,
)
from literate_ai.contracts import (
    ComponentCommandPhase,
    StandardMixCommandProfile,
    parse_standard_command_profile,
)
from tests.unit.test_elixir_standard_mix import _tool
from tests.unit.test_standard_command_projection import _locked_snapshot, _observation

_REPOSITORY = Path(__file__).resolve().parents[2]


def _mix_snapshot(
    root,
    *,
    language="elixir",
    platform="macos",
    required=None,
    multiple=False,
    generation_ready=False,
    library=False,
):
    flavor = root / "fixture-mix-flavor"
    (flavor / "openspec").mkdir(parents=True)
    content = (_REPOSITORY / "flavors/build-cargo/flavor.md").read_text()
    content = (
        content.replace("cargo", "mix")
        .replace("Cargo", "Mix")
        .replace("rust", "elixir")
        .replace("Rust", "Elixir")
    )
    content = re.sub(
        r"authoring_inputs:\n.*?contributions:",
        "authoring_inputs: []\ncontributions:",
        content,
        count=1,
        flags=re.DOTALL,
    )
    content = content.replace(
        "conflicts: []",
        '  - contribution_id: "hex-constraint"\n'
        '    kind: "toolchain"\n'
        '    merge_operator: "exact-singleton"\n'
        '    slot: "hex"\n'
        "    content:\n"
        '      kind: "toolchain-constraint"\n'
        '      uri: "hex.json"\n'
        "conflicts: []",
    )
    (flavor / "flavor.md").write_text(content, encoding="utf-8", newline="\n")
    (flavor / "openspec/spec.md").write_text(
        "# Test-owned Mix profile\n\n"
        "### Requirement: Declarative intent\n\n"
        "The build SHALL use declarative Mix intent.\n\n"
        "#### Scenario: Produce native artifacts\n\n"
        "- **WHEN** the authorized Mix build runs\n"
        "- **THEN** it retains native artifacts\n",
        encoding="utf-8",
        newline="\n",
    )
    (flavor / "standard-command-profile.json").write_text(
        json.dumps(
            StandardMixCommandProfile("mix", "hex", "source/mix-project.json").to_dict()
        ),
        encoding="utf-8",
    )
    constraint = {
        "schema": "urn:literate-ai:schema:v2:toolchain-constraint",
        "toolchain": "hex",
        "minimum_version": [2, 5],
        "maximum_exclusive_version": [3],
    }
    if required is not None:
        constraint["required_version"] = required
    (flavor / "hex.json").write_text(json.dumps(constraint), encoding="utf-8")
    return _locked_snapshot(
        root / "project",
        language=language,
        platform=platform,
        build_system="mix",
        test_build_flavor_root=flavor,
        duplicate_entrypoint=multiple,
        generation_ready=generation_ready,
        no_entrypoint=library,
    )


class MixProfileTests(unittest.TestCase):
    def test_locked_mix_library_projects_compiled_import_verification(self):
        import sys

        from literate_ai.adapters.builders.python import discover_python_toolchain

        python = discover_python_toolchain(pinned_command=sys.executable)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, snapshot, execution = _mix_snapshot(root, library=True)
            tool = _tool(root)
            with (
                mock.patch.object(HexToolchain, "require_unchanged"),
                mock.patch.object(ElixirToolchain, "require_unchanged"),
                mock.patch(
                    "literate_ai.adapters.standard_project.discover_mix_toolchain",
                    return_value=tool.mix,
                ),
                mock.patch(
                    "literate_ai.adapters.standard_project.discover_hex_toolchain",
                    return_value=tool,
                ),
            ):
                closure = project_locked_standard_toolchain_closure(
                    snapshot,
                    execution,
                    host_platform="macos",
                    environment={"LITAI_HEX_EBIN": str(root / "hex")},
                    toolchain_discoverer=lambda name, *args: (
                        python if name == "python" else tool.mix.elixir
                    ),
                    dependency_observer=_observation,
                )
            contract = closure.contracts[0]
            self.assertTrue(contract.is_library)
            self.assertEqual(
                contract.command(ComponentCommandPhase.EXECUTE).argv[3], "elixir-mix"
            )
            self.assertEqual(len(closure.mix_targets), 1)
            from literate_ai.adapters.lifecycle.standard_local import (
                LocalSourceTreeRegistry,
            )
            from literate_ai.adapters.lifecycle.standard_mix import (
                StandardMixLifecyclePorts,
            )

            arguments = {
                "source_trees": LocalSourceTreeRegistry(),
                "object_root": root / "objects",
                "contracts": closure.contracts,
                "tool_bindings": closure.tool_bindings,
                "mix_targets": closure.mix_targets,
            }
            ports = StandardMixLifecyclePorts(**arguments)
            self.assertEqual(
                ports._contract(contract.component_revision).language_runtime_identity,
                contract.tool_binding(ComponentCommandPhase.EXECUTE).toolchain_identity,
            )
            with self.assertRaisesRegex(ValueError, "verification driver"):
                StandardMixLifecyclePorts(
                    **{
                        **arguments,
                        "contracts": (
                            replace(
                                contract,
                                language_runtime_identity=contract.language_compiler_identity,
                            ),
                        ),
                    }
                )

    def test_schema_and_contract_reject_native_manifests_and_extra_authority(self):
        schema = json.loads(
            (
                _REPOSITORY / "schemas/v2/standard-command-profiles.schema.json"
            ).read_text()
        )
        validator = Draft202012Validator(schema)
        valid = StandardMixCommandProfile(
            "mix", "hex", "source/mix-project.json"
        ).to_dict()
        self.assertTrue(validator.is_valid(valid))
        self.assertEqual(parse_standard_command_profile(valid).to_dict(), valid)
        for changes in (
            {"manifest": "source/mix.exs"},
            {"manifest": "source/../mix-project.json"},
            {"toolchain": "mix"},
            {"lockfile": "source/mix.lock"},
            {"target": "cargo"},
        ):
            invalid = {**valid, **changes}
            with self.subTest(changes=changes):
                self.assertFalse(validator.is_valid(invalid))
                with self.assertRaises(ValueError):
                    parse_standard_command_profile(invalid)

    def test_locked_profile_binds_hex_and_keeps_tree_runtime_on_all_platforms(self):
        for platform in ("macos", "linux", "windows"):
            with (
                self.subTest(platform=platform),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                _, snapshot, execution = _mix_snapshot(
                    root, platform=platform, required=[2, 5]
                )
                tool = _tool(root)
                with (
                    mock.patch.object(HexToolchain, "require_unchanged"),
                    mock.patch.object(ElixirToolchain, "require_unchanged"),
                    mock.patch(
                        "literate_ai.adapters.standard_project.discover_mix_toolchain",
                        return_value=tool.mix,
                    ),
                    mock.patch(
                        "literate_ai.adapters.standard_project.discover_hex_toolchain",
                        return_value=tool,
                    ),
                ):
                    closure = project_locked_standard_toolchain_closure(
                        snapshot,
                        execution,
                        host_platform=platform,
                        environment={"LITAI_HEX_EBIN": str(root / "hex")},
                        toolchain_discoverer=lambda *args, selected=tool: (
                            selected.mix.elixir
                        ),
                        dependency_observer=_observation,
                    )
                    closure.require_unchanged()
                self.assertEqual(len(closure.mix_targets), 1)
                self.assertEqual(closure.mix_targets[0].hex_toolchain, tool)
                contract = closure.contracts[0]
                self.assertEqual(
                    contract.command(ComponentCommandPhase.BUILD).argv[:2],
                    ("{tool}", "compile"),
                )
                runtime = contract.command(ComponentCommandPhase.EXECUTE).argv
                self.assertEqual(runtime[-3:-1], ("tree", "source/main.exs"))
                self.assertEqual(
                    contract.build_system_toolchain_identity,
                    closure.mix_targets[0].build_system_toolchain_identity,
                )

    def test_explicit_plugin_is_mandatory_and_discovery_never_runs_project(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, snapshot, execution = _mix_snapshot(root)
            tool = _tool(root)
            with mock.patch(
                "literate_ai.adapters.standard_project.discover_hex_toolchain"
            ) as discover:
                with self.assertRaisesRegex(
                    StandardCommandProjectionError, "explicitly staged"
                ):
                    project_locked_standard_toolchain_closure(
                        snapshot,
                        execution,
                        host_platform="macos",
                        environment={},
                        toolchain_discoverer=lambda *args, selected=tool: (
                            selected.mix.elixir
                        ),
                        dependency_observer=_observation,
                    )
                discover.assert_not_called()

    def test_wrong_language_is_rejected_by_flavor_co_requisite(self):
        from literate_ai.adapters.component_lock_planning import (
            ComponentLockPlanningError,
        )

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ComponentLockPlanningError):
                _mix_snapshot(Path(directory), language="python")

    def test_wrong_plugin_version_and_multiple_entrypoints_fail(self):
        for options in ({"required": [2, 6]}, {"multiple": True}):
            with (
                self.subTest(options=options),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                _, snapshot, execution = _mix_snapshot(root, **options)
                tool = _tool(root)
                with (
                    mock.patch.object(HexToolchain, "require_unchanged"),
                    mock.patch.object(ElixirToolchain, "require_unchanged"),
                    mock.patch(
                        "literate_ai.adapters.standard_project.discover_mix_toolchain",
                        return_value=tool.mix,
                    ),
                    mock.patch(
                        "literate_ai.adapters.standard_project.discover_hex_toolchain",
                        return_value=tool,
                    ),
                ):
                    with self.assertRaises(StandardCommandProjectionError):
                        project_locked_standard_toolchain_closure(
                            snapshot,
                            execution,
                            host_platform="macos",
                            environment={"LITAI_HEX_EBIN": str(root / "hex")},
                            toolchain_discoverer=lambda *args, selected=tool: (
                                selected.mix.elixir
                            ),
                            dependency_observer=_observation,
                        )


if __name__ == "__main__":
    unittest.main()
