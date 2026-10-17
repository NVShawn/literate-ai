"""Receiver-side FINALIZE grant checks matching the portable child runtime.

Private receiver startup supplies the grant path, tools and oracle; the
configured worker supplies the exact child environment. Requests select none.
The parent measures the same portable runtime the child measures, so it never
relies on the child's own grant check, and an operator can be told the exact
runtime a grant must name.
"""

import os
from datetime import UTC, datetime
from functools import partial
from types import MappingProxyType

from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.action_finalize_authority import (
    FinalizeExecutionGuard,
    finalize_execution_request,
)
from literate_ai.adapters.action_finalize_grant import PrivateFinalizeGrantProvider
from literate_ai.adapters.action_finalize_inputs import FinalizeRuntimeInputs
from literate_ai.adapters.action_finalize_profile import (
    portable_finalize_runtime_identity,
)
from literate_ai.adapters.action_finalize_record import FinalizeWorkerInput
from literate_ai.adapters.action_toolchains import WorkerToolchainRegistry
from literate_ai.adapters.lifecycle.standard_local import LocalStandardLifecyclePorts
from literate_ai.diagnostics import inherited_verbose_environment
from literate_ai.security import BuildAuthorization, SecurityProfile

_RESERVED = frozenset(
    {
        "litai_finalize_input_identity",
        "litai_finalize_deadline",
        "litai_finalize_cas",
        "litai_finalize_workspace",
    }
)


def portable_child_environment(environment, launcher_environment=()):
    """Return the environment a supervised child measures after its controls.

    The bounded launcher adds run-scoped diagnostics, and Windows reports
    ``os.environ`` keys upper-cased, so measure exactly what the child sees.
    """
    merged = dict(environment)
    merged.update(dict(launcher_environment))
    if any(not isinstance(k, str) or not isinstance(v, str) for k, v in merged.items()):
        raise TypeError("runtime environment must contain text pairs")
    merged = inherited_verbose_environment(merged)
    if os.name == "nt":
        merged = {k.upper(): v for k, v in merged.items()}
    return {k: v for k, v in merged.items() if k.casefold() not in _RESERVED}


class PortableFinalizeParentAuthority:
    """Two-step parent authority for ConfiguredFinalizeWorker.

    admit() runs before inputs are staged and requires a current grant scoped to
    the exact input, revision and privileges; it cannot check the runtime, which
    depends on staged contracts. require() runs once inputs are staged, measures
    the child's portable runtime and applies the complete grant guard.
    """

    def __init__(
        self,
        *,
        grant_path,
        tool_bindings,
        oracle,
        clock=lambda: datetime.now(UTC),
    ):
        if not callable(clock):
            raise TypeError("FINALIZE requires a current clock")
        self.provider = PrivateFinalizeGrantProvider(grant_path)
        self.tool_bindings = tuple(tool_bindings)
        self.startup = WorkerToolchainRegistry(self.tool_bindings)
        self.oracle = oracle
        self.clock = clock

    def admit(self, value):
        if not isinstance(value, FinalizeWorkerInput):
            raise ActionWireError(
                "action_finalize.authority_mismatch", "unexpected FINALIZE input"
            )
        grant = self.provider()
        now = self.clock()
        if (
            not isinstance(grant, BuildAuthorization)
            or grant.revoked
            or grant.profile is SecurityProfile.BLOCKED
            or grant.classification_digest != record_identity(value.to_bytes()).uri
            or grant.effective_revision_digest != value.component_lock.root_revision.uri
            or grant.privileges != ("execute-project",)
            or not grant.issued_at <= now < grant.expires_at
        ):
            raise ActionWireError(
                "action_finalize.authority_invalid", "private FINALIZE grant refused"
            )

    def runtime_identity(self, prepared, object_root, environment):
        """Measure the portable runtime the child will measure for these inputs."""
        if not isinstance(prepared, FinalizeRuntimeInputs):
            raise ActionWireError(
                "action_finalize.authority_mismatch", "unexpected FINALIZE input"
            )
        ports = LocalStandardLifecyclePorts(
            source_trees=prepared.source_trees,
            object_root=object_root,
            contracts=tuple(
                build.inputs.contract for build in prepared.component_builds
            ),
            tool_bindings=self.tool_bindings,
            independent_acceptance_oracle=self.oracle,
        )
        return portable_finalize_runtime_identity(
            prepared.intent,
            ports,
            startup=self.startup,
            environment=MappingProxyType(dict(environment)),
        )

    def grant_request(self, prepared, object_root, environment):
        """The exact request an operator grant must authorize for these inputs."""
        return finalize_execution_request(
            prepared.intent, self.runtime_identity(prepared, object_root, environment)
        )

    def require(self, prepared, object_root, environment):
        observe = partial(self.runtime_identity, prepared, object_root, environment)
        FinalizeExecutionGuard(
            prepared.intent,
            observe(),
            grant_provider=self.provider,
            observe_runtime=observe,
            clock=self.clock,
        )(prepared.intent)
