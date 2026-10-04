from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.source_to_specification import (
    WireMigrationError,
    migrate_source_to_specification_result_files,
    migrate_v1_source_to_specification_result,
)

REPOSITORY = Path(__file__).resolve().parents[2]
HISTORICAL_RESULT = (
    REPOSITORY
    / "tests"
    / "fixtures"
    / "schemas"
    / "v0.1.1"
    / "source-to-specification-result.json"
)


class VersionedWireContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.historical = json.loads(HISTORICAL_RESULT.read_text(encoding="utf-8"))

    def test_multi_file_migration_restores_exact_bytes_after_commit_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.json"
            second = root / "second.json"
            first.write_text(json.dumps(self.historical, separators=(",", ":")))
            second.write_text(json.dumps(self.historical, indent=4) + "\n\n")
            before = {path: path.read_bytes() for path in (first, second)}
            calls = 0

            def fail_second(source: Path, target: Path) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected second replacement failure")
                os.replace(source, target)

            with patch(
                "literate_ai.source_to_specification.wire_migrations._replace_for_commit",
                side_effect=fail_second,
            ):
                with self.assertRaises(WireMigrationError) as raised:
                    migrate_source_to_specification_result_files([first, second])
            self.assertEqual(raised.exception.code, "wire.file_migration_commit_failed")
            self.assertEqual(
                {path: path.read_bytes() for path in (first, second)}, before
            )

    def test_nested_invalid_v1_result_never_reaches_staging_or_commit(self) -> None:
        malformed = dict(self.historical, observations="not-an-array")
        with self.assertRaises(WireMigrationError) as raised:
            migrate_v1_source_to_specification_result(malformed)
        self.assertEqual(raised.exception.code, "wire.v1_result_invalid")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "legacy.json"
            path.write_text(json.dumps(malformed))
            before = path.read_bytes()
            with patch(
                "literate_ai.source_to_specification.wire_migrations._replace_for_commit"
            ) as replacement:
                with self.assertRaises(WireMigrationError) as raised:
                    migrate_source_to_specification_result_files([path])
            self.assertEqual(raised.exception.code, "wire.v1_result_invalid")
            replacement.assert_not_called()
            self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
