"""Module and JSX source extensions remain eligible without executing input."""

import tempfile
import unittest
from pathlib import Path

from literate_ai.source_to_specification.inventory import (
    SourceFileClassification,
    inventory_source,
)
from literate_ai.source_to_specification.skills import (
    builtin_skill_set,
    load_builtin_skill_catalog,
)
from literate_ai.source_to_specification.static_workflow import derive_static_checkout

EXTENSIONS = {
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "typescript",
}
SOURCE = (
    "export function calculate(value) { return value + 1; }\n"
    "throw new Error('static input must never execute');\n"
)


class SourceInventoryModuleTests(unittest.TestCase):
    def test_module_and_jsx_extensions_derive_with_their_unchanged_paths(self):
        catalog = load_builtin_skill_catalog()
        for extension, language in EXTENSIONS.items():
            with (
                self.subTest(extension=extension),
                tempfile.TemporaryDirectory() as tmp,
            ):
                root = Path(tmp)
                source = root / ("example" + extension)
                source.write_text(SOURCE, encoding="utf-8")
                inventory = inventory_source(root)
                entry = inventory.entries[0]
                self.assertEqual(entry.path, source.name)
                self.assertEqual(entry.language, language)
                self.assertEqual(entry.classification, SourceFileClassification.SOURCE)
                result = derive_static_checkout(
                    source=root,
                    inventory=inventory,
                    origin_attestation_id="fixture-origin",
                    skill_set=builtin_skill_set(catalog, languages=(language,)),
                    skill_catalog=catalog,
                )
                self.assertTrue(result.result.observations)
                self.assertEqual(source.read_text(encoding="utf-8"), SOURCE)

    def test_test_and_minified_names_keep_their_classifications(self):
        for extension in EXTENSIONS:
            for stem, expected in (
                ("example.test", SourceFileClassification.TEST),
                ("example.spec", SourceFileClassification.TEST),
                ("example.min", SourceFileClassification.MINIFIED),
            ):
                with self.subTest(extension=extension, stem=stem):
                    with tempfile.TemporaryDirectory() as tmp:
                        source = Path(tmp) / (stem + extension)
                        source.write_text(SOURCE, encoding="utf-8")
                        self.assertEqual(
                            inventory_source(source).entries[0].classification, expected
                        )

    def test_sensitive_binary_and_generated_exclusions_still_precede_source(self):
        for extension in EXTENSIONS:
            for content, expected in (
                (
                    b"api_key='fixture_credential_12345';\n",
                    SourceFileClassification.SENSITIVE,
                ),
                (b"binary\0payload", SourceFileClassification.BINARY),
                (
                    b"// @generated\n" + SOURCE.encode(),
                    SourceFileClassification.GENERATED,
                ),
            ):
                with self.subTest(extension=extension, expected=expected):
                    with tempfile.TemporaryDirectory() as tmp:
                        source = Path(tmp) / ("example" + extension)
                        source.write_bytes(content)
                        self.assertEqual(
                            inventory_source(source).entries[0].classification, expected
                        )

    def test_large_module_source_remains_large(self):
        content = b"export const value = 7;\n" * 100000
        self.assertGreater(len(content), 2 * 1024 * 1024)
        for extension in (".mjs", ".tsx"):
            with (
                self.subTest(extension=extension),
                tempfile.TemporaryDirectory() as tmp,
            ):
                source = Path(tmp) / ("example" + extension)
                source.write_bytes(content)
                entry = inventory_source(source).entries[0]
                self.assertEqual(entry.classification, SourceFileClassification.LARGE)
                self.assertIn(entry.language, {"javascript", "typescript"})
