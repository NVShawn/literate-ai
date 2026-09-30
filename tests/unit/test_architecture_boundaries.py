"""Executable dependency-direction rules for the hexagonal package layout."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "src" / "literate_ai"

DOMAIN_PACKAGES = (
    "artifacts",
    "composition",
    "contracts",
    "intelligence",
    "models",
    "registry",
    "security",
    "workflows",
)
INFRASTRUCTURE_PREFIXES = (
    "literate_ai.adapters",
    "literate_ai.cli",
)


def _imports(path: Path) -> tuple[tuple[str, tuple[str, ...]], ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    result: list[tuple[str, tuple[str, ...]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.extend((alias.name, ()) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.append((node.module, tuple(alias.name for alias in node.names)))
    return tuple(result)


def _python_files(root: Path) -> tuple[Path, ...]:
    return tuple(sorted(root.rglob("*.py")))


class ArchitectureBoundaryTests(unittest.TestCase):
    def test_domain_never_imports_application_adapters_or_cli(self) -> None:
        forbidden = (*INFRASTRUCTURE_PREFIXES, "literate_ai.application")
        violations = []
        for package in DOMAIN_PACKAGES:
            for path in _python_files(PACKAGE / package):
                for module, _names in _imports(path):
                    if module.startswith(forbidden):
                        violations.append(f"{path.relative_to(ROOT)} imports {module}")
        self.assertEqual(violations, [])

    def test_application_never_imports_adapters_or_cli(self) -> None:
        violations = []
        for path in _python_files(PACKAGE / "application"):
            for module, _names in _imports(path):
                if module.startswith(INFRASTRUCTURE_PREFIXES):
                    violations.append(f"{path.relative_to(ROOT)} imports {module}")
        self.assertEqual(violations, [])

    def test_new_generation_service_depends_only_on_contracts(self) -> None:
        path = PACKAGE / "application" / "generation_preparation.py"
        internal = {
            module
            for module, _names in _imports(path)
            if module.startswith("literate_ai.")
        }
        self.assertEqual(internal, {"literate_ai.contracts.identity"})

    def test_generation_cli_does_not_own_authoritative_preparation_compilers(
        self,
    ) -> None:
        path = PACKAGE / "cli" / "generation.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        definitions = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        forbidden = {
            "_applicable_project_default_flavors",
            "_component_catalog",
            "_component_documents",
            "_compile_execution_plan_adapter",
            "_create_locked_generation_recipe",
            "_effective_toolchain_constraints",
            "_flavor_contributions",
            "_load_flavor_catalog",
            "_locked_component_definition",
            "_locked_component_documents",
            "_locked_recipe_flavors",
            "_locked_toolchain_constraints",
            "_managed_sbom_graph_for_composition",
            "_pinned_reference_content",
            "_prepare_generation_unchecked",
            "_require_declared_slot_targets",
            "_require_flavor_slots",
            "_required_generation_entrypoints",
            "_review_component_authority_adapter",
        }
        self.assertEqual(definitions & forbidden, set())

    def test_project_cli_does_not_own_project_validation_or_authority(self) -> None:
        path = PACKAGE / "cli" / "project.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        definitions = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        forbidden = {
            "_agent_skill",
            "_authority_review",
            "_catalog_file_identities",
            "_documentation_catalog",
            "_inverse_skill_catalog",
            "_skill_catalog",
            "_validate_component_authoring",
            "_validated_project",
            "validated_project_authority_identity",
        }
        self.assertEqual(definitions & forbidden, set())

    def test_rebuild_cli_does_not_own_external_driver_adaptation(self) -> None:
        path = PACKAGE / "cli" / "rebuild.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        definitions = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        forbidden = {
            "_argv",
            "_driver_environment",
            "_driver_environment_identity_material",
            "_driver_implementation_identity",
            "_executable",
            "_require_driver_implementation",
            "_require_executable_unchanged",
            "_run_driver",
        }
        self.assertEqual(definitions & forbidden, set())

    def test_standard_plan_composition_stays_in_adapter_and_application_layers(
        self,
    ) -> None:
        cli = PACKAGE / "cli" / "generation.py"
        adapter = PACKAGE / "adapters" / "standard_project.py"
        cli_imports = _imports(cli)
        adapter_imports = _imports(adapter)

        standard_cli_imports = next(
            names
            for module, names in cli_imports
            if module == "literate_ai.adapters.standard_project"
        )
        self.assertIn("FilesystemStandardProjectPlanningAdapter", standard_cli_imports)
        self.assertIn("FilesystemStandardSourceGenerationAdapter", standard_cli_imports)
        self.assertNotIn("prepare_component_generation_nodes", standard_cli_imports)
        self.assertFalse(
            any(
                module == "literate_ai.application.component_execution_planning"
                for module, _names in cli_imports
            )
        )
        standard_service_imports = next(
            names
            for module, names in adapter_imports
            if module == "literate_ai.application.standard_project_services"
        )
        self.assertIn("StandardProjectApplicationService", standard_service_imports)

    def test_adapters_and_samples_import_no_cli_private_helpers(self) -> None:
        violations = []
        roots = (
            PACKAGE / "adapters",
            ROOT / "samples",
            ROOT / "tests" / "conformance" / "support",
        )
        for root in roots:
            for path in _python_files(root):
                for module, names in _imports(path):
                    if module.startswith("literate_ai.cli"):
                        violations.append(
                            f"{path.relative_to(ROOT)} imports CLI module {module} "
                            f"names {names}"
                        )
        self.assertEqual(violations, [])

    def test_conformance_support_uses_the_public_standard_runtime_facade(self) -> None:
        path = ROOT / "tests" / "conformance" / "support" / "standard_service_stack.py"
        source = path.read_text(encoding="utf-8")
        imported_names = {name for _module, names in _imports(path) for name in names}
        self.assertTrue(
            {
                "StandardProjectExecutionRequest",
                "assemble_filesystem_standard_project_runtime",
            }
            <= imported_names
        )
        self.assertFalse(
            {
                "LocalStandardLifecyclePorts",
                "StandardProjectLifecycleService",
            }
            & imported_names
        )
        self.assertNotIn("forbidden_exact_argument_vectors", source)
        self.assertNotIn('document.path == "acceptance/execution.json"', source)

    def test_production_code_never_writes_legacy_component_manifests(self) -> None:
        violations = []
        for path in _python_files(PACKAGE):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"write_bytes", "write_text"}
                ):
                    continue
                if any(
                    isinstance(part, ast.Constant) and part.value == "component.json"
                    for part in ast.walk(node.func.value)
                ):
                    violations.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        self.assertEqual(violations, [])

    def test_source_promotion_does_not_depend_on_legacy_authoring_migration(
        self,
    ) -> None:
        source = (PACKAGE / "cli" / "source_to_specification.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("component_authoring_migration", source)
        self.assertNotIn("ComponentDefinition(", source)


if __name__ == "__main__":
    unittest.main()
