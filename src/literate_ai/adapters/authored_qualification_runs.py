"""Join original authored owners to complete shared Standard run observations."""

from literate_ai.adapters.authored_qualification_comparison import (
    compare_authored_qualification,
)
from literate_ai.adapters.authored_qualification_planning import (
    CurrentAuthoredQualificationPlan,
)
from literate_ai.contracts.authored_qualification_requests import (
    AuthoredQualificationLifecycleRequest,
)
from literate_ai.contracts.authored_qualification_runs import (
    AuthoredQualificationNodeRun,
    AuthoredQualificationRun,
)
from literate_ai.source_to_specification.qualification_lifecycle import (
    QualificationLifecycleExecution,
    QualificationLifecycleRunner,
)


def observe_authored_qualification_run(current, *, request, execution, reader):
    """Capture a completed run without asserting permission or publication.

    The original executor still owns allocation, host authorization and model
    execution. A receiver must independently reopen these retained observations;
    this function neither decodes a saved plan into an owner nor grants admission.
    """

    if (
        not isinstance(current, CurrentAuthoredQualificationPlan)
        or not isinstance(request, AuthoredQualificationLifecycleRequest)
        or not isinstance(execution, QualificationLifecycleExecution)
    ):
        raise TypeError("original authored owner, request and Standard run required")
    current.require_unchanged()
    with current.oracles.capture_current():
        if (
            request.origin != current.plan.origin
            or request.plan_identity != current.plan.identity
            or request.oracle_set_identity != current.plan.oracle_set_identity
            or execution.driver_identity != current.plan.driver_identity
            or execution.lifecycle_policy_identity
            != current.plan.lifecycle_policy_identity
            or execution.framework_distribution_identity
            != current.plan.framework_distribution_identity
        ):
            raise ValueError("qualification.authored.run_plan_mismatch")
        # Keep source-derived and authored execution on the same substantive
        # Standard receipt, fresh-source, typed-stage and cache checks.
        QualificationLifecycleRunner._derive_standard(execution)
        lifecycle = execution.lifecycle
        plan = current.plan.execution_plan
        expected = {p.component_revision: p for p in plan.generation_plans}
        nodes = lifecycle.node_results
        membership = lifecycle.lifecycle_membership
        if (
            lifecycle.execution_plan_identity != plan.identity
            or membership.execution_plan_identity != plan.identity
            or len(nodes) != len(expected)
            or {n.component_revision for n in nodes} != set(expected)
            or {n.component_revision for n in membership.planned_nodes} != set(expected)
            or lifecycle.root_integration is None
            or lifecycle.root_integration_evidence_identity
            != lifecycle.root_integration.identity
        ):
            raise ValueError("qualification.authored.run_membership_mismatch")
        for item in membership.planned_nodes:
            selected = expected[item.component_revision]
            if (
                item.generation_plan_identity != selected.identity
                or item.generation_key_identity != selected.generation_key.identity
            ):
                raise ValueError("qualification.authored.run_membership_mismatch")
        comparison = compare_authored_qualification(
            current,
            run_identity=request.run_identity,
            root=lifecycle.root_integration,
            reader=reader,
        )
        result = AuthoredQualificationRun(
            request,
            execution.driver_identity,
            execution.lifecycle_policy_identity,
            execution.framework_distribution_identity,
            execution.lifecycle_request_identity,
            execution.lifecycle_invocation_identity,
            lifecycle.identity,
            execution.receipt,
            lifecycle.root_integration,
            comparison,
            tuple(
                AuthoredQualificationNodeRun(
                    node.source_generation,
                    node.source_output,
                    node.index_identity,
                    node.acceptance_evidence,
                    node.identity,
                )
                for node in sorted(nodes, key=lambda n: n.component_revision.uri)
            ),
        )
    current.require_unchanged()
    return result
