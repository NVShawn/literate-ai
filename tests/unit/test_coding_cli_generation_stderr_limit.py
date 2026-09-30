"""Configure finite source-generation stderr custody without widening other limits."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.adapters.models import CodingCliError
from literate_ai.adapters.models.coding_cli import (
    DEFAULT_MAXIMUM_GENERATION_CLI_STDERR_BYTES,
)
from literate_ai.adapters.standard_project import (
    _MAXIMUM_CODING_CLI_GENERATION_STDERR_LIMIT_BYTES,
    CODING_CLI_GENERATION_STDERR_LIMIT_ENVIRONMENT,
    FilesystemStandardSourceGenerationAdapter,
    _coding_cli_generation_stderr_limit_bytes,
)


class CodingCliGenerationStderrLimitTests(unittest.TestCase):
    def test_defaults_to_the_finite_source_generation_allowance(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=False):
            os.environ.pop(CODING_CLI_GENERATION_STDERR_LIMIT_ENVIRONMENT, None)
            self.assertEqual(
                _coding_cli_generation_stderr_limit_bytes(),
                DEFAULT_MAXIMUM_GENERATION_CLI_STDERR_BYTES,
            )

    def test_honors_a_configured_64_mib_allowance(self) -> None:
        configured = 64 * 1024 * 1024
        with mock.patch.dict(
            "os.environ",
            {CODING_CLI_GENERATION_STDERR_LIMIT_ENVIRONMENT: str(configured)},
        ):
            self.assertEqual(_coding_cli_generation_stderr_limit_bytes(), configured)

    def test_rejects_invalid_or_excessive_allowances(self) -> None:
        invalid_values = (
            "0",
            "-5",
            "not-a-number",
            "",
            str(_MAXIMUM_CODING_CLI_GENERATION_STDERR_LIMIT_BYTES + 1),
        )
        for invalid in invalid_values:
            with self.subTest(invalid=invalid):
                with mock.patch.dict(
                    "os.environ",
                    {CODING_CLI_GENERATION_STDERR_LIMIT_ENVIRONMENT: invalid},
                ):
                    with self.assertRaises(CodingCliError) as raised:
                        _coding_cli_generation_stderr_limit_bytes()
                self.assertEqual(
                    raised.exception.code,
                    "coding_cli.generation_stderr_limit_configuration_invalid",
                )

    def test_standard_local_generation_receives_only_the_configured_stderr_limit(
        self,
    ) -> None:
        configured = 64 * 1024 * 1024
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.dict(
                "os.environ",
                {
                    "CODING_CLI": "codex",
                    CODING_CLI_GENERATION_STDERR_LIMIT_ENVIRONMENT: str(configured),
                },
            ),
            mock.patch(
                "literate_ai.adapters.models.coding_cli.shutil.which",
                return_value=str(Path(sys.executable).resolve()),
            ),
        ):
            root = Path(directory)
            adapter = FilesystemStandardSourceGenerationAdapter.from_environment(
                project_root=root,
                cache_root=root / "cache",
                cas_root=root / "cas",
            )

        self.assertEqual(adapter.generator.maximum_cli_stderr_bytes, configured)
        self.assertEqual(
            adapter.generator.maximum_cli_stdout_bytes,
            1024 * 1024,
        )


if __name__ == "__main__":
    unittest.main()
