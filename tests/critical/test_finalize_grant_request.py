"""A receiver cannot get an operator to sign a grant for anything but the plan."""

import json
import unittest
from dataclasses import replace

import tests.support.fixtures_test_action_package_record as fixture_module
from literate_ai.adapters.action_dispatch_wire import ActionWireError
from literate_ai.adapters.action_finalize_authority import finalize_execution_request
from literate_ai.adapters.action_finalize_grant_request import (
    decode_finalize_grant_request,
    encode_finalize_grant_request,
)
from literate_ai.adapters.action_finalize_record import FinalizeWorkerInput
from literate_ai.contracts import canonical_identity, canonical_json_bytes


class FinalizeGrantRequestTests(unittest.TestCase):
    def setUp(self):
        f = fixture_module.PackageWorkerInputTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.value = FinalizeWorkerInput(
            f.fixture.fixture.lock,
            f.fixture.result.project_build_plan,
            f.value,
            canonical_identity("package-result"),
        )

    def test_described_request_must_reconstruct_from_the_planned_intent(self):
        runtime = canonical_identity("measured private runtime")
        request = finalize_execution_request(self.value, runtime)
        exact = encode_finalize_grant_request(self.value, request)
        self.assertEqual(decode_finalize_grant_request(exact, self.value), request)
        other = replace(self.value, package_result_identity=canonical_identity("x"))
        document = json.loads(exact)
        substitutions = {
            "another input": encode_finalize_grant_request(
                other, finalize_execution_request(other, runtime)
            ),
            "wider privileges": canonical_json_bytes(
                document
                | {
                    "request": replace(
                        request, requested_privileges=("execute-project", "network")
                    ).to_dict()
                }
            ),
            "builder for another runtime": canonical_json_bytes(
                document
                | {
                    "request": replace(
                        request, builder_id="standard-finalize:sha256:" + "0" * 64
                    ).to_dict()
                }
            ),
            "noncanonical bytes": json.dumps(document, indent=1).encode(),
        }
        for name, content in substitutions.items():
            with self.subTest(name), self.assertRaises(ActionWireError) as refused:
                decode_finalize_grant_request(content, self.value)
            self.assertEqual(
                refused.exception.code, "action_finalize.grant_request_invalid"
            )


if __name__ == "__main__":
    unittest.main()
