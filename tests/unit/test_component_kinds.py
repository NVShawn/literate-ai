"""Component lifecycle kinds make the CLI argv contract an explicit subclass."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.component_markdown import (
    parse_component_markdown,
    render_component_markdown,
)
from literate_ai.contracts import (
    CLI_APPLICATION_LIFECYCLE_ASSUMPTIONS,
    NATIVE_CLI_ENTRYPOINT_KIND,
    ComponentKind,
    ContractValidationError,
    Entrypoint,
    component_kind_from_reviewed_node,
    infer_component_kind,
    resolve_component_kind,
    validate_component_kind,
)


def _document(*, kind: str | None = None, entrypoints: str | None = None) -> str:
    lines = [
        "---",
        "namespace: samples",
        "name: invoice-cli",
        "version: 1.0.0",
        "display_name: Invoice CLI",
    ]
    if kind is not None:
        lines.append(f"kind: {kind}")
    lines.extend(
        [
            "profiles:",
            "  - sample",
            "sample: true",
            "provides: []",
            "requires: []",
            "authoring_inputs: []",
            "workflow_definition: workflows/host.md",
            "routing_policy: routing/default.json",
            "flavor_slots: []",
        ]
    )
    if entrypoints is None:
        lines.extend(
            [
                "entrypoints:",
                "  - name: run",
                "    kind: portable-application",
                "    path: run",
            ]
        )
    else:
        lines.append(entrypoints)
    lines.extend(
        [
            "acceptance_contracts: []",
            "source_dependencies: []",
            "---",
            "A portable invoice application.",
            "",
        ]
    )
    return "\n".join(lines)


class ComponentKindTests(unittest.TestCase):
    def _parse(self, text: str):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        path = root / "components" / "invoice-cli" / "component.md"
        path.parent.mkdir(parents=True)
        parsed = parse_component_markdown(path, text, project_root=root)
        return parsed, root, path

    def test_cli_application_is_an_explicit_subclass_not_the_base(self) -> None:
        self.assertEqual(
            infer_component_kind((Entrypoint("run", "portable-application", "run"),)),
            ComponentKind.CLI_APPLICATION,
        )
        self.assertEqual(infer_component_kind(()), ComponentKind.COMPONENT)
        self.assertNotEqual(ComponentKind.CLI_APPLICATION, ComponentKind.COMPONENT)
        self.assertEqual(len(CLI_APPLICATION_LIFECYCLE_ASSUMPTIONS), 4)

    def test_omitted_kind_infers_cli_and_does_not_emit_the_field(self) -> None:
        parsed, root, path = self._parse(_document())
        rendered = render_component_markdown(parsed, path, project_root=root)
        self.assertEqual(parsed.kind, "")
        self.assertEqual(parsed.resolved_kind, "cli-application")
        self.assertNotIn("\nkind:", rendered)

    def test_native_cli_kind_is_explicit_and_not_the_json_argv_abi(self) -> None:
        native = Entrypoint("run", NATIVE_CLI_ENTRYPOINT_KIND, "run")
        self.assertEqual(
            infer_component_kind((native,)), ComponentKind.NATIVE_CLI_APPLICATION
        )
        parsed, root, path = self._parse(
            _document(
                kind="native-cli-application",
                entrypoints=(
                    "entrypoints:\n  - name: run\n    kind: native-cli\n    path: run"
                ),
            )
        )
        rendered = render_component_markdown(parsed, path, project_root=root)
        self.assertEqual(parsed.resolved_kind, "native-cli-application")
        self.assertIn("kind: native-cli-application\n", rendered)
        with self.assertRaisesRegex(
            ContractValidationError, "portable-application JSON entrypoint"
        ):
            validate_component_kind(
                ComponentKind.NATIVE_CLI_APPLICATION,
                (native, Entrypoint("json", "portable-application", "json")),
            )
        with self.assertRaisesRegex(
            ContractValidationError, "cannot declare a native-cli entrypoint"
        ):
            validate_component_kind(
                ComponentKind.CLI_APPLICATION,
                (Entrypoint("json", "portable-application", "json"), native),
            )

    def test_explicit_library_rejects_cli_entrypoints(self) -> None:
        with self.assertRaises(ContractValidationError) as raised:
            self._parse(_document(kind="library"))
        self.assertIn("library entrypoints must be empty", str(raised.exception))

    def test_library_kind_round_trips_with_empty_entrypoints(self) -> None:
        parsed, root, path = self._parse(
            _document(kind="library", entrypoints="entrypoints: []")
        )
        rendered = render_component_markdown(parsed, path, project_root=root)
        reparsed = parse_component_markdown(path, rendered, project_root=root)
        self.assertEqual(parsed.kind, "library")
        self.assertEqual(parsed.resolved_kind, "library")
        self.assertIn("kind: library\n", rendered)
        self.assertEqual(reparsed, parsed)

    def test_schema_only_and_ui_and_packaged_module_have_distinct_contracts(
        self,
    ) -> None:
        schema, _, _ = self._parse(
            _document(kind="schema-only", entrypoints="entrypoints: []")
        )
        ui, _, _ = self._parse(_document(kind="ui", entrypoints="entrypoints: []"))
        packaged, _, _ = self._parse(
            _document(kind="packaged-module", entrypoints="entrypoints: []")
        )
        self.assertEqual(schema.resolved_kind, "schema-only")
        self.assertEqual(ui.resolved_kind, "ui")
        self.assertEqual(packaged.resolved_kind, "packaged-module")
        with self.assertRaises(ContractValidationError):
            self._parse(_document(kind="ui"))
        with self.assertRaises(ContractValidationError):
            self._parse(_document(kind="packaged-module"))
        with self.assertRaises(ContractValidationError):
            self._parse(_document(kind="schema-only"))

    def test_persistent_service_requires_its_entrypoint_kind(self) -> None:
        parsed, _, _ = self._parse(
            _document(
                kind="persistent-service",
                entrypoints=(
                    "entrypoints:\n"
                    "  - name: service\n"
                    "    kind: persistent-service\n"
                    "    path: service"
                ),
            )
        )
        self.assertEqual(parsed.resolved_kind, "persistent-service")
        with self.assertRaises(ContractValidationError):
            self._parse(_document(kind="persistent-service"))

    def test_reviewed_inverse_node_kinds_map_onto_component_kinds(self) -> None:
        self.assertEqual(
            component_kind_from_reviewed_node("cli"),
            ComponentKind.CLI_APPLICATION,
        )
        self.assertEqual(
            component_kind_from_reviewed_node("library"), ComponentKind.LIBRARY
        )
        self.assertEqual(
            component_kind_from_reviewed_node("service"),
            ComponentKind.PERSISTENT_SERVICE,
        )
        with self.assertRaises(ContractValidationError):
            component_kind_from_reviewed_node("batch")

    def test_multi_entrypoint_set_is_accepted_and_fails_closed_on_contradiction(
        self,
    ) -> None:
        """ADR 0026: kind must be consistent with the SET of entrypoint kinds."""

        cli_and_service = (
            Entrypoint("run", "portable-application", "run"),
            Entrypoint("collector", "persistent-service", "collector"),
        )
        # A cli-application Component may own several cooperating surfaces so long
        # as its required portable-application entrypoint is present.
        self.assertEqual(
            resolve_component_kind("cli-application", cli_and_service),
            ComponentKind.CLI_APPLICATION,
        )
        validate_component_kind(ComponentKind.CLI_APPLICATION, cli_and_service)
        # An unnamed deployment unit resolves to the entrypoint's own implicit unit.
        self.assertEqual(cli_and_service[0].resolved_deployment_unit, "run")
        # A persistent-service Component with two service surfaces is consistent.
        services = (
            Entrypoint("api", "persistent-service", "api", deployment_unit="api"),
            Entrypoint(
                "worker",
                "persistent-service",
                "worker",
                deployment_unit="worker",
            ),
        )
        validate_component_kind(ComponentKind.PERSISTENT_SERVICE, services)
        self.assertEqual(services[0].resolved_deployment_unit, "api")
        self.assertEqual(services[1].resolved_deployment_unit, "worker")
        # Fail closed: a persistent-service that also declares a CLI surface, and
        # a library that declares any entrypoint at all.
        with self.assertRaises(ContractValidationError):
            validate_component_kind(ComponentKind.PERSISTENT_SERVICE, cli_and_service)
        with self.assertRaises(ContractValidationError):
            validate_component_kind(ComponentKind.LIBRARY, services)
        # Fail closed: a persistent-service kind whose required entrypoint kind is
        # absent from the set.
        cli_only = (
            Entrypoint("run", "portable-application", "run"),
            Entrypoint("admin", "portable-application", "admin"),
        )
        with self.assertRaises(ContractValidationError):
            validate_component_kind(ComponentKind.PERSISTENT_SERVICE, cli_only)

    def test_entrypoint_deployment_unit_is_absent_when_undeclared(self) -> None:
        entrypoint = Entrypoint("run", "portable-application", "run")
        self.assertIsNone(entrypoint.deployment_unit)
        self.assertNotIn("deployment_unit", entrypoint.to_dict())
        self.assertEqual(Entrypoint.from_dict(entrypoint.to_dict()), entrypoint)
        declared = Entrypoint(
            "run", "portable-application", "run", deployment_unit="frontend"
        )
        self.assertEqual(declared.to_dict()["deployment_unit"], "frontend")
        self.assertEqual(Entrypoint.from_dict(declared.to_dict()), declared)

    def test_component_rejects_duplicate_resolved_deployment_units(self) -> None:
        document = _document(
            kind="cli-application",
            entrypoints=(
                "entrypoints:\n"
                "  - name: run\n"
                "    kind: portable-application\n"
                "    path: run\n"
                "    deployment_unit: shared\n"
                "  - name: inspect\n"
                "    kind: portable-application\n"
                "    path: inspect\n"
                "    deployment_unit: shared"
            ),
        )
        with self.assertRaisesRegex(
            ContractValidationError, "resolved deployment units"
        ):
            self._parse(document)

    def test_declined_kinds_are_rejected_and_named_assumptions_stay_cli_only(
        self,
    ) -> None:
        with self.assertRaises(ContractValidationError):
            resolve_component_kind("batch", ())
        with self.assertRaises(ContractValidationError):
            resolve_component_kind("event", ())
        with self.assertRaises(ContractValidationError):
            resolve_component_kind("wrapped-source", ())
        validate_component_kind(ComponentKind.COMPONENT, ())
        with self.assertRaises(ContractValidationError):
            validate_component_kind(
                ComponentKind.COMPONENT,
                (Entrypoint("run", "portable-application", "run"),),
            )


if __name__ == "__main__":
    unittest.main()
