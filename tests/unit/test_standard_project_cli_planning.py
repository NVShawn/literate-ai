"""CLI coverage for Standard locked-project planning delegation."""

from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from literate_ai.cli import main
from literate_ai.contracts import (
    ContentIdentity,
    ModelScopeBinding,
)
from tests.unit.test_cli_locked_generation import (
    _TARGET,
    _generation_fixture,
    _write_lock,
)


class StandardProjectCliPlanningTests(unittest.TestCase):
    def test_locked_plan_uses_standard_service_without_running_coding_cli(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            component, flavors = _generation_fixture(root)
            lock = _write_lock(component, flavors)
            binary = root / "bin" / ("codex.cmd" if os.name == "nt" else "codex")
            binary.parent.mkdir()
            binary.write_bytes(
                b"@exit /b 97\r\n" if os.name == "nt" else b"#!/bin/sh\nexit 97\n"
            )
            binary.chmod(0o755)
            environment = {
                "CODING_CLI": "codex",
                "PATH": os.pathsep.join((str(binary.parent), os.environ["PATH"])),
            }

            with (
                mock.patch.dict(os.environ, environment),
                mock.patch("subprocess.run") as process,
            ):
                stdout = io.StringIO()
                stderr = io.StringIO()
                status = main(
                    (
                        "plan",
                        str(component),
                        "--target",
                        _TARGET,
                        "--flavor",
                        "+macos",
                        "--flavor",
                        "+python",
                        "--flavor-root",
                        str(flavors),
                        "--model",
                        "pipeline-model",
                    ),
                    stdout=stdout,
                    stderr=stderr,
                )

            process.assert_not_called()
            self.assertEqual(status, 0, stderr.getvalue())
            envelope = json.loads(stdout.getvalue())
            self.assertEqual(envelope["schema"], "literate-ai/cli-result@1")
            report = envelope["result"]
            self.assertEqual(report["schema"], "literate-ai/generation-plan@6")
            standard = report["standard_component_execution"]
            self.assertIsInstance(standard, dict)
            assert isinstance(standard, dict)
            self.assertEqual(
                ContentIdentity.from_dict(standard["component_lock_identity"]),
                lock.identity,
            )
            self.assertEqual(len(standard["generation_plans"]), len(lock.nodes))
            model_scopes = {
                item["component_revision"]: ModelScopeBinding.from_dict(item["binding"])
                for item in report["model_scopes"]
            }
            self.assertEqual(
                {
                    ContentIdentity.from_dict(item["generation_key"]["model_identity"])
                    for item in standard["generation_plans"]
                },
                {binding.identity for binding in model_scopes.values()},
            )
            self.assertEqual(
                {binding.explicit_model for binding in model_scopes.values()},
                {"pipeline-model"},
            )
            selected = report["coding_cli_selection"]["selected"]
            self.assertEqual(selected["coding_cli"], "codex")
            self.assertEqual(selected["executable"], str(binary.resolve()))
            self.assertEqual(
                selected["executable_identity"],
                "sha256:" + hashlib.sha256(binary.read_bytes()).hexdigest(),
            )
            self.assertEqual(selected["binding_state"], "exact-host-binding")

    def test_locked_plan_uses_a_logical_provider_when_no_cli_is_installed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            component, flavors = _generation_fixture(root)
            _write_lock(component, flavors)
            environment = {"CODING_CLI": "codex", "PATH": str(root / "empty-bin")}

            with mock.patch.dict(os.environ, environment, clear=True):
                stdout = io.StringIO()
                stderr = io.StringIO()
                status = main(
                    (
                        "plan",
                        str(component),
                        "--target",
                        _TARGET,
                        "--flavor",
                        "+macos",
                        "--flavor",
                        "+python",
                        "--flavor-root",
                        str(flavors),
                    ),
                    stdout=stdout,
                    stderr=stderr,
                )

            self.assertEqual(status, 0, stderr.getvalue())
            report = json.loads(stdout.getvalue())["result"]
            selected = report["coding_cli_selection"]["selected"]
            self.assertEqual(
                selected,
                {
                    "coding_cli": "codex",
                    "binding_state": "unbound-unavailable-at-plan-time",
                },
            )
            model_scopes = {
                ModelScopeBinding.from_dict(item["binding"])
                for item in report["model_scopes"]
            }
            self.assertEqual(
                {
                    ContentIdentity.from_dict(item["generation_key"]["model_identity"])
                    for item in report["standard_component_execution"][
                        "generation_plans"
                    ]
                },
                {binding.identity for binding in model_scopes},
            )
            self.assertEqual(
                {binding.explicit_model for binding in model_scopes}, {None}
            )

    def test_locked_plan_accepts_authenticated_inherited_session_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            component, flavors = _generation_fixture(root)
            _write_lock(component, flavors)
            provider_identity = "sha256:" + "a" * 64
            environment = {
                "LITAI_CODING_PROVIDER": "inherited-session",
                "LITAI_INHERITED_SESSION_PROVIDER_IDENTITY": provider_identity,
                "LITAI_INHERITED_SESSION_IDENTITY": "sha256:" + "b" * 64,
                "LITAI_INHERITED_SESSION_AUTH_KEY_ID": "ephemeral-key",
            }

            with mock.patch.dict(os.environ, environment, clear=True):
                stdout = io.StringIO()
                stderr = io.StringIO()
                status = main(
                    (
                        "plan",
                        str(component),
                        "--target",
                        _TARGET,
                        "--flavor",
                        "+macos",
                        "--flavor",
                        "+python",
                        "--flavor-root",
                        str(flavors),
                    ),
                    stdout=stdout,
                    stderr=stderr,
                )

            self.assertEqual(status, 0, stderr.getvalue())
            report = json.loads(stdout.getvalue())["result"]
            selected = report["coding_cli_selection"]["selected"]
            self.assertEqual(selected["coding_provider"], "inherited-session")
            self.assertEqual(selected["provider_identity"], provider_identity)
            bindings = {
                ModelScopeBinding.from_dict(item["binding"])
                for item in report["model_scopes"]
            }
            self.assertEqual(
                {binding.provider_id for binding in bindings}, {"inherited-session"}
            )
            self.assertEqual({binding.explicit_model for binding in bindings}, {None})


if __name__ == "__main__":
    unittest.main()
