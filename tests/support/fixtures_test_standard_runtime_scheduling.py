"""Shared fixtures extracted from ``tests.unit.test_standard_runtime_scheduling``."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from literate_ai.contracts import StandardExecutionAuthority
from literate_ai.contracts.standard_execution_inputs import standard_execution_request
from literate_ai.security import BuildAuthorization, SecurityProfile
from tests.support.fixtures_test_standard_project_lifecycle import (
    ContractEvidenceLifecyclePorts,
    _identity,
    canonical_identity,
)


class ScopedRuntimePorts(ContractEvidenceLifecyclePorts):
    """Typed controller fixture; real process/grant custody has adapter tests."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.execution_scopes = {}
        self.execution_inputs = {}

    def execute_scoped(self, plan, exports, scope, providers):
        self.execution_scopes[plan.component_revision.uri] = scope
        self.execution_inputs[plan.component_revision.uri] = providers
        original = super().execute(plan, exports)
        command, runtime = _identity("scoped-command"), _identity("scoped-runtime")
        request = standard_execution_request(
            scope, plan.request.source_tree_identity, command, runtime
        )
        now = datetime(2026, 8, 7, tzinfo=UTC)
        grant = BuildAuthorization(
            "fixture-scoped",
            scope.identity.uri,
            canonical_identity(request.to_dict()).uri,
            plan.component_revision.uri,
            "fixture",
            "execute admitted runtime inputs",
            SecurityProfile.CONSTRAINED,
            request.requested_privileges,
            now,
            now + timedelta(minutes=30),
        )
        authority = StandardExecutionAuthority(
            scope, plan.request.source_tree_identity, command, runtime, grant
        )
        result = replace(
            original,
            provider_artifact_identities=scope.provider_artifact_identities,
            execution_authority=authority,
        )
        self.typed_acceptances[plan.component_revision.uri] = replace(
            self.typed_acceptances[plan.component_revision.uri], execution=result
        )
        return result
