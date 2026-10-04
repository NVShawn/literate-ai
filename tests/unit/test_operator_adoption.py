"""Public operator-adoption contracts and fail-closed authority behavior."""

from __future__ import annotations

import io
import json
import unittest
from unittest import mock

from literate_ai.cli import main
from literate_ai.contracts import canonical_identity
from literate_ai.contracts.operator_adoption import (
    ConversionAuthorityStage,
    ConversionAuthorityState,
)


def invoke(*arguments: str) -> tuple[int, dict[str, object]]:
    output = io.StringIO()
    errors = io.StringIO()
    status = main(arguments, stdout=output, stderr=errors)
    document = output.getvalue() if status == 0 else errors.getvalue()
    return status, json.loads(document)


def status_fixture() -> dict[str, object]:
    return {
        "schema": "literate-ai/operator-status@1",
        "view": "status",
        "project": {
            "root": "/project",
            "project_id": "example",
            "kind": "adopted",
            "authority": {"state": "current"},
            "conversion_authority": {"stage": "wrapped"},
            "locks": {"state": "current"},
            "test_receipt": {"state": "missing"},
        },
        "host": {
            "host_tools": {"ready": True},
            "coding_cli": {"name": "codex", "state": "authenticated"},
        },
        "next_verb": "litai project test-receipt run-retained CANDIDATE",
    }


class ConversionAuthorityContractTests(unittest.TestCase):
    def test_release_authority_claim_cannot_be_forged(self) -> None:
        state = ConversionAuthorityState(
            project_id="example",
            stage=ConversionAuthorityStage.WRAPPED,
            evidence_identities=(canonical_identity({"evidence": "wrapped"}),),
        )
        document = state.to_dict()
        document["release_authority"] = "specification"

        with self.assertRaisesRegex(ValueError, "claim is invalid"):
            ConversionAuthorityState.from_dict(document)


class OperatorCliTests(unittest.TestCase):
    def test_status_json_uses_the_common_cli_envelope(self) -> None:
        with mock.patch(
            "literate_ai.cli.operator.inspect_operator_status",
            return_value=status_fixture(),
        ):
            status, envelope = invoke("--json", "status", "--project", ".")

        self.assertEqual(status, 0, envelope)
        self.assertEqual(envelope["schema"], "literate-ai/cli-result@1")
        self.assertEqual(envelope["command"], "status")
        self.assertEqual(
            envelope["result"]["project"]["conversion_authority"]["stage"],
            "wrapped",
        )

    def test_doctor_reports_host_preflight_without_requiring_a_project(self) -> None:
        with mock.patch(
            "literate_ai.cli.operator.inspect_host_preflight",
            return_value={"schema": "literate-ai/operator-host-preflight@1"},
        ):
            status, envelope = invoke("--json", "doctor")

        self.assertEqual(status, 0, envelope)
        self.assertEqual(envelope["result"]["view"], "doctor")
        self.assertIsNone(envelope["result"]["project"])

    def test_onboard_apply_refuses_without_acknowledgement(self) -> None:
        with (
            mock.patch(
                "literate_ai.cli.operator._create_plan",
                return_value={
                    "path": "/new",
                    "writes": False,
                    "apply_supported": True,
                },
            ),
            mock.patch("literate_ai.cli.operator.init_project_from_args") as initialize,
        ):
            status, envelope = invoke("--json", "onboard", "create", "/new", "--apply")

        self.assertEqual(status, 2, envelope)
        self.assertEqual(
            envelope["error"]["code"],
            "operator.onboard_acknowledgement_required",
        )
        initialize.assert_not_called()


if __name__ == "__main__":
    unittest.main()
