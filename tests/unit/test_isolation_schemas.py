"""Public isolation wire contracts preserve the non-authorizing report boundary."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import unittest
from dataclasses import replace

from jsonschema import Draft202012Validator, ValidationError
from referencing import Registry, Resource

from literate_ai.schema_catalog import schema_path, verify_schema_catalog
from literate_ai.security import (
    ContainmentControl,
    IsolationDecision,
    IsolationEvidenceAuthentication,
    IsolationLevel,
    IsolationObservation,
    IsolationPolicy,
    IsolationRequest,
    evaluate_isolation_policy,
)
from tests.support.fixtures_test_isolation_contracts import _observation, _policy, _request


class IsolationSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = json.loads(
            schema_path("isolation.schema.json", catalog_version="v2").read_bytes()
        )
        Draft202012Validator.check_schema(cls.document)
        cls.registry = Registry().with_resource(
            cls.document["$id"], Resource.from_contents(cls.document)
        )
        cls.validator = Draft202012Validator(cls.document, registry=cls.registry)

    def assert_wire(self, record):
        wire = record.to_dict()
        self.validator.validate(wire)
        self.assertEqual(type(record).from_dict(wire), record)

    def test_public_security_import_succeeds_in_a_fresh_interpreter(self):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from literate_ai.security import "
                "BuildAuthorization, IsolationRequest; "
                "from literate_ai.security.isolation import "
                "IsolationRequest as direct; "
                "assert IsolationRequest is direct",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_current_catalog_registers_all_four_public_writers(self):
        verify_schema_catalog("v2")
        compatibility = json.loads(
            schema_path("compatibility.json", catalog_version="v2").read_bytes()
        )
        for contract in (
            IsolationPolicy,
            IsolationRequest,
            IsolationObservation,
            IsolationDecision,
        ):
            self.assertIn(contract.SCHEMA, compatibility["write_contracts"])

    def test_public_wire_round_trips_and_recomputes_without_authentication(self):
        request, policy, observation = _request(), _policy(), _observation()
        decision = evaluate_isolation_policy(request, policy, observation)
        for record in (request, policy, observation, decision):
            with self.subTest(record=record.SCHEMA):
                self.assert_wire(record)
        restored = IsolationDecision.from_dict(decision.to_dict())
        restored.require_exact_recomputation(request, policy, observation)
        self.assertIs(
            restored.authentication,
            IsolationEvidenceAuthentication.UNAUTHENTICATED_LOCAL,
        )

    def test_supported_negative_reports_remain_serializable(self):
        request, policy = _request(), _policy()
        cases = (
            (request, policy, None),
            (request, policy, _observation(complete=False)),
            (request, policy, _observation(target_os="windows")),
            (
                request,
                policy,
                replace(_observation(), policy_identity="sha256:" + "9" * 64),
            ),
            (
                request,
                policy,
                _observation(level=IsolationLevel.PROCESS_LIMITED),
            ),
            (
                request,
                policy,
                replace(
                    _observation(),
                    enforced_controls=tuple(
                        control
                        for control in _observation().enforced_controls
                        if control is not ContainmentControl.NETWORK_DENIED
                    ),
                ),
            ),
            (
                _request(IsolationLevel.HOST_YOLO),
                _policy(IsolationLevel.HOST_YOLO, controls=()),
                None,
            ),
            (
                _request(IsolationLevel.HOST_YOLO),
                _policy(IsolationLevel.HOST_YOLO, controls=(), allow_host_yolo=True),
                None,
            ),
        )
        for req, pol, obs in cases:
            decision = evaluate_isolation_policy(req, pol, obs)
            with self.subTest(reason=decision.reason_code):
                self.assertFalse(decision.reported_sufficient)
                self.assert_wire(decision)

    def test_wire_rejects_unknown_fields_versions_and_self_authentication(self):
        decision = evaluate_isolation_policy(_request(), _policy(), _observation())
        for record in (_request(), _policy(), _observation(), decision):
            wire = record.to_dict()
            changed = copy.deepcopy(wire)
            changed["schema"] = wire["schema"].replace("@1", "@2")
            for mutation in (changed, {**wire, "authorized": True}):
                with self.subTest(schema=record.SCHEMA, mutation=mutation):
                    with self.assertRaises(ValidationError):
                        self.validator.validate(mutation)
                    with self.assertRaises(ValueError):
                        type(record).from_dict(mutation)
        for authentication in ("authenticated", "signed", True):
            changed = {**decision.to_dict(), "authentication": authentication}
            with self.subTest(authentication=authentication):
                with self.assertRaises(ValidationError):
                    self.validator.validate(changed)
                with self.assertRaises(ValueError):
                    IsolationDecision.from_dict(changed)

    def test_wire_rejects_contradictory_report_fields_and_controls(self):
        decision = evaluate_isolation_policy(_request(), _policy(), _observation())
        changes = (
            {"reason_code": "containment.backend-unavailable"},
            {"achieved_level": "host-yolo"},
            {"observation_identity": None},
            {"required_controls": []},
            {"missing_controls": ["network-denied"]},
            {"status": "authorized"},
        )
        for change in changes:
            wire = {**decision.to_dict(), **change}
            with self.subTest(change=change):
                with self.assertRaises(ValidationError):
                    self.validator.validate(wire)
                with self.assertRaises((ValueError, TypeError)):
                    IsolationDecision.from_dict(wire)
        for controls in ([], ["invented-control"], ["network-denied"] * 2):
            wire = {**_observation().to_dict(), "enforced_controls": controls}
            with self.subTest(controls=controls):
                with self.assertRaises(ValidationError):
                    self.validator.validate(wire)
                with self.assertRaises(ValueError):
                    IsolationObservation.from_dict(wire)

    def test_typed_decoder_enforces_canonical_order_and_unique_stage_rules(self):
        # JSON Schema handles structure; the typed boundary additionally enforces
        # order and uniqueness by stage, as the published schema documents.
        wire = _observation().to_dict()
        wire["enforced_controls"].reverse()
        self.validator.validate(wire)
        with self.assertRaises(ValueError):
            IsolationObservation.from_dict(wire)
        policy = _policy().to_dict()
        second_rule = copy.deepcopy(policy["rules"][0])
        second_rule["minimum_level"] = "vm-isolated"
        policy["rules"].append(second_rule)
        self.validator.validate(policy)
        with self.assertRaises(ValueError):
            IsolationPolicy.from_dict(policy)
