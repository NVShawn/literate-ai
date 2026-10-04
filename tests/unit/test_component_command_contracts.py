"""Locked provider-neutral Component command authority tests."""

from __future__ import annotations

import unittest
from dataclasses import replace

from literate_ai.contracts import (
    ComponentArtifactExportShape,
    ComponentCommandContract,
    ComponentCommandPhase,
    ComponentCommandRole,
    ComponentCommandToolBinding,
    ComponentEntrypointCommandContract,
    ComponentLifecycleCommand,
    LibraryCapabilityImport,
    LibraryConsumerBinding,
    LibraryImportSurface,
    canonical_identity,
)
from tests.support.fixtures_test_schema_catalog import SchemaCatalog

COMMAND_SCHEMA = "urn:literate-ai:schema:v2:component-command-contract"
LIFECYCLE_COMMAND_SCHEMA = "urn:literate-ai:schema:v2:component-lifecycle-command"
LIBRARY_IMPORT_SURFACE_SCHEMA = "urn:literate-ai:schema:v2:library-import-surface@1"
LIBRARY_CONSUMER_BINDING_SCHEMA = "urn:literate-ai:schema:v2:library-consumer-binding@1"


def identity(label: str):
    return canonical_identity({"component-command-test": label})


def command(phase: ComponentCommandPhase) -> ComponentLifecycleCommand:
    if phase is ComponentCommandPhase.BUILD:
        argv = (
            "{tool}",
            "build",
            "{source_root}",
            "--object-root",
            "{object_root}",
            "--output",
            "{export_path}",
            "{provider_artifacts}",
        )
    else:
        argv = ("{tool}", phase.value, "{artifact_root}")
    return ComponentLifecycleCommand(phase, argv)


def contract() -> ComponentCommandContract:
    revision = identity("revision")
    compiler = identity("compiler")
    export = ComponentArtifactExportShape(
        "sample-app",
        "executable",
        identity("abi"),
        identity("target"),
        "application/vnd.literate-ai.executable",
        identity("producer"),
    )
    return ComponentCommandContract(
        component_revision=revision,
        locked_build_authority_identity=identity("locked-flavor-build-authority"),
        build_system_resolver_identity=identity("build-system-resolver"),
        build_system_toolchain_identity=identity("bazel-toolchain"),
        language_compiler_identity=compiler,
        language_runtime_identity=identity("runtime"),
        commands=tuple(command(phase) for phase in ComponentCommandPhase),
        tool_bindings=(
            ComponentCommandToolBinding(
                ComponentCommandPhase.BUILD, identity("bazel-toolchain")
            ),
            ComponentCommandToolBinding(
                ComponentCommandPhase.TEST, identity("bazel-toolchain")
            ),
            ComponentCommandToolBinding(
                ComponentCommandPhase.EXECUTE, identity("runtime")
            ),
        ),
        artifact_export=export,
    )


def entrypoint_command(phase: ComponentCommandPhase) -> ComponentLifecycleCommand:
    return ComponentLifecycleCommand(phase, ("{tool}", phase.value, "{artifact_root}"))


def entrypoint_contract(label: str) -> ComponentEntrypointCommandContract:
    export = ComponentArtifactExportShape(
        f"surface-{label}",
        "portable-application",
        identity(f"abi-{label}"),
        identity("target"),
        "application/vnd.literate-ai.executable",
        identity("producer"),
    )
    return ComponentEntrypointCommandContract(
        entrypoint_identity=export.identity,
        deployment_unit=label,
        commands=(
            entrypoint_command(ComponentCommandPhase.TEST),
            entrypoint_command(ComponentCommandPhase.EXECUTE),
        ),
        tool_bindings=(
            ComponentCommandToolBinding(
                ComponentCommandPhase.TEST, identity("runtime")
            ),
            ComponentCommandToolBinding(
                ComponentCommandPhase.EXECUTE, identity("runtime")
            ),
        ),
        artifact_export=export,
    )


