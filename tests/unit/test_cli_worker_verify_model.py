from __future__ import annotations

import io
import json
import unittest
from unittest import mock

from literate_ai.adapters.live_test_selection import LiveTestSelection
from literate_ai.adapters.models.coding_cli import CodingCliError
from literate_ai.cli.dispatch import main


class WorkerVerifyModelCliTests(unittest.TestCase):
    _SELECTION = LiveTestSelection(
        "opencode",
        "custom-provider/router/example-model",
        "test-config",
        "test-config",
    )

    def test_scoped_help_is_available(self) -> None:
        output = io.StringIO()
        self.assertEqual(main(["worker", "verify-model", "help"], stdout=output), 0)
        self.assertIn("--model", output.getvalue())
        self.assertIn("--coding-cli", output.getvalue())

    def test_resolving_model_returns_ok_json(self) -> None:
        output = io.StringIO()
        with (
            mock.patch(
                "literate_ai.adapters.live_test_selection.resolve_live_test_selection",
                return_value=self._SELECTION,
            ),
            mock.patch(
                "literate_ai.adapters.live_test_selection.verify_live_model_resolves",
            ) as verified,
        ):
            status = main(["--json", "worker", "verify-model"], stdout=output)
        self.assertEqual(status, 0)
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["result"]["resolves"])
        self.assertEqual(
            payload["result"]["model"],
            "custom-provider/router/example-model",
        )
        verified.assert_called_once()

    def test_unresolvable_model_fails_closed(self) -> None:
        output = io.StringIO()
        errors = io.StringIO()
        with (
            mock.patch(
                "literate_ai.adapters.live_test_selection.resolve_live_test_selection",
                return_value=LiveTestSelection(
                    "opencode",
                    "router/example-model",
                    "test-config",
                    "test-config",
                ),
            ),
            mock.patch(
                "literate_ai.adapters.live_test_selection.verify_live_model_resolves",
                side_effect=CodingCliError(
                    "coding_cli.model_unavailable",
                    "live-qualification model 'router/example-model' did "
                    "not resolve for coding CLI 'opencode' (coding_cli.task_failed): "
                    "ProviderModelNotFoundError: Model not found.",
                ),
            ),
        ):
            status = main(
                ["--json", "worker", "verify-model"], stdout=output, stderr=errors
            )
        self.assertNotEqual(status, 0)
        payload = json.loads(errors.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "coding_cli.model_unavailable")
        self.assertIn("router/example-model", payload["error"]["message"])


if __name__ == "__main__":
    unittest.main()
