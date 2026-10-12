"""Observe the portable FINALIZE runtime configuration for private grant binding."""

from literate_ai.adapters.action_dispatch_wire import ActionWireError
from literate_ai.adapters.action_finalize_record import FinalizeWorkerInput
from literate_ai.adapters.action_toolchains import WorkerToolchainRegistry
from literate_ai.adapters.lifecycle.standard_local import (
    LocalIndependentAcceptanceCase,
    LocalStandardLifecyclePorts,
)
from literate_ai.contracts import (
    PORTABLE_APPLICATION_ENTRYPOINT_KIND,
    ContentIdentity,
    canonical_identity,
)


def portable_finalize_runtime_identity(value, ports, *, startup, environment):
    """Observe supported private runtime state; expose only its content identity.

    Startup is a measured tool registry, including any startup authority guard.
    The caller supplies the actual child environment on every observation. Other
    runtime kinds require their own complete configuration observers.
    """
    if (
        not isinstance(value, FinalizeWorkerInput)
        or not isinstance(ports, LocalStandardLifecyclePorts)
        or not isinstance(startup, WorkerToolchainRegistry)
    ):
        raise TypeError("FINALIZE observation requires private Standard runtime")
    plan = value.package_input.plan
    if (
        len(plan.entrypoints) != 1
        or plan.entrypoints[0].kind != PORTABLE_APPLICATION_ENTRYPOINT_KIND
        or ports._native_sdk_inputs is not None
        or ports.npm_targets
        or ports.python_targets
        or ports.python_wheelhouse is not None
        or ports.browser_driver is not None
        or ports.ipc_surface_probe is not None
        or ports.shared_cache is not None
    ):
        raise ActionWireError(
            "action_finalize.profile_unsupported", "runtime requires another observer"
        )
    child_environment = dict(environment)
    if any(
        not isinstance(key, str) or not isinstance(item, str)
        for key, item in child_environment.items()
    ):
        raise TypeError("runtime environment must contain text pairs")
    oracle = ports.independent_acceptance_oracle
    oracle_identity = getattr(oracle, "identity", None)
    if not isinstance(oracle_identity, ContentIdentity):
        raise ActionWireError(
            "action_finalize.oracle_invalid", "private oracle required"
        )
    cases = tuple(oracle.cases(value.component_lock))
    if (
        not cases
        or any(not isinstance(case, LocalIndependentAcceptanceCase) for case in cases)
        or len({case.case_id for case in cases}) != len(cases)
        or oracle.identity != oracle_identity
    ):
        raise ActionWireError(
            "action_finalize.oracle_invalid", "private oracle changed"
        )
    tools = WorkerToolchainRegistry(tuple(ports.tool_bindings.values()))
    return canonical_identity(
        {
            "schema": "literate-ai/portable-finalize-runtime@1",
            "startup": startup.identity.uri,
            "tools": tools.identity.uri,
            "contracts": sorted(
                contract.identity.uri for contract in ports.contracts.values()
            ),
            "command_phases": sorted(phase.value for phase in ports._command_phases),
            "oracle": oracle_identity.uri,
            "oracle_cases": [case.identity.uri for case in cases],
            "environment": child_environment,
            "provider_environment": dict(ports.provider_environment),
            "dependency_observation": {
                "components": ports.dependency_observation.components,
                "edges": ports.dependency_observation.edges,
            },
        }
    )
