"""Live qualification requires an explicit coding CLI and model (ADR 0017)."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.adapters.live_test_selection import (
    LIVE_MODEL_ENVIRONMENT,
    REMOTE_LIVE_GATE_ENVIRONMENT,
    LiveTestSelection,
    apply_live_test_selection,
    posix_export_prefix,
    remote_live_gate_overlay,
    resolve_live_test_selection,
    try_resolve_live_test_selection,
    verify_live_model_resolves,
)
from literate_ai.adapters.models.coding_cli import CodingCliError, select_coding_cli


class LiveTestSelectionTests(unittest.TestCase):
    def test_missing_configuration_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(CodingCliError) as raised:
                resolve_live_test_selection(
                    environment={},
                    test_config_path=Path(temporary) / "missing.json",
                )
        self.assertEqual(
            raised.exception.code, "coding_cli.test_selection_unconfigured"
        )

    def test_try_resolve_returns_none_when_unconfigured(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            selected = try_resolve_live_test_selection(
                environment={},
                test_config_path=Path(temporary) / "missing.json",
            )
        self.assertIsNone(selected)

    def test_test_config_supplies_the_durable_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "literate.test.json"
            path.write_text(
                json.dumps(
                    {
                        "schema": "literate-ai/global-test-matrix@3",
                        "coding_cli": "opencode",
                        "model": "openai/gpt-5",
                        "default_samples": ["*"],
                        "workers": [{"worker_id": "local"}],
                    }
                ),
                encoding="utf-8",
            )
            selected = resolve_live_test_selection(
                environment={},
                test_config_path=path,
            )
        self.assertEqual(selected.coding_cli, "opencode")
        self.assertEqual(selected.model, "openai/gpt-5")
        self.assertEqual(selected.coding_cli_provenance, "test-config")
        self.assertEqual(selected.model_provenance, "test-config")

    def test_environment_overrides_test_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "literate.test.json"
            path.write_text(
                json.dumps({"coding_cli": "opencode", "model": "from-file"}),
                encoding="utf-8",
            )
            selected = resolve_live_test_selection(
                environment={
                    "CODING_CLI": "claude",
                    LIVE_MODEL_ENVIRONMENT: "from-env",
                },
                test_config_path=path,
            )
        self.assertEqual(selected.coding_cli, "claude")
        self.assertEqual(selected.model, "from-env")
        self.assertEqual(selected.coding_cli_provenance, "environment")
        self.assertEqual(selected.model_provenance, "environment")

    def test_cli_flags_override_environment_and_test_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "literate.test.json"
            path.write_text(
                json.dumps({"coding_cli": "opencode", "model": "from-file"}),
                encoding="utf-8",
            )
            selected = resolve_live_test_selection(
                coding_cli="codex",
                model="from-flag",
                environment={
                    "CODING_CLI": "claude",
                    LIVE_MODEL_ENVIRONMENT: "from-env",
                },
                test_config_path=path,
            )
        self.assertEqual(selected.coding_cli, "codex")
        self.assertEqual(selected.model, "from-flag")
        self.assertEqual(selected.coding_cli_provenance, "cli-flag")
        self.assertEqual(selected.model_provenance, "cli-flag")
        self.assertEqual(selected.user_coding_cli, "opencode")
        self.assertEqual(selected.user_model, "from-file")

    def test_complete_cli_selection_does_not_require_a_canonical_project(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch("pathlib.Path.cwd", return_value=Path(temporary)):
                selected = resolve_live_test_selection(
                    coding_cli="codex",
                    model="gpt-5.6-sol-high",
                    environment={},
                )
        self.assertEqual(selected.coding_cli, "codex")
        self.assertEqual(selected.model, "gpt-5.6-sol-high")
        self.assertIsNone(selected.user_coding_cli)
        self.assertIsNone(selected.user_model)

    def test_cli_override_is_used_by_select_coding_cli_without_rewriting_the_file(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "literate.test.json"
            original = json.dumps({"coding_cli": "opencode", "model": "from-file"})
            path.write_text(original, encoding="utf-8")
            selected = resolve_live_test_selection(
                coding_cli="cursor-agent",
                model="gpt-5.6-sol-high",
                environment={},
                test_config_path=path,
            )
            overlay = apply_live_test_selection({}, selected)
            self.assertEqual(path.read_text(encoding="utf-8"), original)
            self.assertEqual(overlay["CODING_CLI"], "cursor-agent")
            self.assertEqual(overlay[LIVE_MODEL_ENVIRONMENT], "gpt-5.6-sol-high")
            self.assertTrue(selected.cli_flags_override_user_default())

            def locate(name, *, path):
                del path
                if name in {"opencode", "cursor-agent", "codex", "claude"}:
                    return sys.executable
                return None

            with mock.patch(
                "literate_ai.adapters.models.coding_cli.shutil.which",
                side_effect=locate,
            ) as which:
                bound = select_coding_cli({**overlay, "PATH": "/tools"})
            self.assertEqual(bound.name, "cursor-agent")
            which.assert_called_once_with("cursor-agent", path="/tools")

    def test_unknown_coding_cli_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(CodingCliError) as raised:
                resolve_live_test_selection(
                    coding_cli="other",
                    model="any",
                    environment={},
                    test_config_path=Path(temporary) / "missing.json",
                )
        self.assertEqual(raised.exception.code, "coding_cli.unsupported")

    def test_empty_test_config_fields_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "literate.test.json"
            path.write_text(
                json.dumps({"coding_cli": "  ", "model": "ok"}),
                encoding="utf-8",
            )
            with self.assertRaises(CodingCliError) as raised:
                resolve_live_test_selection(
                    environment={},
                    test_config_path=path,
                )
        self.assertEqual(raised.exception.code, "coding_cli.test_selection_invalid")

    def test_remote_default_rejects_a_non_opencode_cli(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "missing.json"
            with self.assertRaises(CodingCliError) as raised:
                resolve_live_test_selection(
                    environment={
                        "CODING_CLI": "claude",
                        LIVE_MODEL_ENVIRONMENT: "any",
                        REMOTE_LIVE_GATE_ENVIRONMENT: "1",
                    },
                    test_config_path=missing,
                )
        self.assertEqual(raised.exception.code, "coding_cli.remote_prerequisite")
        self.assertIn("opencode", raised.exception.message)

    def test_remote_opencode_requires_openai_api_key_without_a_model_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "missing.json"
            with self.assertRaises(CodingCliError) as raised:
                resolve_live_test_selection(
                    environment={
                        "CODING_CLI": "opencode",
                        LIVE_MODEL_ENVIRONMENT: "openai/gpt-5",
                        REMOTE_LIVE_GATE_ENVIRONMENT: "1",
                    },
                    test_config_path=missing,
                )
        self.assertEqual(raised.exception.code, "coding_cli.remote_prerequisite")
        self.assertIn("OPENAI_API_KEY", raised.exception.message)

    def test_remote_cli_flag_may_select_another_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            selected = resolve_live_test_selection(
                coding_cli="claude",
                model="anthropic/claude",
                environment={REMOTE_LIVE_GATE_ENVIRONMENT: "1"},
                test_config_path=Path(temporary) / "missing.json",
            )
        self.assertEqual(selected.coding_cli, "claude")
        self.assertEqual(selected.coding_cli_provenance, "cli-flag")

    def test_remote_opencode_succeeds_when_the_key_is_present(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            selected = resolve_live_test_selection(
                environment={
                    "CODING_CLI": "opencode",
                    LIVE_MODEL_ENVIRONMENT: "openai/gpt-5",
                    REMOTE_LIVE_GATE_ENVIRONMENT: "1",
                    "OPENAI_API_KEY": "sk-test",
                },
                test_config_path=Path(temporary) / "missing.json",
            )
        self.assertEqual(selected.coding_cli, "opencode")
        overlay = apply_live_test_selection({}, selected)
        self.assertEqual(overlay["CODING_CLI"], "opencode")
        self.assertEqual(overlay[LIVE_MODEL_ENVIRONMENT], "openai/gpt-5")

    def test_require_opencode_rejects_json_claude_on_the_controller(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "literate.test.json"
            path.write_text(
                json.dumps({"coding_cli": "claude", "model": "any"}),
                encoding="utf-8",
            )
            with self.assertRaises(CodingCliError) as raised:
                resolve_live_test_selection(
                    environment={"CODING_CLI": "claude"},
                    test_config_path=path,
                    require_opencode=True,
                    ignore_environment_pins=True,
                )
        self.assertEqual(raised.exception.code, "coding_cli.remote_prerequisite")

    def test_posix_export_prefix_quotes_values(self) -> None:
        prefix = posix_export_prefix(
            remote_live_gate_overlay(
                LiveTestSelection(
                    "opencode",
                    "openai/gpt-5",
                    "test-config",
                    "test-config",
                )
            )
        )
        self.assertTrue(prefix.startswith("export "))
        self.assertIn(f"{REMOTE_LIVE_GATE_ENVIRONMENT}=1", prefix)
        self.assertIn("CODING_CLI=opencode", prefix)
        self.assertIn(f"{LIVE_MODEL_ENVIRONMENT}=openai/gpt-5", prefix)


class VerifyLiveModelResolvesTests(unittest.TestCase):
    """A pinned model is unvalidated until the preflight proves it resolves."""

    _SELECTION = LiveTestSelection(
        "opencode",
        "custom-provider/router/example-model",
        "test-config",
        "test-config",
    )

    def _runner(self, *, response=None, error=None):
        runner = mock.Mock()
        if error is not None:
            runner.run_json_task.side_effect = error
        else:
            result = mock.Mock()
            result.response = response
            runner.run_json_task.return_value = result
        return runner

    def test_passes_when_model_resolves_and_returns_preflight_json(self) -> None:
        runner = self._runner(
            response={"schema": "literate-ai/live-model-preflight@1", "ok": True}
        )
        with mock.patch(
            "literate_ai.adapters.models.coding_cli.CodingCliTaskRunner",
            return_value=runner,
        ):
            # No exception == pass.
            verify_live_model_resolves(self._SELECTION, environment={})
        self.assertEqual(runner.run_json_task.call_count, 1)
        _, kwargs = runner.run_json_task.call_args
        self.assertEqual(kwargs["model"], "custom-provider/router/example-model")

    def test_wrong_model_id_fails_closed_naming_model_and_cause(self) -> None:
        runner = self._runner(
            error=CodingCliError(
                "coding_cli.task_failed",
                "opencode model task failed: ProviderModelNotFoundError: Model "
                "not found: router/example-model.",
            )
        )
        with mock.patch(
            "literate_ai.adapters.models.coding_cli.CodingCliTaskRunner",
            return_value=runner,
        ):
            with self.assertRaises(CodingCliError) as raised:
                verify_live_model_resolves(
                    LiveTestSelection(
                        "opencode",
                        "router/example-model",
                        "test-config",
                        "test-config",
                    ),
                    environment={},
                )
        self.assertEqual(raised.exception.code, "coding_cli.model_unavailable")
        # The typed error names the offending model and carries the real cause.
        self.assertIn("router/example-model", raised.exception.message)
        self.assertIn("ProviderModelNotFoundError", raised.exception.message)
        self.assertIn("coding_cli.task_failed", raised.exception.message)

    def test_reachable_but_unusable_model_fails_closed(self) -> None:
        # The model answers but not with the expected preflight contract.
        runner = self._runner(response={"unexpected": "shape"})
        with mock.patch(
            "literate_ai.adapters.models.coding_cli.CodingCliTaskRunner",
            return_value=runner,
        ):
            with self.assertRaises(CodingCliError) as raised:
                verify_live_model_resolves(self._SELECTION, environment={})
        self.assertEqual(raised.exception.code, "coding_cli.model_unavailable")

    def test_preflight_applies_the_selection_overlay_to_the_runner_environment(
        self,
    ) -> None:
        captured: dict[str, object] = {}

        def factory(*, environment, timeout_seconds):
            captured["environment"] = dict(environment)
            runner = mock.Mock()
            result = mock.Mock()
            result.response = {
                "schema": "literate-ai/live-model-preflight@1",
                "ok": True,
            }
            runner.run_json_task.return_value = result
            return runner

        with mock.patch(
            "literate_ai.adapters.models.coding_cli.CodingCliTaskRunner",
            side_effect=factory,
        ):
            verify_live_model_resolves(self._SELECTION, environment={"PATH": "/tools"})
        self.assertEqual(captured["environment"]["CODING_CLI"], "opencode")
        self.assertEqual(
            captured["environment"][LIVE_MODEL_ENVIRONMENT],
            "custom-provider/router/example-model",
        )


if __name__ == "__main__":
    unittest.main()
