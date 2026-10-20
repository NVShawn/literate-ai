"""Historical facts bind exact execution context without granting admission."""

import unittest
from dataclasses import replace

from literate_ai.contracts import canonical_identity
from literate_ai.contracts.retained_secondary_access import (
    RetainedReadOnlyView,
    RetainedSecondaryAccessPolicy,
)
from literate_ai.contracts.retained_secondary_evidence import (
    RetainedSecondaryAccessEvidence,
)
from literate_ai.contracts.retained_test_identity import RetainedSecondaryTestIdentity
from tests.support.fixtures_retained_project_sbom import retained_project_sbom_fixture


def evidence_fixture(plan, *, exports, commands):
    policy = plan.profile.secondary_access
    base = {"plan_identity": plan.identity.uri, "policy_identity": policy.identity.uri}
    inspections = [
        {
            **base,
            "schema": "literate-ai/retained-allocation-observation@1",
            "request_identity": canonical_identity(("request", n)).uri,
            "response_identity": canonical_identity(("response", n)).uri,
            "configuration_identity": canonical_identity("private config").uri,
            "allocation_identity": canonical_identity("instance").uri,
            "worker_identity": policy.worker_identity.uri,
            "remaining_seconds_at_response": 3600,
            "acceptance": False,
        }
        for n in range(2)
    ]
    event_base = {
        **base,
        "schema": "literate-ai/retained-readonly-view-observation@1",
        "allocation_observation_identity": canonical_identity(inspections[0]).uri,
    }
    events = []
    for origin in ("inputs", "exports"):
        for view in policy.views:
            if view.origin == origin:
                manifest = plan.manifest_identity if origin == "inputs" else exports
                events.append(
                    {
                        **event_base,
                        "event": "mounted",
                        "view_identity": canonical_identity(view.to_dict()).uri,
                        "manifest_binding": canonical_identity(
                            {
                                "view": view.to_dict(),
                                "manifest_identity": manifest.uri,
                            }
                        ).uri,
                        "readonly": True,
                        "nosuid": True,
                    }
                )
    for action in plan.profile.actions:
        if action.action_id in policy.action_ids:
            events.extend(
                (
                    {
                        **event_base,
                        "event": "activated",
                        "action_id": action.action_id,
                        "account_identity": policy.account.identity.uri,
                    },
                    {
                        **event_base,
                        "event": "deactivated",
                        "action_id": action.action_id,
                    },
                )
            )
    events.append({**event_base, "event": "closed", "cleanup_ok": True})
    return {
        **base,
        "schema": RetainedSecondaryAccessEvidence.SCHEMA,
        "worker_identity": policy.worker_identity.uri,
        "account_identity": policy.account.identity.uri,
        "command_observations": [v.uri for v in commands],
        "allocation_observations": inspections,
        "view_observations": events,
        "cleanup_ok": True,
    }


class RetainedSecondaryEvidenceTests(unittest.TestCase):
    def setUp(self):
        plan, _ = retained_project_sbom_fixture()
        policy = RetainedSecondaryAccessPolicy(
            plan.profile.worker_identity,
            RetainedSecondaryTestIdentity("litaitest", 2001, 2001, (2001,)),
            ("test",),
            (
                RetainedReadOnlyView("runtime", "inputs", "sdk"),
                RetainedReadOnlyView("products", "exports", "app"),
            ),
            ("work",),
        )
        self.plan = replace(
            plan, profile=replace(plan.profile, secondary_access=policy)
        )
        self.exports = canonical_identity("sealed build exports")
        self.commands = tuple(
            canonical_identity(a.to_dict()) for a in plan.profile.actions
        )
        self.value = evidence_fixture(
            self.plan, exports=self.exports, commands=self.commands
        )

    def bind(self, value):
        result = RetainedSecondaryAccessEvidence.from_dict(value)
        result.require_context(
            self.plan,
            export_manifest_identity=self.exports,
            command_observations=self.commands,
        )
        return result

    def test_canonical_immutable_roundtrip_and_exact_context(self):
        evidence = self.bind(self.value)
        self.assertEqual(evidence.identity, canonical_identity(self.value))
        self.assertEqual(RetainedSecondaryAccessEvidence(evidence.content), evidence)
        self.value["view_observations"].clear()
        copy = evidence.to_dict()
        copy["allocation_observations"].clear()
        self.assertEqual(len(evidence.to_dict()["view_observations"]), 5)
        self.assertEqual(len(evidence.to_dict()["allocation_observations"]), 2)
        with self.assertRaisesRegex(ValueError, "canonical-evidence"):
            RetainedSecondaryAccessEvidence(evidence.content + b"\n")

    def test_missing_or_failed_cleanup_cannot_become_evidence(self):
        for location in ("summary", "event", "missing"):
            value = self.bind(self.value).to_dict()
            if location == "summary":
                value["cleanup_ok"] = False
            elif location == "event":
                value["view_observations"][-1]["cleanup_ok"] = False
            else:
                value["view_observations"].pop()
            with self.subTest(location=location), self.assertRaises(ValueError):
                self.bind(value)

    def test_incomplete_reordered_or_extra_activation_refuses(self):
        for change in ("missing", "reordered", "extra"):
            value = self.bind(self.value).to_dict()
            events = value["view_observations"]
            if change == "missing":
                events.pop(3)
            elif change == "reordered":
                events[2], events[3] = events[3], events[2]
            else:
                events.insert(4, dict(events[2]))
            with (
                self.subTest(change=change),
                self.assertRaisesRegex(ValueError, "event-sequence"),
            ):
                self.bind(value)

    def test_allocation_replay_context_and_instance_drift_refuse(self):
        for key in (
            "request_identity",
            "allocation_identity",
            "plan_identity",
            "configuration_identity",
        ):
            value = self.bind(self.value).to_dict()
            item = value["allocation_observations"][1]
            item[key] = (
                value["allocation_observations"][0][key]
                if key == "request_identity"
                else canonical_identity("changed").uri
            )
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.bind(value)

    def test_unknown_fields_unobserved_allocation_and_writable_mount_refuse(self):
        for change in ("unknown", "allocation", "writable", "account"):
            value = self.bind(self.value).to_dict()
            if change == "unknown":
                value["disposable"] = True
            elif change == "allocation":
                value["view_observations"][0]["allocation_observation_identity"] = (
                    canonical_identity("unknown").uri
                )
            elif change == "writable":
                value["view_observations"][0]["readonly"] = False
            else:
                value["view_observations"][2]["account_identity"] = canonical_identity(
                    "other"
                ).uri
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.bind(value)

    def test_changed_manifest_command_or_plan_does_not_reuse_evidence(self):
        evidence = self.bind(self.value)
        for changes in (
            {"export_manifest_identity": canonical_identity("new exports")},
            {"command_observations": tuple(reversed(self.commands))},
        ):
            with self.assertRaises(ValueError):
                evidence.require_context(
                    self.plan,
                    **{
                        "export_manifest_identity": self.exports,
                        "command_observations": self.commands,
                        **changes,
                    },
                )
        with self.assertRaises(ValueError):
            evidence.require_context(
                replace(self.plan, project_id="different"),
                export_manifest_identity=self.exports,
                command_observations=self.commands,
            )
