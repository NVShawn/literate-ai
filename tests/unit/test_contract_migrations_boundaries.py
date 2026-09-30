"""Contract migration and dependency-direction tests."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from literate_ai.contracts import MigrationError, SchemaMigrationRegistry

ROOT = Path(__file__).resolve().parents[2]


class ContractMigrationTests(unittest.TestCase):
    def test_migration_is_pure_explicit_and_deterministic(self) -> None:
        registry = SchemaMigrationRegistry()
        registry.register(
            "example/item@1",
            "example/item@2",
            lambda item: {
                "schema": "example/item@2",
                "name": item["legacy_name"],
                "extensions": {
                    key: value
                    for key, value in item.items()
                    if key not in {"schema", "legacy_name"}
                },
            },
        )
        original = {
            "schema": "example/item@1",
            "legacy_name": "sample",
            "future": {"kept": True},
        }
        result = registry.migrate(original, target_schema="example/item@2")
        self.assertEqual(original["schema"], "example/item@1")
        self.assertEqual(result["name"], "sample")
        self.assertEqual(result["extensions"]["future"], {"kept": True})

    def test_missing_path_has_stable_error(self) -> None:
        with self.assertRaises(MigrationError) as caught:
            SchemaMigrationRegistry().migrate(
                {"schema": "example/item@1"}, target_schema="example/item@2"
            )
        self.assertEqual(caught.exception.code, "contracts.migration_path_missing")

    def test_nondeterministic_migration_is_rejected(self) -> None:
        calls = iter((1, 2))
        registry = SchemaMigrationRegistry()
        registry.register(
            "example/item@1",
            "example/item@2",
            lambda item: {"schema": "example/item@2", "call": next(calls)},
        )
        with self.assertRaises(MigrationError) as caught:
            registry.migrate(
                {"schema": "example/item@1"}, target_schema="example/item@2"
            )
        self.assertEqual(caught.exception.code, "contracts.migration_nondeterministic")


class DependencyDirectionTests(unittest.TestCase):
    def test_domain_kernel_has_no_ova_or_adapter_imports(self) -> None:
        kernel_directories = (
            "contracts",
            "models",
            "security",
            "artifacts",
            "composition",
            "registry",
            "intelligence",
            "workflows",
        )
        violations: list[str] = []
        for directory in kernel_directories:
            for path in sorted((ROOT / "src" / "literate_ai" / directory).glob("*.py")):
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                for node in ast.walk(tree):
                    imported: list[str] = []
                    if isinstance(node, ast.Import):
                        imported = [alias.name for alias in node.names]
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        imported = [node.module]
                    for name in imported:
                        if name == "ova" or name.startswith("ova."):
                            violations.append(
                                f"{path.relative_to(ROOT)} imports {name}"
                            )
                        if name.startswith("literate_ai.adapters"):
                            violations.append(
                                f"{path.relative_to(ROOT)} imports {name}"
                            )
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
