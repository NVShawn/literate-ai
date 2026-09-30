"""Reviewed compare-and-swap rebind of project Standard lifecycle authority."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol

from literate_ai.contracts import (
    ContentIdentity,
    ProjectInitializationOrigin,
    ProjectTestReceiptPolicy,
    StandardLifecyclePolicy,
    StandardProjectLifecycleDriver,
    canonical_identity,
    load_current_standard_lifecycle_policy,
)
from literate_ai.projects import ProjectConfigurationStore, ProjectError

STANDARD_REBIND_PLAN_SCHEMA = "literate-ai/standard-lifecycle-rebind-plan@1"
STANDARD_REBIND_RESULT_SCHEMA = "literate-ai/standard-lifecycle-rebind-result@1"


class InstalledDistributionMember(Protocol):
    size: int


class InstalledFrameworkDistribution(Protocol):
    distribution_name: str
    distribution_version: str
    members: Sequence[InstalledDistributionMember]

    @property
    def identity(self) -> ContentIdentity: ...


DistributionObserver = Callable[[], InstalledFrameworkDistribution]
PolicyLoader = Callable[[], StandardLifecyclePolicy]
OriginObserver = Callable[[str], ProjectInitializationOrigin]


class StandardLifecycleRebindError(ValueError):
    """A Standard lifecycle rebind could not preserve its review boundary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _error(code: str, message: str) -> None:
    raise StandardLifecycleRebindError(code, message)


def _store(project: Path) -> tuple[ProjectConfigurationStore, Any]:
    try:
        snapshot = ProjectConfigurationStore.discover(project)
    except ProjectError as exc:
        raise StandardLifecycleRebindError(exc.code, exc.message) from exc
    if snapshot is None:
        _error(
            "standard_rebind.project_unavailable",
            "no Literate AI project manifest is available from the selected path",
        )
    return ProjectConfigurationStore(snapshot.root), snapshot


def _observe(
    *,
    distribution_observer: DistributionObserver,
    policy_loader: PolicyLoader,
    origin_observer: OriginObserver,
) -> tuple[
    InstalledFrameworkDistribution,
    StandardLifecyclePolicy,
    ProjectInitializationOrigin,
]:
    try:
        distribution = distribution_observer()
        policy = policy_loader()
        origin = origin_observer(distribution.distribution_version)
    except (TypeError, ValueError) as exc:
        code = getattr(exc, "code", "standard_rebind.installed_authority_invalid")
        message = getattr(
            exc,
            "message",
            "installed Standard lifecycle authority is invalid",
        )
        raise StandardLifecycleRebindError(
            str(code),
            str(message),
        ) from exc
    try:
        distribution_valid = (
            isinstance(distribution.distribution_name, str)
            and isinstance(distribution.distribution_version, str)
            and isinstance(distribution.identity, ContentIdentity)
            and isinstance(distribution.members, Sequence)
            and bool(distribution.members)
            and all(
                isinstance(member.size, int)
                and not isinstance(member.size, bool)
                and member.size >= 0
                for member in distribution.members
            )
        )
    except (AttributeError, TypeError):
        distribution_valid = False
    if not distribution_valid:
        _error(
            "standard_rebind.installed_authority_invalid",
            "distribution observer returned the wrong authority type",
        )
    if not isinstance(policy, StandardLifecyclePolicy):
        _error(
            "standard_rebind.installed_authority_invalid",
            "policy loader returned the wrong authority type",
        )
    if not isinstance(origin, ProjectInitializationOrigin):
        _error(
            "standard_rebind.installed_authority_invalid",
            "origin observer returned the wrong authority type",
        )
    if (
        origin.distribution_name != distribution.distribution_name
        or origin.distribution_version != distribution.distribution_version
    ):
        _error(
            "standard_rebind.distribution_origin_mismatch",
            "embedded origin does not identify the observed installed distribution",
        )
    return distribution, policy, origin


def _receipt_policy(
    driver: StandardProjectLifecycleDriver,
    policy: StandardLifecyclePolicy,
    *,
    configured_driver: StandardProjectLifecycleDriver,
    configured_receipt: ProjectTestReceiptPolicy,
) -> ProjectTestReceiptPolicy:
    # Only the Standard suite belongs to the installed policy. A custom suite's
    # runner can derive from the Standard driver without surrendering its contract.
    if configured_receipt.suite_id != policy.policy_id:
        if configured_receipt.runner_identity == configured_driver.identity:
            return replace(configured_receipt, runner_identity=driver.identity)
        return configured_receipt
    return ProjectTestReceiptPolicy(
        suite_id=policy.policy_id,
        suite_version=policy.policy_version,
        runner_identity=driver.identity,
        required_evidence_kinds=policy.required_evidence_kinds,
        minimum_test_count=policy.minimum_test_count,
    )


