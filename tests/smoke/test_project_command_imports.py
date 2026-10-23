"""Keep optional MAC validation out of ordinary project command startup."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class ProjectCommandImportTests(unittest.TestCase):
    def test_project_import_does_not_initialize_optional_schema_parsers(self) -> None:
        probe = """
import importlib.abc
import sys
class RefuseOptionalSchemas(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        optional = 'literate_ai.adapters.mac_project_contract'
        grammars = {'jsonschema', 'rfc3987_syntax', 'lark'}
        if fullname == optional or fullname.split('.')[0] in grammars:
            raise AssertionError('ordinary project startup loaded ' + fullname)
sys.meta_path.insert(0, RefuseOptionalSchemas())
import literate_ai.cli.project
"""
        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            timeout=60,
            env=dict(os.environ),
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_mac_command_still_rejects_strict_schema_violation(self) -> None:
        from literate_ai.cli import main
        from literate_ai.contracts import CYCLONEDX_SCHEMA_URI, canonical_json_bytes

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bom = root / "invalid.cdx.json"
            bom.write_bytes(
                canonical_json_bytes(
                    {
                        "$schema": CYCLONEDX_SCHEMA_URI,
                        "bomFormat": "CycloneDX",
                        "specVersion": "1.7",
                        "version": 1,
                        "components": "invalid component collection",
                    }
                )
            )
            output = io.StringIO()
            errors = io.StringIO()
            status = main(
                [
                    "--json",
                    "project",
                    "mac-contract",
                    str(bom),
                    "--project",
                    str(Path(__file__).resolve().parents[2]),
                    "--platform",
                    "linux",
                ],
                stdout=output,
                stderr=errors,
            )
            self.assertNotEqual(status, 0)
            response = json.loads(errors.getvalue())
            self.assertEqual(
                response["error"]["code"], "mac-contract.bom-schema-invalid"
            )
