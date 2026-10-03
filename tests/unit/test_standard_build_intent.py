"""Portable build-intent inputs retain exact source and dependency custody."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.lifecycle.standard_local import LocalStandardLifecycleError
from literate_ai.application.standard_build_intent import (
    create_standard_component_build_intent,
)
from literate_ai.application.standard_project_lifecycle import (
    StandardProjectLifecycleError,
)
from literate_ai.contracts.executable_components import (
    ArtifactExport,
    ComponentCommandContract,
    GeneratedSourceCandidate,
)
from literate_ai.contracts.native_sdks import native_sdk_consumer_build_identity
from tests.unit.test_standard_local_command_adapter import (
    _fixture,
    _identity,
    _provider_export,
    _python_copy_lifecycle,
)


class StandardBuildIntentTests(unittest.TestCase):
    def test_serialized_inputs_reconstruct_without_local_custody(self):
        with tempfile.TemporaryDirectory() as scratch:
            ports, _, candidate, expected = _python_copy_lifecycle(Path(scratch))
            contract = ports.contracts[candidate.component_revision.uri]
            wire = json.loads(
                json.dumps(
                    {
                        "candidate": candidate.to_dict(),
                        "contract": contract.to_dict(),
                        "provider": _provider_export("provider").to_dict(),
                        "package": _provider_export("package").to_dict(),
                    }
                )
            )
        del ports
        candidate = GeneratedSourceCandidate.from_dict(wire["candidate"])
        contract = ComponentCommandContract.from_dict(wire["contract"])
        result = create_standard_component_build_intent(
            candidate.component_generation_plan_identity,
            candidate,
            contract,
            (),
            (),
            (),
            dependency_resolution="none",
        )
        self.assertEqual(result.to_dict(), expected.to_dict())
        provider = ArtifactExport.from_dict(wire["provider"])
        package = ArtifactExport.from_dict(wire["package"])
        sdk = (_identity("sdk"),)
        for mode in ("none", "npm", "python"):
            with self.subTest(mode=mode):
                result = create_standard_component_build_intent(
                    candidate.component_generation_plan_identity,
                    candidate,
                    contract,
                    (provider,),
                    (package,),
                    sdk,
                    dependency_resolution=mode,
                )
                self.assertEqual(
                    result.provider_artifact_identities, (provider.identity,)
                )
                self.assertEqual(
                    result.package_artifact_identities, (package.identity,)
                )
                self.assertEqual(result.native_sdk_input_identities, sdk)
                self.assertEqual(
                    result.build_request.builder_id,
                    native_sdk_consumer_build_identity(
                        contract.locked_build_authority_identity, sdk
                    ).uri,
                )
                self.assertEqual(
                    "network-access" in result.build_request.requested_privileges,
                    mode == "npm",
                )
                self.assertNotEqual(result.identity, expected.identity)

    def test_foreign_candidate_plan_and_contract_are_refused(self):
        with tempfile.TemporaryDirectory() as scratch:
            ports, _, candidate, _ = _python_copy_lifecycle(Path(scratch))
            contract = ports.contracts[candidate.component_revision.uri]
            arguments = dict(
                generation_plan_identity=candidate.component_generation_plan_identity,
                candidate=candidate,
                contract=contract,
                providers=(),
                packages=(),
                native_sdk_inputs=(),
                dependency_resolution="none",
            )
            for changed in (
                {"generation_plan_identity": _identity("other")},
                {
                    "candidate": replace(
                        candidate, component_revision=_identity("other")
                    )
                },
                {"contract": replace(contract, component_revision=_identity("other"))},
                {"dependency_resolution": "unrecognized"},
            ):
                with (
                    self.subTest(changed=tuple(changed)),
                    self.assertRaises(StandardProjectLifecycleError) as raised,
                ):
                    create_standard_component_build_intent(**(arguments | changed))
                self.assertEqual(
                    raised.exception.code, "standard_lifecycle.intent_inputs_mismatch"
                )


class StandardBuildIntentAdmissionTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.ports, _, self.candidate, _ = _python_copy_lifecycle(Path(scratch.name))
        _, execution = _fixture()
        self.provider = _provider_export("new-provider")
        self.arguments = (
            execution,
            execution.generation_plans[0],
            self.candidate,
            (self.provider,),
            (),
        )
        self.before = self.registered()

    def registered(self):
        return tuple(
            dict(value)
            for value in (
                self.ports._intent_artifacts,
                self.ports._intent_package_artifacts,
                self.ports._library_consumer_bindings,
            )
        )

    def test_input_capture_does_not_register_and_exact_result_is_admitted(self):
        inputs = self.ports.build_intent_inputs(*self.arguments)
        intent = inputs.create()
        self.assertEqual(self.registered(), self.before)
        result = self.ports.accept_build_intent(*self.arguments, intent)
        self.assertEqual(result, intent)
        self.assertEqual(
            self.ports._intent_artifacts[result.identity.uri], (self.provider,)
        )
        self.assertEqual(self.ports.library_consumer_bindings(result), ())

    def test_substituted_result_cannot_register_any_intent_state(self):
        intent = self.ports.build_intent_inputs(*self.arguments).create()
        changed = replace(intent, package_artifact_identities=(_identity("foreign"),))
        with self.assertRaisesRegex(
            LocalStandardLifecycleError, "current local authority"
        ):
            self.ports.accept_build_intent(*self.arguments, changed)
        self.assertEqual(self.registered(), self.before)

    def test_sdk_change_after_capture_refuses_old_result_without_registration(self):
        intent = self.ports.build_intent_inputs(*self.arguments).create()
        with patch.object(
            self.ports, "_sdk_input_identities", return_value=(_identity("new-sdk"),)
        ):
            with self.assertRaisesRegex(
                LocalStandardLifecycleError, "current local authority"
            ):
                self.ports.accept_build_intent(*self.arguments, intent)
        self.assertEqual(self.registered(), self.before)

    def test_evidence_refusal_leaves_all_intent_maps_unchanged(self):
        intent = self.ports.build_intent_inputs(*self.arguments).create()
        with (
            patch.object(self.ports, "_evidence_recorder", object()),
            patch.object(
                self.ports,
                "_record_evidence",
                side_effect=RuntimeError("evidence refused"),
            ),
            self.assertRaisesRegex(RuntimeError, "evidence refused"),
        ):
            self.ports.accept_build_intent(*self.arguments, intent)
        self.assertEqual(self.registered(), self.before)
