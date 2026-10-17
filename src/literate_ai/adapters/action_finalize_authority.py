"""Private live grant checks for exact packaged-project FINALIZE execution."""

from datetime import UTC, datetime

from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.action_finalize_inputs import FinalizeRuntimeInputs
from literate_ai.adapters.action_finalize_record import FinalizeWorkerInput
from literate_ai.contracts import ContentIdentity, canonical_identity
from literate_ai.security import (
    AuthorizationError,
    BuildAuthorization,
    BuildRequest,
    SecurityProfile,
)


def finalize_execution_request(value, runtime_identity):
    """Describe exact project/package execution for the existing private grant flow."""
    if not isinstance(value, FinalizeWorkerInput) or not isinstance(
        runtime_identity, ContentIdentity
    ):
        raise TypeError("FINALIZE authority requires typed intent and runtime identity")
    return BuildRequest(
        effective_revision_digest=value.component_lock.root_revision.uri,
        source_bundle_digest=record_identity(value.to_bytes()).uri,
        builder_id=f"standard-finalize:{runtime_identity.uri}",
        toolchain_digest=runtime_identity.uri,
        sandbox_profile="local-explicit-host-process",
        requested_privileges=("execute-project",),
        allowed_outputs=("stdout", "stderr"),
    )


class FinalizeExecutionGuard:
    """Startup supplies trusted grant lookup and a measured private runtime observer.

    Runtime identity must include the selected commands, tools, oracles and private
    runtime configuration. Grant lookup must reflect current revocation. No grant
    is accepted from the wire, minted here or inferred from earlier BUILD grants.
    """

    def __init__(
        self,
        value,
        runtime_identity,
        *,
        grant_provider,
        observe_runtime,
        clock=lambda: datetime.now(UTC),
    ):
        self.request = finalize_execution_request(value, runtime_identity)
        if not all(callable(item) for item in (grant_provider, observe_runtime, clock)):
            raise TypeError("FINALIZE requires private grant and runtime observers")
        self.input_identity = record_identity(value.to_bytes())
        self.runtime_identity = runtime_identity
        self.grant_provider = grant_provider
        self.observe_runtime = observe_runtime
        self.clock = clock

    def __call__(self, value):
        if (
            not isinstance(value, FinalizeWorkerInput)
            or record_identity(value.to_bytes()) != self.input_identity
            or self.observe_runtime() != self.runtime_identity
        ):
            raise ActionWireError(
                "action_finalize.authority_mismatch", "FINALIZE authority scope differs"
            )
        grant = self.grant_provider()
        if (
            not isinstance(grant, BuildAuthorization)
            or grant.classification_digest != self.input_identity.uri
            or grant.request_digest != canonical_identity(self.request.to_dict()).uri
            or grant.privileges != self.request.requested_privileges
            or grant.profile is SecurityProfile.BLOCKED
        ):
            raise ActionWireError(
                "action_finalize.authority_invalid", "private FINALIZE grant refused"
            )
        try:
            grant.require_valid(self.request, now=self.clock())
        except AuthorizationError as exc:
            raise ActionWireError(
                "action_finalize.authority_invalid",
                "private FINALIZE grant is not current",
            ) from exc
        if self.observe_runtime() != self.runtime_identity:
            raise ActionWireError(
                "action_finalize.authority_mismatch", "private runtime changed"
            )

    def require_stage(self, stage, prepared):
        if stage not in {
            "root-integration-test",
            "packaged-execution",
            "independent-project-acceptance",
        } or not isinstance(prepared, FinalizeRuntimeInputs):
            raise ActionWireError(
                "action_finalize.authority_mismatch", "unexpected FINALIZE stage"
            )
        self(prepared.intent)
