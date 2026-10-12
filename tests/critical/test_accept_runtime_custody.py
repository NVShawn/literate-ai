"""ACCEPT receives the exact runtime closure and its refusal prevents execution."""

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from literate_ai.contracts import StandardExecutionAuthority
from literate_ai.contracts.capabilities import DependencyKind
from literate_ai.contracts.standard_execution_inputs import standard_execution_request
from literate_ai.security import BuildAuthorization, SecurityProfile
from tests.support.fixtures_test_component_execution_planning import _diamond_lock
from tests.support.fixtures_test_component_generation_scheduling import (
    _names,
    _prepared_execution,
)
from tests.support.fixtures_test_standard_project_lifecycle import (
    ContractEvidenceLifecyclePorts,
    _decision,
    _identity,
    _prepared_nodes,
    _service,
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
            execution_authorized_at=now,
        )
        self.typed_acceptances[plan.component_revision.uri] = replace(
            self.typed_acceptances[plan.component_revision.uri], execution=result
        )
        return result


class AcceptRuntimeCustodyTests(unittest.TestCase):
    def setUp(self):
        self.lock = _diamond_lock(dependency_kind=DependencyKind.RUNTIME)
        self.execution, requests = _prepared_execution(self.lock)
        self.names = _names(self.lock)
        self.nodes = _prepared_nodes(self.execution, requests)

    def run_lifecycle(self, ports, **kwargs):
        return _service(ports).execute(
            self.execution,
            component_lock=self.lock,
            invalidation=_decision(
                self.execution,
                self.names,
                "money",
                () if kwargs.get("resume_candidates") else tuple(self.names.values()),
            ),
            prepared_nodes=self.nodes,
            max_parallelism=2,
            **kwargs,
        )

    def test_acceptor_receives_full_runtime_closure_with_local_execution(self):
        ports = ScopedRuntimePorts(self.execution, self.names)
        retained = {}

        class Acceptor:
            def retain_execution_provider_evidence(self, plan, scope, receipts):
                value = (scope, receipts)
                if retained.setdefault(plan.identity, value) != value:
                    raise AssertionError("runtime custody changed")

            def accept(self, plan, test, execution):
                scope, receipts = retained[plan.identity]
                if scope != ports.execution_scopes[plan.component_revision.uri]:
                    raise AssertionError("receipt scope differs from execution")
                return ports.accept(plan, test, execution)

        service = _service(ports)
        service.acceptor = Acceptor()
        result = service.execute(
            self.execution,
            component_lock=self.lock,
            invalidation=_decision(
                self.execution, self.names, "money", tuple(self.names.values())
            ),
            prepared_nodes=self.nodes,
            max_parallelism=2,
        )
        self.assertTrue(result.successful)
        root_scope, root_receipts = next(
            value
            for value in retained.values()
            if value[0].component_revision == self.execution.root_revision
        )
        self.assertEqual(
            {self.names[r.component_revision.uri] for r in root_receipts},
            {"money", "pricing", "reporting"},
        )
        self.assertEqual(
            {a.identity for r in root_receipts for a in r.build.exports},
            set(root_scope.provider_artifact_identities),
        )

    def test_execution_grant_is_checked_when_the_execution_ran(self):
        """Reused evidence outlives its grant; only the run must be authorized."""

        ports = ScopedRuntimePorts(self.execution, self.names)
        self.assertTrue(self.run_lifecycle(ports).successful)
        evidence = next(
            item.execution
            for item in ports.typed_acceptances.values()
            if getattr(item.execution, "execution_authority", None) is not None
        )
        grant = evidence.execution_authority.grant
        # Long after the grant expired, the recorded execution stays authorized.
        evidence.require_authorized(now=grant.expires_at + timedelta(hours=6))
        # Evidence cannot claim a run the grant has yet to authorize.
        with self.assertRaises(ValueError):
            evidence.require_authorized(
                now=evidence.execution_authorized_at - timedelta(seconds=1)
            )
        for outside in (
            grant.issued_at - timedelta(seconds=1),
            grant.expires_at,
            None,
        ):
            with self.subTest(outside=outside), self.assertRaises(ValueError):
                replace(evidence, execution_authorized_at=outside)

    def test_acceptor_receipt_refusal_prevents_execution(self):
        ports = ScopedRuntimePorts(self.execution, self.names)

        class Acceptor:
            def retain_execution_provider_evidence(self, plan, scope, receipts):
                raise RuntimeError("ACCEPT runtime custody unavailable")

            def accept(self, *args):
                raise AssertionError("accepted after receipt refusal")

        service = _service(ports)
        service.acceptor = Acceptor()
        result = service.execute(
            self.execution,
            component_lock=self.lock,
            invalidation=_decision(
                self.execution, self.names, "money", tuple(self.names.values())
            ),
            prepared_nodes=self.nodes,
            max_parallelism=2,
        )
        self.assertFalse(result.successful)
        self.assertFalse(any(e[0] in {"execute", "accept"} for e in ports.events))
