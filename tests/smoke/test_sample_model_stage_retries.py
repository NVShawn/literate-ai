"""Recognized generated JavaScript closure errors keep bounded candidate repair."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from literate_ai.adapters.models import CodingCliError
from literate_ai.application import GenerationStatus
from literate_ai.application.orchestrator import GenerationFailure
from tests.conformance.support import sample_runner

_BUNDLE_MISSING = "coding_cli.generated_javascript_bundle_module_missing"


def _failure(code=_BUNDLE_MISSING, *, built=False):
    run = SimpleNamespace(
        run_id="generation:javascript-closure",
        status=GenerationStatus.FAILED,
        events=[
            SimpleNamespace(event_type="model-stage-failed", stage_id="generate"),
            SimpleNamespace(
                event_type="run-failed",
                stage_id=None,
                data={"code": "generation.model-stage-failed"},
            ),
        ],
        step=lambda name: object() if built and name == "build" else None,
    )
    failure = GenerationFailure("generation.model-stage-failed", "rejected", run)
    failure.__cause__ = CodingCliError(code, "generated bundle omits a local module")
    return failure


def _run(root):
    return sample_runner._run_host_e2e(
        root,
        root,
        "fixture",
        None,
        None,
        None,
        None,
        variant=SimpleNamespace(variant_id="javascript"),
        source_generator=object(),
        allow_host_execution=False,
    )


class SampleModelStageRetryTests(unittest.TestCase):
    def test_missing_bundle_module_gets_fresh_attempt_and_rejection_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(
                sample_runner,
                "_run_host_e2e_once",
                side_effect=[_failure(), {"accepted": True}],
            ) as once:
                result = _run(root)
            self.assertTrue(result["accepted"])
            self.assertEqual(result["generation_candidate_attempt_count"], 2)
            self.assertEqual(once.call_count, 2)
            attempts = [call.args[1] for call in once.call_args_list]
            self.assertEqual([path.name for path in attempts], ["01", "02"])
            self.assertNotEqual(*attempts)
            self.assertTrue(
                all(
                    not call.kwargs["allow_host_execution"]
                    for call in once.call_args_list
                )
            )
            rejection = result["rejected_generation_candidates"][0]
            self.assertEqual(
                once.call_args_list[1].kwargs["candidate_feedback"],
                result["rejected_generation_candidates"],
            )
            self.assertEqual(rejection["contract_failure_code"], _BUNDLE_MISSING)
            self.assertEqual(
                rejection["rejection_kind"], "generated-source-contract-rejected"
            )
            retained = root / "candidate-attempts/javascript/rejections-01.json"
            self.assertIn(_BUNDLE_MISSING, json.dumps(json.loads(retained.read_text())))

    def test_repeated_bundle_failures_exhaust_three_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(
                sample_runner, "_run_host_e2e_once", side_effect=_failure()
            ) as once:
                with self.assertRaises(GenerationFailure) as caught:
                    _run(root)
            self.assertEqual(
                caught.exception.code,
                "generation.generated-candidate-attempts-exhausted",
            )
            self.assertEqual(once.call_count, 3)
            self.assertTrue(
                (root / "candidate-attempts/javascript/rejections-03.json").is_file()
            )

    def test_authentication_and_post_build_failures_remain_terminal(self):
        for failure in (
            _failure("coding_cli.authentication_failed"),
            _failure(built=True),
        ):
            with (
                self.subTest(failure=failure),
                tempfile.TemporaryDirectory() as directory,
            ):
                with mock.patch.object(
                    sample_runner, "_run_host_e2e_once", side_effect=failure
                ) as once:
                    with self.assertRaises(GenerationFailure) as caught:
                        _run(Path(directory))
                self.assertIs(caught.exception, failure)
                self.assertEqual(once.call_count, 1)


if __name__ == "__main__":
    unittest.main()
