"""FINALIZE descriptors cannot substitute lock, component plans or package intent."""

import json
import unittest
from dataclasses import replace

import tests.support.fixtures_test_action_package_record as fixture_module
from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.action_finalize_record import FinalizeWorkerInput
from literate_ai.contracts import canonical_identity, canonical_json_bytes


class FinalizeWorkerInputTests(unittest.TestCase):
    def setUp(self):
        f = fixture_module.PackageWorkerInputTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.deadline = f.deadline
        self.package = f.fixture.result.root_integration.package_result
        self.value = FinalizeWorkerInput(
            f.fixture.fixture.lock,
            f.fixture.result.project_build_plan,
            f.value,
            canonical_identity("package-result"),
        )

    def admit(self, value=None, raw=None):
        raw = (value or self.value).to_bytes() if raw is None else raw
        return FinalizeWorkerInput.admit(raw, record_identity(raw), self.deadline)

    def test_exact_descriptors_roundtrip_without_claiming_package_proof(self):
        self.assertEqual(self.admit(), self.value)

    def test_other_lock_and_execution_plan_refuse(self):
        for field in ("component_lock", "project_plan"):
            doc = json.loads(self.value.to_bytes())
            if field == "component_lock":
                doc[field]["target_name"] = "foreign"
            else:
                doc[field]["execution_plan_identity"] = canonical_identity(
                    "foreign"
                ).uri
            with self.assertRaises(ActionWireError):
                self.admit(raw=canonical_json_bytes(doc))

    def test_missing_component_plan_refuses(self):
        self.assertGreater(len(self.value.project_plan.components), 1)
        with self.assertRaises(ActionWireError):
            self.admit(
                replace(
                    self.value,
                    project_plan=replace(
                        self.value.project_plan,
                        components=self.value.project_plan.components[:-1],
                    ),
                )
            )

    def test_reordered_and_duplicate_component_plans_refuse(self):
        doc = json.loads(self.value.to_bytes())
        components = doc["project_plan"]["components"]
        for changed in (list(reversed(components)), [*components, components[0]]):
            doc["project_plan"]["components"] = changed
            with self.assertRaises(ActionWireError):
                self.admit(raw=canonical_json_bytes(doc))

    def test_manifest_substitution_is_not_a_new_valid_finalize_intent(self):
        doc = json.loads(self.value.to_bytes())
        doc["project_plan"]["components"][0]["manifest"][
            "build_system_driver_identity"
        ] = canonical_identity("foreign").to_dict()
        with self.assertRaises(ActionWireError):
            self.admit(raw=canonical_json_bytes(doc))

    def test_missing_lock_authoring_cannot_reconstruct_selected_authority(self):
        doc = json.loads(self.value.to_bytes())
        doc["authorings"] = doc["authorings"][:-1]
        with self.assertRaises(ActionWireError):
            self.admit(raw=canonical_json_bytes(doc))

    def test_closed_canonical_envelope_and_identity(self):
        raw = self.value.to_bytes()
        for changed in (
            raw + b"\n",
            b"[]",
            canonical_json_bytes(json.loads(raw) | {"skip_acceptance": True}),
        ):
            with self.assertRaises(ActionWireError):
                self.admit(raw=changed)
        with self.assertRaises(ActionWireError):
            FinalizeWorkerInput.admit(raw, canonical_identity("foreign"), self.deadline)

    def test_full_plan_runtime_still_requires_link_proof_after_descriptor_admission(
        self,
    ):
        plans = self.value.project_plan.components
        changed_plan = replace(
            plans[0],
            request=replace(
                plans[0].request,
                language_runtime_identity=canonical_identity("foreign"),
            ),
        )
        changed = replace(
            self.value,
            project_plan=replace(
                self.value.project_plan, components=(changed_plan, *plans[1:])
            ),
        )
        self.assertEqual(self.admit(changed), changed)
