"""Capacity admission must not invent capacity, identity, or cleanup authority."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from literate_ai.application.worker_capacity import (
    CapacityDecision,
    CapacityHealth,
    assess_worker_capacity,
)
from literate_ai.contracts import (
    CapacityMetric,
    CapacityProbeStatus,
    ContractValidationError,
    QuotaCapacitySample,
    StorageCapacitySample,
    StorageRoleCapacityPolicy,
    WorkerCapacityObservation,
    WorkerCapacityPolicy,
    canonical_identity,
)
from literate_ai.schema_catalog import verify_schema_catalog

ROLES = ("cache", "output", "temp", "workspace")
JOB = canonical_identity({"job": "capacity-fixture"})
NOW = 100_000
MEASURED = CapacityProbeStatus.MEASURED
NOT_APPLICABLE = CapacityMetric(CapacityProbeStatus.NOT_APPLICABLE)


def policy(**role_changes):
    return WorkerCapacityPolicy(
        tuple(
            replace(
                StorageRoleCapacityPolicy(role, role, 0, 0, 0, 0, 0, True, False),
                **role_changes,
            )
            for role in ROLES
        ),
        True,
        30_000,
        5_000,
        2,
        0,
        canonical_identity({"private_role_bindings": "fixture"}),
    )


def observation(selected, *, available=1000, total=2000, separate=False):
    return WorkerCapacityObservation(
        "worker.fixture",
        selected.identity,
        JOB,
        "linux",
        NOW - 100,
        NOW - 1,
        NOW + 10_000,
        tuple(
            StorageCapacitySample(
                role,
                role if separate else "volume-1",
                total,
                CapacityMetric(MEASURED, available),
                NOT_APPLICABLE,
                (QuotaCapacitySample("no-quota", NOT_APPLICABLE, NOT_APPLICABLE),),
            )
            for role in ROLES
        ),
    )


def assess(selected, observed=None, *, attempt=0, **options):
    return assess_worker_capacity(
        selected,
        observation(selected) if observed is None else observed,
        worker_id="worker.fixture",
        job_identity=JOB,
        now_ms=NOW,
        attempt=attempt,
        **options,
    )


def change_sample(observed, role, **changes):
    return replace(
        observed,
        samples=tuple(
            replace(sample, **changes) if sample.role == role else sample
            for sample in observed.samples
        ),
    )


class WorkerCapacityTests(unittest.TestCase):
    def test_shared_volume_combines_peak_demand_and_retains_one_reserve(self):
        selected = policy(additional_bytes=20, reserve_bytes=20)
        for available, expected in (
            (99, CapacityDecision.HOLD),
            (100, CapacityDecision.PROCEED),
        ):
            with self.subTest(available=available):
                result = assess(selected, observation(selected, available=available))
                self.assertEqual(result.decision, expected)
                finding = next(
                    item for item in result.findings if item.resource == "bytes"
                )
                self.assertEqual(finding.minimum, 100)
                self.assertEqual(finding.roles, ROLES)
                self.assertEqual(finding.to_dict()["deficit"], max(0, 100 - available))

    def test_separate_volumes_never_pool_free_space(self):
        selected = policy(additional_bytes=20, reserve_bytes=10)
        observed = change_sample(
            observation(selected, separate=True),
            "temp",
            available_bytes=CapacityMetric(MEASURED, 29),
        )
        result = assess(selected, observed)
        self.assertEqual(result.decision, CapacityDecision.HOLD)
        deficits = [
            item for item in result.findings if item.health is CapacityHealth.CRITICAL
        ]
        self.assertEqual(
            [(item.roles, item.minimum) for item in deficits], [(("temp",), 30)]
        )
        self.assertEqual(
            assess(
                selected,
                change_sample(
                    observed, "temp", available_bytes=CapacityMetric(MEASURED, 30)
                ),
            ).decision,
            CapacityDecision.PROCEED,
        )

    def test_free_percentage_is_a_rounded_up_post_allocation_floor(self):
        selected = policy(minimum_free_basis_points=1000)
        selected = replace(
            selected,
            roles=tuple(
                replace(item, additional_bytes=150) if item.role == "temp" else item
                for item in selected.roles
            ),
        )
        observed = observation(selected, available=200, total=1000)
        result = assess(selected, observed)
        self.assertEqual(result.decision, CapacityDecision.HOLD)
        self.assertEqual(
            next(f.minimum for f in result.findings if f.resource == "bytes"), 250
        )
        selected = policy(minimum_free_basis_points=1)
        result = assess(selected, observation(selected, available=1, total=10001))
        self.assertEqual(
            next(f.minimum for f in result.findings if f.resource == "bytes"), 2
        )
        self.assertEqual(result.decision, CapacityDecision.HOLD)

    def test_same_quota_domain_combines_demand_across_physical_volumes(self):
        selected = policy(additional_bytes=20, reserve_bytes=10)
        observed = observation(selected, separate=True)
        observed = replace(
            observed,
            samples=tuple(
                replace(
                    sample,
                    quotas=(
                        QuotaCapacitySample(
                            "shared-quota", CapacityMetric(MEASURED, 89), NOT_APPLICABLE
                        ),
                    ),
                )
                for sample in observed.samples
            ),
        )
        result = assess(selected, observed)
        quota = next(item for item in result.findings if item.resource == "quota")
        self.assertEqual((quota.minimum, quota.available), (90, 89))
        self.assertEqual(result.decision, CapacityDecision.HOLD)
        independent = replace(
            observed,
            samples=tuple(
                replace(sample, quotas=(replace(sample.quotas[0], domain=sample.role),))
                for sample in observed.samples
            ),
        )
        self.assertEqual(
            assess(selected, independent).decision, CapacityDecision.PROCEED
        )

    def test_quota_exhaustion_holds_even_with_healthy_filesystem_capacity(self):
        selected = policy(additional_bytes=1)
        observed = change_sample(
            observation(selected),
            "cache",
            quotas=(
                QuotaCapacitySample(
                    "cache-quota", CapacityMetric(MEASURED, 0), NOT_APPLICABLE
                ),
            ),
        )
        result = assess(selected, observed)
        self.assertEqual(result.health, CapacityHealth.CRITICAL)
        self.assertEqual(result.decision, CapacityDecision.HOLD)
        self.assertTrue(
            any(
                item.resource == "bytes" and item.health is CapacityHealth.HEALTHY
                for item in result.findings
            )
        )

    def test_inode_demands_combine_and_unknown_inodes_cannot_admit_them(self):
        selected = policy(additional_inodes=10, reserve_inodes=5)
        observed = observation(selected)
        self.assertEqual(
            assess(selected, observed).decision, CapacityDecision.RETRY_AFTER_RECHECK
        )
        for available, expected in (
            (44, CapacityDecision.HOLD),
            (45, CapacityDecision.PROCEED),
        ):
            with self.subTest(available=available):
                measured = replace(
                    observed,
                    samples=tuple(
                        replace(
                            sample, available_inodes=CapacityMetric(MEASURED, available)
                        )
                        for sample in observed.samples
                    ),
                )
                result = assess(selected, measured)
                self.assertEqual(result.decision, expected)
                self.assertEqual(
                    next(
                        item.minimum
                        for item in result.findings
                        if item.resource == "inodes"
                    ),
                    45,
                )

    def test_overlapping_quota_domains_each_constrain_the_full_bound_demand(self):
        selected = policy(additional_bytes=20, reserve_bytes=10)
        observed = observation(selected)
        for available, decision in (
            (89, CapacityDecision.HOLD),
            (90, CapacityDecision.PROCEED),
        ):
            changed = replace(
                observed,
                samples=tuple(
                    replace(
                        sample,
                        quotas=(
                            QuotaCapacitySample(
                                "group-" + sample.role,
                                CapacityMetric(MEASURED, 30),
                                NOT_APPLICABLE,
                            ),
                            QuotaCapacitySample(
                                "user",
                                CapacityMetric(MEASURED, available),
                                NOT_APPLICABLE,
                            ),
                        ),
                    )
                    for sample in observed.samples
                ),
            )
            result = assess(selected, changed)
            self.assertEqual(result.decision, decision)
            quotas = [f for f in result.findings if f.resource == "quota"]
            self.assertEqual(sorted(f.minimum for f in quotas), [30, 30, 30, 30, 90])

    def test_quota_inodes_cannot_be_masked_by_free_physical_inodes(self):
        selected = policy(additional_inodes=3, reserve_inodes=2)
        observed = observation(selected)
        for available, decision in (
            (13, CapacityDecision.HOLD),
            (14, CapacityDecision.PROCEED),
        ):
            changed = replace(
                observed,
                samples=tuple(
                    replace(
                        sample,
                        available_inodes=CapacityMetric(MEASURED, 1000),
                        quotas=(
                            QuotaCapacitySample(
                                "user",
                                NOT_APPLICABLE,
                                CapacityMetric(MEASURED, available),
                            ),
                        ),
                    )
                    for sample in observed.samples
                ),
            )
            result = assess(selected, changed)
            self.assertEqual(result.decision, decision)
            self.assertEqual(
                next(
                    f.minimum for f in result.findings if f.resource == "quota-inodes"
                ),
                14,
            )

    def test_missing_shared_quota_sample_cannot_erase_its_roles_demand(self):
        selected = policy(additional_bytes=20, reserve_bytes=10, require_quota=False)
        observed = observation(selected)
        changed = replace(
            observed,
            samples=tuple(
                replace(
                    sample,
                    quotas=(
                        QuotaCapacitySample(
                            "user",
                            CapacityMetric(MEASURED, 89)
                            if sample.role == "cache"
                            else CapacityMetric(CapacityProbeStatus.DENIED),
                            NOT_APPLICABLE,
                        ),
                    ),
                )
                for sample in observed.samples
            ),
        )
        self.assertEqual(assess(selected, changed).decision, CapacityDecision.HOLD)
        budget = next(
            f
            for f in assess(selected, changed).findings
            if f.minimum is not None and f.resource == "quota"
        )
        self.assertEqual((budget.minimum, budget.roles), (90, ROLES))

    def test_conflicting_shared_quota_applicability_requires_recheck(self):
        selected = policy(require_quota=False)
        observed = observation(selected)
        changed = change_sample(
            observed,
            "cache",
            quotas=(
                QuotaCapacitySample(
                    "no-quota", CapacityMetric(MEASURED, 1000), NOT_APPLICABLE
                ),
            ),
        )
        result = assess(selected, changed)
        self.assertEqual(result.decision, CapacityDecision.RETRY_AFTER_RECHECK)
        self.assertIn(
            "conflicting-quota-applicability", [f.reason for f in result.findings]
        )
        self.assertEqual(
            assess(selected, changed, attempt=2).decision, CapacityDecision.HOLD
        )

    def test_required_probe_failures_remain_distinct_and_retries_are_bounded(self):
        selected = policy()
        for status in CapacityProbeStatus:
            if status is MEASURED:
                continue
            with self.subTest(status=status):
                observed = change_sample(
                    observation(selected),
                    "temp",
                    available_bytes=CapacityMetric(status),
                )
                result = assess(selected, observed)
                self.assertEqual(result.health, CapacityHealth.UNKNOWN)
                self.assertEqual(result.decision, CapacityDecision.RETRY_AFTER_RECHECK)
                self.assertIn(
                    f"probe-{status.value}", [item.reason for item in result.findings]
                )
                self.assertEqual(
                    assess(selected, observed, attempt=2).decision,
                    CapacityDecision.HOLD,
                )
                self.assertEqual(
                    assess(selected, observed, attempt=3).decision,
                    CapacityDecision.HOLD,
                )

    def test_optional_unknown_is_visible_without_blocking_otherwise_justified_work(
        self,
    ):
        selected = policy(require_quota=False)
        observed = change_sample(
            observation(selected),
            "cache",
            quotas=(
                QuotaCapacitySample(
                    "denied", CapacityMetric(CapacityProbeStatus.DENIED), NOT_APPLICABLE
                ),
            ),
        )
        result = assess(selected, observed)
        self.assertEqual(
            (result.health, result.decision),
            (CapacityHealth.UNKNOWN, CapacityDecision.PROCEED),
        )
        self.assertEqual(
            assess(
                policy(), replace(observed, policy_identity=policy().identity)
            ).decision,
            CapacityDecision.RETRY_AFTER_RECHECK,
        )

    def test_known_no_quota_and_inapplicable_optional_inodes_are_not_failed_probes(
        self,
    ):
        selected = policy()
        for family in ("linux", "macos", "windows"):
            with self.subTest(family=family):
                result = assess(
                    selected, replace(observation(selected), os_family=family)
                )
                self.assertEqual(
                    (result.health, result.decision),
                    (CapacityHealth.HEALTHY, CapacityDecision.PROCEED),
                )

    def test_warning_does_not_hold_and_read_only_work_does_not_claim_healthy_capacity(
        self,
    ):
        selected = replace(policy(reserve_bytes=100), warning_headroom_bytes=20)
        result = assess(selected, observation(selected, available=119))
        self.assertEqual(
            (result.health, result.decision),
            (CapacityHealth.WARNING, CapacityDecision.PROCEED),
        )
        readonly = replace(selected, write_heavy=False)
        result = assess(readonly, observation(readonly, available=1))
        self.assertEqual(
            (result.health, result.decision),
            (CapacityHealth.CRITICAL, CapacityDecision.PROCEED),
        )
        with self.assertRaises(ContractValidationError):
            replace(policy(additional_bytes=1), write_heavy=False)

    def test_read_only_admission_still_requires_policy_required_measurements(self):
        selected = replace(policy(), write_heavy=False)
        observed = change_sample(
            observation(selected),
            "temp",
            quotas=(
                QuotaCapacitySample(
                    "denied", CapacityMetric(CapacityProbeStatus.DENIED), NOT_APPLICABLE
                ),
            ),
        )
        self.assertEqual(
            assess(selected, observed).decision, CapacityDecision.RETRY_AFTER_RECHECK
        )
        self.assertEqual(
            assess(selected, observed, attempt=2).decision, CapacityDecision.HOLD
        )

    def test_expiry_boundary_requires_fresh_measurement_not_an_attempt_reset(self):
        selected = policy()
        observed = replace(observation(selected), expires_at_ms=NOW)
        self.assertEqual(
            assess(selected, observed).decision, CapacityDecision.RETRY_AFTER_RECHECK
        )
        self.assertEqual(
            assess(selected, observed, attempt=selected.maximum_retries).decision,
            CapacityDecision.HOLD,
        )
        fresh = replace(observed, expires_at_ms=NOW + 1)
        self.assertEqual(
            assess(selected, fresh, attempt=selected.maximum_retries).decision,
            CapacityDecision.PROCEED,
        )

    def test_identity_clock_window_and_expiry_mismatches_always_hold(self):
        selected = policy()
        observed = observation(selected)
        changes = (
            ({"worker_id": "other"}, "worker-mismatch"),
            ({"policy_identity": canonical_identity("other")}, "policy-mismatch"),
            ({"job_identity": canonical_identity("other")}, "job-mismatch"),
            ({"job_identity": None}, "job-mismatch"),
            ({"completed_at_ms": NOW + 1}, "future-observation"),
            ({"started_at_ms": NOW - 6000}, "probe-window-exceeded"),
            ({"expires_at_ms": NOW + 30000}, "expiry-exceeds-policy"),
        )
        for change, reason in changes:
            with self.subTest(reason=reason):
                result = assess(selected, replace(observed, **change))
                self.assertEqual(
                    (result.health, result.decision),
                    (CapacityHealth.UNKNOWN, CapacityDecision.HOLD),
                )
                self.assertIn(reason, [item.reason for item in result.findings])

    def test_missing_and_unexpected_role_are_not_healthy(self):
        selected = policy()
        observed = observation(selected)
        missing = replace(observed, samples=observed.samples[:-1])
        self.assertEqual(
            assess(selected, missing).decision, CapacityDecision.RETRY_AFTER_RECHECK
        )
        extra = replace(
            observed,
            samples=tuple(
                sorted(
                    (
                        *observed.samples,
                        replace(observed.samples[0], role="unexpected"),
                    ),
                    key=lambda item: item.role,
                )
            ),
        )
        self.assertEqual(assess(selected, extra).decision, CapacityDecision.HOLD)
        self.assertEqual(
            assess(selected, replace(observed, samples=())).health,
            CapacityHealth.UNKNOWN,
        )

    def test_conflicting_volume_totals_refuse_and_varying_headroom_uses_minimum(self):
        selected = policy(reserve_bytes=100)
        observed = observation(selected)
        changed = change_sample(observed, "temp", total_bytes=2001)
        self.assertEqual(
            assess(selected, changed).decision, CapacityDecision.RETRY_AFTER_RECHECK
        )
        changed = change_sample(
            observed, "temp", available_bytes=CapacityMetric(MEASURED, 99)
        )
        self.assertEqual(assess(selected, changed).decision, CapacityDecision.HOLD)

    def test_aggregate_demand_overflow_holds_and_preserves_canonical_report(self):
        selected = policy(additional_bytes=2**63 - 1)
        result = assess(selected)
        self.assertEqual(result.decision, CapacityDecision.HOLD)
        self.assertIn(
            "demand-exceeds-supported-range", [item.reason for item in result.findings]
        )
        canonical_identity(result.to_dict())

    def test_policy_and_observation_roundtrip_without_private_paths(self):
        selected = policy(additional_bytes=1, reserve_bytes=5)
        observed = observation(selected)
        self.assertEqual(WorkerCapacityPolicy.from_dict(selected.to_dict()), selected)
        self.assertEqual(
            WorkerCapacityObservation.from_dict(observed.to_dict()), observed
        )
        moved = replace(
            selected,
            storage_bindings_identity=canonical_identity("changed private paths"),
        )
        self.assertEqual(assess(moved, observed).decision, CapacityDecision.HOLD)
        report = assess(selected, observed).to_dict()
        self.assertEqual(report["assessed_at_ms"], NOW)
        self.assertEqual(report["observation_expires_at_ms"], observed.expires_at_ms)
        rendered = json.dumps(assess(selected, observed).to_dict())
        for forbidden in (
            "hostname",
            "endpoint",
            "credential",
            "command",
            "workspace_path",
        ):
            self.assertNotIn(forbidden, rendered)
        with self.assertRaises(ContractValidationError):
            replace(selected.roles[0], role="/private/cache")
        with self.assertRaises(ContractValidationError):
            WorkerCapacityObservation.from_dict(
                {**observed.to_dict(), "endpoint": "private"}
            )

    def test_invalid_values_shapes_and_unbounded_inputs_refuse(self):
        for value in (True, -1, 1.5, "1", 2**63):
            with self.subTest(value=value), self.assertRaises(ContractValidationError):
                CapacityMetric(MEASURED, value)
        with self.assertRaises(ContractValidationError):
            CapacityMetric(CapacityProbeStatus.DENIED, 100)
        selected = policy()
        for changes in (
            {"roles": selected.roles[:-1]},
            {"roles": selected.roles * 5},
            {"maximum_retries": 11},
            {"maximum_age_ms": 0},
            {"probe_timeout_ms": 60001},
            {"maximum_age_ms": 1},
            {"write_heavy": "false"},
        ):
            with (
                self.subTest(changes=changes),
                self.assertRaises(ContractValidationError),
            ):
                replace(selected, **changes)
        observed = observation(selected)
        for changes in (
            {"samples": observed.samples * 5},
            {"samples": observed.samples[::-1]},
            {"expires_at_ms": NOW - 100},
            {"os_family": "unknown"},
        ):
            with (
                self.subTest(changes=changes),
                self.assertRaises(ContractValidationError),
            ):
                replace(observed, **changes)
        sample = observed.samples[0]
        for changes in (
            {"volume": None},
            {"total_bytes": None},
            {"total_bytes": 999},
            {"quotas": ()},
            {"quotas": sample.quotas * 2},
        ):
            with (
                self.subTest(changes=changes),
                self.assertRaises(ContractValidationError),
            ):
                replace(sample, **changes)

    def test_versioned_schemas_validate_wire_records_and_reject_invalid_metrics(self):
        root = Path(__file__).resolve().parents[2] / "schemas"
        registry = Registry()
        for path in sorted(root.glob("v*/*.schema.json")):
            document = json.loads(path.read_text())
            registry = registry.with_resource(
                document["$id"], Resource.from_contents(document)
            )
        registry = registry.crawl()
        selected = policy()
        observed = observation(selected)
        for value in (
            selected.to_dict(),
            observed.to_dict(),
            assess(selected, observed).to_dict(),
        ):
            Draft202012Validator({"$ref": value["schema"]}, registry=registry).validate(
                value
            )
        changed = observed.to_dict()
        changed["samples"][0]["available_bytes"]["value"] = True
        self.assertTrue(
            list(
                Draft202012Validator(
                    {"$ref": observed.SCHEMA}, registry=registry
                ).iter_errors(changed)
            )
        )
        report = verify_schema_catalog("v2", root / "v2")
        self.assertGreater(report["resource_count"], 0)


if __name__ == "__main__":
    unittest.main()