def _installed_evidence(
    distribution: InstalledFrameworkDistribution,
    policy: StandardLifecyclePolicy,
    origin: ProjectInitializationOrigin,
) -> dict[str, object]:
    return {
        "distribution": {
            "name": distribution.distribution_name,
            "version": distribution.distribution_version,
            "identity": distribution.identity.uri,
            "member_count": len(distribution.members),
            "payload_bytes": sum(member.size for member in distribution.members),
        },
        "origin": origin.to_dict(),
        "policy": {
            "id": policy.policy_id,
            "version": policy.policy_version,
            "identity": policy.identity.uri,
        },
    }


def plan_standard_lifecycle_rebind(
    project: Path,
    *,
    distribution_observer: DistributionObserver,
    origin_observer: OriginObserver,
    policy_loader: PolicyLoader = load_current_standard_lifecycle_policy,
) -> dict[str, object]:
    """Plan one reviewed Standard rebind without changing project authority."""

    _configuration_store, snapshot = _store(project)
    configured = snapshot.definition.lifecycle_driver
    configured_receipt = snapshot.definition.test_receipt_policy
    if not isinstance(configured, StandardProjectLifecycleDriver):
        _error(
            "standard_rebind.binding_required",
            "project lifecycle_driver must use the Standard binding",
        )
    if not isinstance(configured_receipt, ProjectTestReceiptPolicy):
        _error(
            "standard_rebind.receipt_policy_required",
            "Standard-bound project must configure its test receipt policy",
        )
    distribution, policy, origin = _observe(
        distribution_observer=distribution_observer,
        policy_loader=policy_loader,
        origin_observer=origin_observer,
    )
    proposed = StandardProjectLifecycleDriver(distribution.identity, policy.identity)
    proposed_receipt = _receipt_policy(
        proposed,
        policy,
        configured_driver=configured,
        configured_receipt=configured_receipt,
    )
    changed_fields = [
        name
        for name, old, new in (
            ("lifecycle_driver", configured, proposed),
            ("test_receipt_policy", configured_receipt, proposed_receipt),
        )
        if old != new
    ]
    result: dict[str, object] = {
        "schema": STANDARD_REBIND_PLAN_SCHEMA,
        "project_configuration_identity": snapshot.content_identity.uri,
        "configured_binding": configured.to_dict(),
        "proposed_binding": proposed.to_dict(),
        "configured_receipt_policy": configured_receipt.to_dict(),
        "proposed_receipt_policy": proposed_receipt.to_dict(),
        "installed_authority": _installed_evidence(distribution, policy, origin),
        "change_required": bool(changed_fields),
        "changed_fields": changed_fields,
        "evidence_after_apply": {
            "test_receipt": "stale-until-rebuild",
            "accepted_source_membership": "stale-until-rebuild",
        },
    }
    result["identity"] = canonical_identity(result).uri
    return result


def _validated_plan(value: Mapping[str, object]) -> dict[str, object]:
    plan = dict(value)
    required = {
        "schema",
        "project_configuration_identity",
        "configured_binding",
        "proposed_binding",
        "configured_receipt_policy",
        "proposed_receipt_policy",
        "installed_authority",
        "change_required",
        "changed_fields",
        "evidence_after_apply",
        "identity",
    }
    if set(plan) != required or plan.get("schema") != STANDARD_REBIND_PLAN_SCHEMA:
        _error("standard_rebind.plan_invalid", "rebind plan has an invalid shape")
    identity = plan.pop("identity")
    if not isinstance(identity, str) or canonical_identity(plan).uri != identity:
        _error("standard_rebind.plan_invalid", "rebind plan identity is invalid")
    plan["identity"] = identity
    try:
        StandardProjectLifecycleDriver.from_dict(
            plan["configured_binding"], path="rebind.configured_binding"
        )
        StandardProjectLifecycleDriver.from_dict(
            plan["proposed_binding"], path="rebind.proposed_binding"
        )
        ProjectTestReceiptPolicy.from_dict(
            plan["configured_receipt_policy"],
            path="rebind.configured_receipt_policy",
        )
        ProjectTestReceiptPolicy.from_dict(
            plan["proposed_receipt_policy"], path="rebind.proposed_receipt_policy"
        )
    except (TypeError, ValueError) as exc:
        raise StandardLifecycleRebindError(
            "standard_rebind.plan_invalid", "rebind plan contains invalid authority"
        ) from exc
    return plan


