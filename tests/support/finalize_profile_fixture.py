"""Measure actual portable ports and bind test-issued project execution authority."""

import os
from datetime import UTC, datetime
from functools import partial

from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.action_finalize_authority import (
    FinalizeExecutionGuard,
    finalize_execution_request,
)
from literate_ai.adapters.action_finalize_profile import (
    portable_finalize_runtime_identity,
)
from literate_ai.adapters.action_toolchains import WorkerToolchainRegistry
from literate_ai.adapters.lifecycle.standard_local import LocalIndependentAcceptanceCase
from literate_ai.contracts import canonical_identity
from literate_ai.security import BuildAuthorization, SecurityProfile

# Operator grants outlive the slowest real-receiver chain, like dispatch.
from tests.support.action_deadline import ACTION_TEST_DEADLINE


def issue_finalize_grant(value, request, *, revoked=False):
    """Stand in for the operator: authorize exactly the planned FINALIZE request."""
    now = datetime.now(UTC)
    return BuildAuthorization(
        "fixture-operator-finalize",
        record_identity(value.to_bytes()).uri,
        canonical_identity(request.to_dict()).uri,
        value.component_lock.root_revision.uri,
        "fixture",
        "test-issued project grant",
        SecurityProfile.CONSTRAINED,
        request.requested_privileges,
        now,
        now + ACTION_TEST_DEADLINE,
        revoked=revoked,
    )


def assert_finalize_profile(case, value, ports):
    startup = WorkerToolchainRegistry(tuple(ports.tool_bindings.values()))
    observe = partial(
        portable_finalize_runtime_identity,
        value,
        ports,
        startup=startup,
        environment=os.environ,
    )
    identity = observe()
    case.assertEqual(observe(), identity)
    case.assertNotEqual(
        portable_finalize_runtime_identity(
            value,
            ports,
            startup=startup,
            environment=dict(os.environ, LITAI_FIXTURE_PROFILE="changed"),
        ),
        identity,
    )
    original = ports.independent_acceptance_oracle

    class ChangedCases:
        identity = original.identity

        def cases(self, lock):
            return (LocalIndependentAcceptanceCase.create("known-output", [], "other"),)

    ports.independent_acceptance_oracle = ChangedCases()
    try:
        case.assertNotEqual(observe(), identity)
    finally:
        ports.independent_acceptance_oracle = original
    ports.provider_environment["fixture-export"] = ("FIXTURE_ENV", "changed")
    try:
        case.assertNotEqual(observe(), identity)
    finally:
        del ports.provider_environment["fixture-export"]
    ports.browser_driver = object()
    try:
        with case.assertRaises(ActionWireError):
            observe()
    finally:
        ports.browser_driver = None
    request = finalize_execution_request(value, identity)
    now = datetime.now(UTC)
    grant = BuildAuthorization(
        "fixture-measured-finalize",
        record_identity(value.to_bytes()).uri,
        canonical_identity(request.to_dict()).uri,
        value.component_lock.root_revision.uri,
        "fixture",
        "test-issued project grant",
        SecurityProfile.CONSTRAINED,
        request.requested_privileges,
        now,
        now + ACTION_TEST_DEADLINE,
    )
    guard = FinalizeExecutionGuard(
        value, identity, grant_provider=lambda: grant, observe_runtime=observe
    )
    guard(value)
    ports.independent_acceptance_oracle = ChangedCases()
    try:
        with case.assertRaises(ActionWireError):
            guard(value)
    finally:
        ports.independent_acceptance_oracle = original
    return guard
