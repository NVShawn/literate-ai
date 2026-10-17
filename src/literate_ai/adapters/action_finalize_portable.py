"""Verify portable FINALIZE observations against reopened source and private oracle."""

import json

from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.action_finalize_inputs import FinalizeRuntimeInputs
from literate_ai.adapters.lifecycle.standard_local import LocalIndependentAcceptanceCase
from literate_ai.contracts import (
    PORTABLE_APPLICATION_ENTRYPOINT_KIND,
    ContentIdentity,
    canonical_json_bytes,
)
from literate_ai.contracts.product_json import product_json_values_equal


def verify_portable_finalize_stages(value, evidence, records, *, prepared, oracle):
    """Refuse other runtimes; private composition must choose their own verifier."""

    def invalid():
        raise ActionWireError(
            "action_finalize.stage_proof_invalid", "portable FINALIZE proof refused"
        )

    def read(identity):
        identity = (
            ContentIdentity.parse_uri(identity)
            if isinstance(identity, str)
            else identity
        )
        raw = records[identity]
        if record_identity(raw) != identity:
            invalid()
        return json.loads(raw)

    def process(identity, phase):
        doc = read(identity)
        if (
            not isinstance(doc, dict)
            or set(doc)
            != {
                "schema",
                "phase",
                "plan_identity",
                "returncode",
                "stdout_identity",
                "stderr_identity",
            }
            or doc["schema"] != "literate-ai/local-process-observation@1"
            or doc["phase"] != phase
            or doc["plan_identity"] != evidence.package_plan.identity.uri
            or type(doc["returncode"]) is not int
            or doc["returncode"] != 0
        ):
            invalid()
        stdout, stderr = read(doc["stdout_identity"]), read(doc["stderr_identity"])
        if not isinstance(stdout, str) or not isinstance(stderr, str):
            invalid()
        return stdout

    try:
        if (
            not isinstance(prepared, FinalizeRuntimeInputs)
            or prepared.intent != value
            or prepared.package != evidence.package_result
            or len(evidence.package_plan.entrypoints) != 1
            or evidence.package_plan.entrypoints[0].kind
            != PORTABLE_APPLICATION_ENTRYPOINT_KIND
        ):
            invalid()
        prepared.package_tree.require_unchanged()
        root = prepared.root_build
        source = prepared.source_trees.evidence(root.candidate.tree_identity)
        if source.identity != root.source_custody_identity:
            invalid()
        suite = source.generated_test_suite
        tests = json.loads(
            process(
                evidence.root_generated_integration_test_identity,
                "packaged-root-generated-test",
            )
        )
        if (
            not isinstance(tests, dict)
            or set(tests) != {"schema", "cases"}
            or tests["schema"] != "literate-ai/generated-test-results@1"
            or not isinstance(tests["cases"], list)
            or len(tests["cases"]) != len(suite.case_ids)
            or any(
                not isinstance(case, dict)
                or set(case) != {"case_id", "outcome"}
                or case["outcome"] != "passed"
                for case in tests["cases"]
            )
            or {case["case_id"] for case in tests["cases"]} != set(suite.case_ids)
        ):
            invalid()
        if not process(
            evidence.packaged_execution_identity, "packaged-project-execution"
        ).strip():
            invalid()
        oracle_identity = oracle.identity
        cases = tuple(oracle.cases(value.component_lock))
        if (
            not isinstance(oracle_identity, ContentIdentity)
            or not cases
            or any(
                not isinstance(case, LocalIndependentAcceptanceCase) for case in cases
            )
            or len({case.case_id for case in cases}) != len(cases)
            or oracle.identity != oracle_identity
        ):
            invalid()
        acceptance = read(evidence.independent_acceptance_identity)
        observations = []
        for observation, case in zip(acceptance["observations"], cases, strict=True):
            if set(observation) != {
                "case_id",
                "case_identity",
                "stdout_identity",
                "stderr_identity",
                "result_identity",
            }:
                invalid()
            stdout, stderr = (
                read(observation["stdout_identity"]),
                read(observation["stderr_identity"]),
            )
            result = read(observation["result_identity"])
            if (
                observation["case_id"] != case.case_id
                or observation["case_identity"] != case.identity.uri
                or not isinstance(stdout, str)
                or not isinstance(stderr, str)
                or not product_json_values_equal(json.loads(stdout), result)
                or not product_json_values_equal(
                    result, json.loads(case.expected_result_document)
                )
            ):
                invalid()
            observations.append(observation)
        expected = {
            "schema": "literate-ai/local-independent-project-acceptance@1",
            "package_plan_identity": evidence.package_plan.identity.uri,
            "package_result_identity": evidence.package_result.identity.uri,
            "root_integration_test_identity": (
                evidence.root_generated_integration_test_identity.uri
            ),
            "packaged_execution_identity": evidence.packaged_execution_identity.uri,
            "oracle_identity": oracle_identity.uri,
            "observations": observations,
        }
        if canonical_json_bytes(acceptance) != canonical_json_bytes(expected):
            invalid()
        prepared.package_tree.require_unchanged()
        if oracle.identity != oracle_identity:
            invalid()
    except (
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        UnicodeError,
        RecursionError,
    ) as exc:
        raise ActionWireError(
            "action_finalize.stage_proof_invalid", "portable FINALIZE proof refused"
        ) from exc