def apply_standard_lifecycle_rebind(
    project: Path,
    plan_value: Mapping[str, object],
    *,
    authorize_rebind: bool,
    distribution_observer: DistributionObserver,
    origin_observer: OriginObserver,
    policy_loader: PolicyLoader = load_current_standard_lifecycle_policy,
) -> dict[str, object]:
    """Apply one exact reviewed plan and roll back post-write host drift."""

    if not authorize_rebind:
        _error(
            "standard_rebind.authorization_required",
            "apply requires --authorize-rebind",
        )
    plan = _validated_plan(plan_value)
    store, snapshot = _store(project)
    if snapshot.content_identity.uri != plan["project_configuration_identity"]:
        _error(
            "standard_rebind.project_changed",
            "project manifest changed after the rebind was planned",
        )
    if snapshot.definition.lifecycle_driver is None:
        _error("standard_rebind.binding_required", "project has no lifecycle binding")
    if snapshot.definition.lifecycle_driver.to_dict() != plan["configured_binding"]:
        _error(
            "standard_rebind.project_changed",
            "project Standard binding changed after the rebind was planned",
        )
    if (
        snapshot.definition.test_receipt_policy is None
        or snapshot.definition.test_receipt_policy.to_dict()
        != plan["configured_receipt_policy"]
    ):
        _error(
            "standard_rebind.project_changed",
            "project test receipt policy changed after the rebind was planned",
        )
    distribution, policy, origin = _observe(
        distribution_observer=distribution_observer,
        policy_loader=policy_loader,
        origin_observer=origin_observer,
    )
    if _installed_evidence(distribution, policy, origin) != plan["installed_authority"]:
        _error(
            "standard_rebind.installed_authority_changed",
            "installed Standard authority changed after the rebind was planned",
        )
    proposed = StandardProjectLifecycleDriver.from_dict(
        plan["proposed_binding"], path="rebind.proposed_binding"
    )
    proposed_receipt = ProjectTestReceiptPolicy.from_dict(
        plan["proposed_receipt_policy"], path="rebind.proposed_receipt_policy"
    )
    if proposed.framework_distribution_identity != distribution.identity:
        _error(
            "standard_binding.distribution_mismatch",
            "project-pinned framework distribution differs from installed wheel bytes",
        )
    if proposed.policy_identity != policy.identity:
        _error(
            "standard_binding.policy_mismatch",
            "project-pinned Standard policy differs from installed policy",
        )

    configured = snapshot.definition.lifecycle_driver
    configured_receipt = snapshot.definition.test_receipt_policy
    if not isinstance(configured, StandardProjectLifecycleDriver):
        _error("standard_rebind.binding_required", "project must use Standard binding")
    expected_receipt = _receipt_policy(
        proposed,
        policy,
        configured_driver=configured,
        configured_receipt=configured_receipt,
    )
    changed_fields = [
        name
        for name, old, new in (
            ("lifecycle_driver", configured, proposed),
            ("test_receipt_policy", configured_receipt, expected_receipt),
        )
        if old != new
    ]
    if (
        proposed_receipt != expected_receipt
        or plan["changed_fields"] != changed_fields
        or plan["change_required"] is not bool(changed_fields)
    ):
        _error(
            "standard_rebind.plan_invalid",
            "rebind plan must preserve project-owned receipt authority; plan again",
        )

    if not plan["change_required"]:
        updated = snapshot
        action = "no-op"
    else:
        definition = replace(
            snapshot.definition,
            lifecycle_driver=proposed,
            test_receipt_policy=proposed_receipt,
        )
        try:
            updated = store.update(snapshot, definition)
        except ProjectError as exc:
            raise StandardLifecycleRebindError(exc.code, exc.message) from exc
        action = "applied"

    try:
        final_distribution, final_policy, final_origin = _observe(
            distribution_observer=distribution_observer,
            policy_loader=policy_loader,
            origin_observer=origin_observer,
        )
        final_evidence = _installed_evidence(
            final_distribution, final_policy, final_origin
        )
        if final_evidence != plan["installed_authority"]:
            raise StandardLifecycleRebindError(
                "standard_rebind.installed_authority_changed",
                "installed Standard authority changed while the rebind was applied",
            )
    except Exception as exc:
        if action == "applied":
            try:
                store.update(updated, snapshot.definition)
            except ProjectError as rollback_error:
                raise StandardLifecycleRebindError(
                    "standard_rebind.rollback_failed",
                    "installed authority changed and project rollback failed",
                ) from rollback_error
        if isinstance(exc, StandardLifecycleRebindError):
            raise
        raise StandardLifecycleRebindError(
            "standard_rebind.installed_authority_changed",
            "installed Standard authority became unavailable while applying rebind",
        ) from exc

    result: dict[str, object] = {
        "schema": STANDARD_REBIND_RESULT_SCHEMA,
        "plan_identity": plan["identity"],
        "action": action,
        "previous_project_configuration_identity": snapshot.content_identity.uri,
        "project_configuration_identity": updated.content_identity.uri,
        "binding": proposed.to_dict(),
        "receipt_policy": proposed_receipt.to_dict(),
        "installed_authority": plan["installed_authority"],
        "evidence": plan["evidence_after_apply"],
        "next_action": "run litai rebuild to produce current accepted evidence",
    }
    result["identity"] = canonical_identity(result).uri
    return result


__all__ = [
    "STANDARD_REBIND_PLAN_SCHEMA",
    "STANDARD_REBIND_RESULT_SCHEMA",
    "StandardLifecycleRebindError",
    "apply_standard_lifecycle_rebind",
    "plan_standard_lifecycle_rebind",
]