def library_surface() -> LibraryImportSurface:
    return LibraryImportSurface(
        language="rust",
        package="exact_stats",
        capabilities=(
            LibraryCapabilityImport(
                capability="statistics",
                interface_identity=identity("statistics-interface"),
                module="exact_stats",
                symbols=("mean", "variance"),
            ),
        ),
    )


class ComponentCommandContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = SchemaCatalog()

    def test_complete_contract_round_trips_and_reuses_export_declaration(self) -> None:
        value = contract()
        wire = value.to_dict()

        self.assertEqual(ComponentCommandContract.from_dict(wire), value)
        self.assertIsInstance(value.artifact_export, ComponentArtifactExportShape)
        self.assertEqual(
            value.command(ComponentCommandPhase.TEST).phase,
            ComponentCommandPhase.TEST,
        )
        self.assertEqual(
            value.tool_binding(ComponentCommandPhase.BUILD).toolchain_identity,
            value.build_system_toolchain_identity,
        )
        self.assertEqual(
            value.tool_binding(ComponentCommandPhase.EXECUTE).toolchain_identity,
            value.language_runtime_identity,
        )
        self.catalog.validate(COMMAND_SCHEMA, wire)
        for item in value.commands:
            self.catalog.validate(LIFECYCLE_COMMAND_SCHEMA, item.to_dict())

    def test_command_identity_changes_with_one_argv_token(self) -> None:
        original = contract()
        changed_build = replace(
            original.commands[0], argv=(*original.commands[0].argv, "--release")
        )
        changed = replace(original, commands=(changed_build, *original.commands[1:]))
        self.assertNotEqual(original.identity, changed.identity)

    def test_library_import_surface_and_consumer_binding_round_trip(self) -> None:
        surface = library_surface()
        export = replace(
            contract().artifact_export,
            role="library",
            abi_identity=surface.identity,
            media_type="application/vnd.literate-ai.rust-crate-tree",
        )
        library = replace(
            contract(), artifact_export=export, library_import_surface=surface
        )
        self.assertTrue(library.is_library)
        self.assertEqual(ComponentCommandContract.from_dict(library.to_dict()), library)
        self.catalog.validate(COMMAND_SCHEMA, library.to_dict())
        self.catalog.validate(LIBRARY_IMPORT_SURFACE_SCHEMA, surface.to_dict())
        self.assertEqual(surface.capability("statistics").symbols, ("mean", "variance"))
        with self.assertRaisesRegex(ValueError, "no import mapping"):
            surface.capability("missing")
        with self.assertRaisesRegex(ValueError, "no executable entrypoint"):
            library.entrypoint_command_contracts()

        binding = LibraryConsumerBinding(
            consumer_component_revision=identity("consumer"),
            provider_component_revision=library.component_revision,
            capability="statistics",
            interface_identity=surface.capability("statistics").interface_identity,
            artifact_identity=export.identity,
            import_surface_identity=surface.identity,
            target_identity=export.target_identity,
            dependency_artifact_identities=(identity("dependency-a"),),
        )
        self.assertEqual(LibraryConsumerBinding.from_dict(binding.to_dict()), binding)
        self.catalog.validate(LIBRARY_CONSUMER_BINDING_SCHEMA, binding.to_dict())

    def test_library_contract_is_additive_and_fails_closed_on_mixed_shape(self) -> None:
        executable = contract()
        self.assertNotIn("library_import_surface", executable.to_dict())
        self.assertEqual(
            ComponentCommandContract.from_dict(executable.to_dict()).identity,
            executable.identity,
        )
        with self.assertRaisesRegex(ValueError, "role 'library'"):
            replace(executable, library_import_surface=library_surface())

        surface = library_surface()
        library_export = replace(executable.artifact_export, role="library")
        with self.assertRaisesRegex(ValueError, "cannot be combined"):
            replace(
                executable,
                artifact_export=library_export,
                entrypoint_contracts=(entrypoint_contract("primary"),),
                library_import_surface=surface,
            )

    def test_library_contracts_require_canonical_non_ambient_names(self) -> None:
        surface = library_surface()
        with self.assertRaises((TypeError, ValueError)):
            replace(surface, package="ambient/package")
        with self.assertRaises((TypeError, ValueError)):
            replace(surface.capabilities[0], symbols=("variance", "mean"))
        with self.assertRaises((TypeError, ValueError)):
            LibraryConsumerBinding(
                consumer_component_revision=identity("consumer"),
                provider_component_revision=identity("provider"),
                capability="statistics",
                interface_identity=identity("interface"),
                artifact_identity=identity("artifact"),
                import_surface_identity=surface.identity,
                target_identity=identity("target"),
                dependency_artifact_identities=(identity("z"), identity("a")),
            )

    def test_build_and_language_authorities_are_independently_bound(self) -> None:
        original = contract()
        for field in (
            "build_system_resolver_identity",
            "build_system_toolchain_identity",
            "language_compiler_identity",
            "language_runtime_identity",
        ):
            with self.subTest(field=field):
                changed = replace(original, **{field: identity(f"changed-{field}")})
                self.assertNotEqual(original.identity, changed.identity)

        changed_binding = replace(
            original.tool_bindings[2], toolchain_identity=identity("other-runtime")
        )
        self.assertNotEqual(
            original.identity,
            replace(
                original,
                tool_bindings=(*original.tool_bindings[:2], changed_binding),
            ).identity,
        )

    def test_wire_requires_new_exact_authority_fields(self) -> None:
        wire = contract().to_dict()
        for field in (
            "build_system_resolver_identity",
            "build_system_toolchain_identity",
            "language_compiler_identity",
            "language_runtime_identity",
            "tool_bindings",
        ):
            with self.subTest(field=field):
                missing = dict(wire)
                missing.pop(field)
                with self.assertRaises((TypeError, ValueError)):
                    ComponentCommandContract.from_dict(missing)
                with self.assertRaises(AssertionError):
                    self.catalog.validate(COMMAND_SCHEMA, missing)
        legacy = dict(wire)
        legacy["toolchain_identity"] = identity("legacy-toolchain").to_dict()
        with self.assertRaises((TypeError, ValueError)):
            ComponentCommandContract.from_dict(legacy)
        with self.assertRaises(AssertionError):
            self.catalog.validate(COMMAND_SCHEMA, legacy)

    def test_tool_bindings_cover_every_phase_once_in_order(self) -> None:
        value = contract()
        with self.assertRaises((TypeError, ValueError)):
            replace(value, tool_bindings=value.tool_bindings[:2])
        with self.assertRaises((TypeError, ValueError)):
            replace(
                value,
                tool_bindings=(
                    value.tool_bindings[1],
                    value.tool_bindings[0],
                    value.tool_bindings[2],
                ),
            )

    def test_shell_strings_and_inferred_executables_are_rejected(self) -> None:
        with self.assertRaises((TypeError, ValueError)):
            ComponentLifecycleCommand(ComponentCommandPhase.BUILD, "tool build source")
        with self.assertRaises((TypeError, ValueError)):
            ComponentLifecycleCommand(
                ComponentCommandPhase.BUILD,
                (
                    "python",
                    "build.py",
                    "{source_root}",
                    "{object_root}",
                    "{export_path}",
                ),
            )
        wire = command(ComponentCommandPhase.BUILD).to_dict()
        wire["argv"] = "sh -c 'build source'"
        with self.assertRaises((TypeError, ValueError)):
            ComponentLifecycleCommand.from_dict(wire)
        with self.assertRaises(AssertionError):
            self.catalog.validate(LIFECYCLE_COMMAND_SCHEMA, wire)

    def test_unknown_embedded_and_duplicate_placeholders_are_rejected(self) -> None:
        base = command(ComponentCommandPhase.BUILD).argv
        invalid = (
            (*base, "{workspace}"),
            (*base, "--root={source_root}"),
            (*base, "{source_root}"),
        )
        for index, argv in enumerate(invalid):
            with self.subTest(case=index):
                with self.assertRaises((TypeError, ValueError)):
                    ComponentLifecycleCommand(ComponentCommandPhase.BUILD, argv)

    def test_every_phase_and_its_required_roles_are_mandatory(self) -> None:
        value = contract()
        with self.assertRaises((TypeError, ValueError)):
            replace(value, commands=value.commands[:2])
        with self.assertRaises((TypeError, ValueError)):
            replace(
                value.commands[0],
                argv=tuple(
                    item for item in value.commands[0].argv if item != "{source_root}"
                ),
            )
        reordered = (value.commands[1], value.commands[0], value.commands[2])
        with self.assertRaises((TypeError, ValueError)):
            replace(value, commands=reordered)

    def test_export_shape_is_static_and_locked_authorities_remain_typed(self) -> None:
        value = contract()
        with self.assertRaises((TypeError, ValueError)):
            replace(value, locked_build_authority_identity="inferred-from-files")
        changed = replace(
            value,
            artifact_export=replace(value.artifact_export, export_id="other-app"),
        )
        self.assertNotEqual(value.identity, changed.identity)
        self.assertNotIn("source_tree_identity", value.artifact_export.to_dict())
        self.assertNotIn("authorization_identity", value.artifact_export.to_dict())

    def test_substitution_requires_exact_typed_roles_and_never_a_shell(self) -> None:
        build = command(ComponentCommandPhase.BUILD)
        bindings = {
            ComponentCommandRole.TOOL: ("bazel",),
            ComponentCommandRole.SOURCE_ROOT: ("/work/source",),
            ComponentCommandRole.OBJECT_ROOT: ("/work/object",),
            ComponentCommandRole.EXPORT_PATH: ("/work/artifact/app",),
            ComponentCommandRole.PROVIDER_ARTIFACTS: (
                "/providers/money",
                "/providers/reporting",
            ),
        }
        self.assertEqual(
            build.substitute(bindings),
            (
                "bazel",
                "build",
                "/work/source",
                "--object-root",
                "/work/object",
                "--output",
                "/work/artifact/app",
                "/providers/money",
                "/providers/reporting",
            ),
        )
        self.assertNotIn(
            "{provider_artifacts}",
            build.substitute({**bindings, ComponentCommandRole.PROVIDER_ARTIFACTS: ()}),
        )
        self.assertEqual(
            build.substitute(
                {
                    **bindings,
                    ComponentCommandRole.TOOL: ("python3", "-I"),
                }
            )[:3],
            ("python3", "-I", "build"),
        )
        invalid = (
            {
                key: item
                for key, item in bindings.items()
                if key is not ComponentCommandRole.TOOL
            },
            {**bindings, ComponentCommandRole.ARTIFACT_ROOT: ("/extra",)},
            {**bindings, ComponentCommandRole.TOOL: "bazel"},
            {**bindings, ComponentCommandRole.TOOL: ()},
            {**bindings, ComponentCommandRole.TOOL: ("bazel\x00sh",)},
        )
        for index, candidate in enumerate(invalid):
            with self.subTest(case=index):
                with self.assertRaises((TypeError, ValueError)):
                    build.substitute(candidate)

    def test_single_entrypoint_contract_is_byte_identical_without_new_field(
        self,
    ) -> None:
        """ADR 0026 hard constraint: the additive field is ABSENT for the common
        single-entrypoint case, so wire bytes and identity are unchanged."""

        value = contract()
        self.assertIsNone(value.entrypoint_contracts)
        self.assertFalse(value.is_multi_entrypoint)
        wire = value.to_dict()
        # The golden shape is exactly the pre-ADR-0026 field set -- the new
        # entrypoint_contracts key does NOT appear.
        self.assertEqual(
            set(wire),
            {
                "schema",
                "component_revision",
                "locked_build_authority_identity",
                "build_system_resolver_identity",
                "build_system_toolchain_identity",
                "language_compiler_identity",
                "language_runtime_identity",
                "commands",
                "tool_bindings",
                "artifact_export",
            },
        )
        self.assertNotIn("entrypoint_contracts", wire)
        # Constructing the same contract WITHOUT ever touching the new field must
        # yield the same identity as the default None value, proving the identity
        # is unchanged from before the field existed.
        explicit_none = ComponentCommandContract(
            component_revision=value.component_revision,
            locked_build_authority_identity=value.locked_build_authority_identity,
            build_system_resolver_identity=value.build_system_resolver_identity,
            build_system_toolchain_identity=value.build_system_toolchain_identity,
            language_compiler_identity=value.language_compiler_identity,
            language_runtime_identity=value.language_runtime_identity,
            commands=value.commands,
            tool_bindings=value.tool_bindings,
            artifact_export=value.artifact_export,
            entrypoint_contracts=None,
        )
        self.assertEqual(value.identity, explicit_none.identity)
        self.assertEqual(value.to_dict(), explicit_none.to_dict())
        self.catalog.validate(COMMAND_SCHEMA, wire)
        # A single-entrypoint contract still yields exactly one synthetic
        # entrypoint contract for uniform dispatch fan-out.
        contracts = value.entrypoint_command_contracts()
        self.assertEqual(len(contracts), 1)
        self.assertEqual(contracts[0].artifact_export, value.artifact_export)

    def test_multi_entrypoint_contract_round_trips_and_keys_by_identity(self) -> None:
        entrypoints = (entrypoint_contract("frontend"), entrypoint_contract("api"))
        value = replace(contract(), entrypoint_contracts=entrypoints)

        self.assertTrue(value.is_multi_entrypoint)
        wire = value.to_dict()
        self.assertIn("entrypoint_contracts", wire)
        self.assertEqual(len(wire["entrypoint_contracts"]), 2)
        self.assertEqual(ComponentCommandContract.from_dict(wire), value)
        self.catalog.validate(COMMAND_SCHEMA, wire)
        # Presence of the additive field changes identity vs. the single case.
        self.assertNotEqual(contract().identity, value.identity)
        # Routing resolves each deployment unit's own commands and export shape.
        resolved = value.entrypoint_command_contract(entrypoints[1].entrypoint_identity)
        self.assertEqual(resolved.deployment_unit, "api")
        self.assertEqual(value.entrypoint_command_contracts(), entrypoints)

    def test_multi_entrypoint_rejects_empty_and_duplicate_entrypoints(self) -> None:
        with self.assertRaises((TypeError, ValueError)):
            replace(contract(), entrypoint_contracts=())
        duplicate = entrypoint_contract("frontend")
        with self.assertRaises((TypeError, ValueError)):
            replace(contract(), entrypoint_contracts=(duplicate, duplicate))


class ComponentEntrypointCommandContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = SchemaCatalog()

    def test_entrypoint_contract_round_trips_and_requires_runtime_phases(
        self,
    ) -> None:
        value = entrypoint_contract("frontend")
        wire = value.to_dict()
        self.assertEqual(ComponentEntrypointCommandContract.from_dict(wire), value)
        self.catalog.validate(
            "urn:literate-ai:schema:v2:multi-entrypoint-component-semantics@1", wire
        )
        self.assertEqual(
            value.command(ComponentCommandPhase.EXECUTE).phase,
            ComponentCommandPhase.EXECUTE,
        )
        # BUILD is never a per-entrypoint phase; the build is per Component.
        with self.assertRaises((TypeError, ValueError)):
            value.command(ComponentCommandPhase.BUILD)
        with self.assertRaises((TypeError, ValueError)):
            replace(
                value,
                commands=(
                    entrypoint_command(ComponentCommandPhase.BUILD),
                    entrypoint_command(ComponentCommandPhase.EXECUTE),
                ),
            )


if __name__ == "__main__":
    unittest.main()
