"""Shared fixtures extracted from ``tests.unit.test_remote_standard_toolchains``."""



from datetime import UTC, datetime



from literate_ai.adapters.action_capabilities import ActionWorkerCapabilities



from literate_ai.adapters.action_tool_observation import WorkerToolObservation



from literate_ai.adapters.standard_toolchain_observations import (
    StandardToolObservations,
)

from literate_ai.application.action_dag_scheduler import LifecycleActionKind

from literate_ai.contracts import canonical_identity







def snapshot(*tools):
    inventory = StandardToolObservations(
        "linux", tuple(sorted(tools, key=lambda item: item.role))
    )
    capability = ActionWorkerCapabilities(
        canonical_identity("request"),
        canonical_identity("worker"),
        canonical_identity("receiver"),
        canonical_identity("python"),
        (3, 14, 0),
        (LifecycleActionKind.BUILD,),
        ("filesystem-cas",),
        datetime.now(UTC),
        canonical_identity("profile"),
        tuple(
            sorted(
                {tool.toolchain_identity for tool in tools}, key=lambda item: item.uri
            )
        ),
        inventory.identity,
    )
    return WorkerToolObservation(capability, inventory)

