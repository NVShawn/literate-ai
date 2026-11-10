"""Join all original current graph oracles to native Standard observations."""

from literate_ai.adapters.authored_qualification_oracles import (
    CurrentAuthoredQualificationOracles,
)
from literate_ai.contracts.authored_qualification_acceptance import (
    AuthoredGraphIndependentAcceptance,
    AuthoredNodeIndependentAcceptance,
)
from literate_ai.contracts.identity import ContentIdentity


def accept_authored_graph(
    oracles,
    component_lock,
    execution_plan,
    package_plan,
    package_result,
    root_test_identity,
    packaged_execution_identity,
    *,
    accept_node,
    record,
):
    """Run the native verifier for each exact node; counts never replace calls.

    Native ports supply their existing guarded artifact/tool verifier. This
    composition does not rewrite the lock's root or invent a source baseline.
    Failed or stale attempts publish no positive aggregate record.
    """

    if not isinstance(oracles, CurrentAuthoredQualificationOracles):
        raise TypeError("current authored oracle owner required")
    with oracles.capture_current():
        origin = oracles.authority.origin
        revisions = {node.revision.identity for node in component_lock.nodes}
        if (
            component_lock.identity != origin.component_lock_identity
            or execution_plan.component_lock_identity != component_lock.identity
            or package_plan.component_lock_identity != component_lock.identity
            or len(oracles.bindings) != len(revisions)
            or {item.component_revision_identity for item in oracles.bindings}
            != revisions
        ):
            raise ValueError("qualification.authored.acceptance_membership_mismatch")
        oracle_set_identity = oracles.identity
        nodes = []
        for binding in oracles.bindings:
            observation = accept_node(binding)
            if not isinstance(observation, ContentIdentity):
                raise TypeError("native independent verifier observation required")
            nodes.append(
                AuthoredNodeIndependentAcceptance(
                    binding.component_revision_identity,
                    binding.oracle_identity,
                    binding.case_map.identity,
                    observation,
                    len(binding.case_map.cases),
                )
            )
        result = AuthoredGraphIndependentAcceptance(
            origin,
            oracle_set_identity,
            execution_plan.identity,
            package_plan.identity,
            package_result.identity,
            root_test_identity,
            packaged_execution_identity,
            tuple(sorted(nodes, key=lambda item: item.component_revision_identity.uri)),
        )
    # Reenter current authority after the original context's closing guards.
    oracles.require_unchanged()
    if record(result.to_dict()) != result.identity:
        raise ValueError("qualification.authored.acceptance_record_mismatch")
    return result.identity
