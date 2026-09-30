"""Real Cargo targets precede the Standard library case-result protocol."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.standard_project import _STANDARD_LIBRARY_TEST_DRIVER


class RustLibraryTestDriverTests(unittest.TestCase):
    def setUp(self):
        cargo = shutil.which("cargo")
        if cargo is None:
            self.skipTest("Cargo is unavailable")
        self.cargo = cargo
        temporary = tempfile.TemporaryDirectory(prefix="rust library targets ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.export = self.root / "library"
        self.package = self.export / "source"
        (self.package / "src/bin").mkdir(parents=True)
        (self.package / "tests").mkdir()
        (self.package / "Cargo.toml").write_text(
            '[package]\nname="fixture-library"\nversion="0.0.0"\nedition="2021"\n'
            '[[bin]]\nname="litai_artifact"\npath="src/bin/litai_artifact.rs"\n',
            encoding="utf-8",
        )
        (self.package / "src/lib.rs").write_text(
            "pub fn value() -> u8 { 7 }\n"
            "#[cfg(test)] mod tests { use super::value; "
            "#[test] fn unit_value() { assert_eq!(value(), 7); } }\n",
            encoding="utf-8",
        )
        (self.package / "src/bin/litai_artifact.rs").write_text(
            'fn main() { println!(r#"{{"schema":"literate-ai/generated-test-results@1",'
            '"cases":[{{"case_id":"example","outcome":"passed"}}]}}"#); }\n',
            encoding="utf-8",
        )
        self.integration = self.package / "tests/litai_test.rs"
        self.integration.write_text(
            "use fixture_library::value;\n"
            "#[test] fn integration_value() { assert_eq!(value(), 7); }\n",
            encoding="utf-8",
        )
        self.environment = {**os.environ, "CARGO_NET_OFFLINE": "true"}
        subprocess.run(
            [
                self.cargo,
                "generate-lockfile",
                "--offline",
                "--manifest-path",
                str(self.package / "Cargo.toml"),
            ],
            check=True,
            capture_output=True,
            env=self.environment,
            timeout=60,
        )

    def run_driver(self):
        before = self.snapshot()
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                _STANDARD_LIBRARY_TEST_DRIVER,
                "rust",
                json.dumps([self.cargo]),
                str(self.export),
                "source/Cargo.toml",
                "litai_artifact",
                str(self.export.parent),
            ],
            capture_output=True,
            text=True,
            env=self.environment,
            timeout=120,
        )
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.package / "target").exists())
        return completed

    def snapshot(self):
        return {
            p.relative_to(self.export).as_posix(): p.read_bytes()
            for p in self.export.rglob("*")
            if p.is_file()
        }

    def test_native_integration_and_nested_unit_tests_pass_before_json_protocol(self):
        result = self.run_driver()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "schema": "literate-ai/generated-test-results@1",
                "cases": [{"case_id": "example", "outcome": "passed"}],
            },
        )
        self.assertIn("integration_value", result.stderr)
        self.assertIn("unit_value", result.stderr)

    def test_invalid_integration_import_refuses_before_passing_protocol_binary(self):
        self.integration.write_text(
            "use super::value;\n"
            "#[test] fn invalid_import() { assert_eq!(value(), 7); }\n",
            encoding="utf-8",
        )
        result = self.run_driver()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("E0433", result.stderr)
        self.assertIn("litai_test.rs", result.stderr)
        self.assertIn("Cargo generated-test command failed:", result.stderr)
        self.assertIn('"--all-targets"', result.stderr)

    def test_failing_native_case_refuses_and_retains_case_diagnostic(self):
        self.integration.write_text(
            "use fixture_library::value;\n"
            "#[test] fn wrong_expected_value() { assert_eq!(value(), 99); }\n",
            encoding="utf-8",
        )
        result = self.run_driver()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("wrong_expected_value", result.stderr)
        self.assertIn("FAILED", result.stderr)

    def test_invalid_additional_target_is_not_hidden_by_selected_binary(self):
        examples = self.package / "examples"
        examples.mkdir()
        (examples / "broken.rs").write_text(
            "fn main() { missing_example_function(); }\n", encoding="utf-8"
        )
        result = self.run_driver()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("missing_example_function", result.stderr)
        self.assertIn("broken.rs", result.stderr)

    def test_native_pass_does_not_replace_the_attributable_protocol(self):
        (self.package / "src/bin/litai_artifact.rs").write_text(
            'fn main() { eprintln!("protocol driver failed"); '
            "std::process::exit(17); }\n",
            encoding="utf-8",
        )
        result = self.run_driver()
        self.assertEqual(result.returncode, 17, result.stderr)
        self.assertIn("integration_value", result.stderr)
        self.assertIn("protocol driver failed", result.stderr)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
