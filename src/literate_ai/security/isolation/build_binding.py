"""Exact build-grant binding for an independently reviewed production runner.

These immutable references do not validate runtime configuration or prove controls.
The trusted launcher must resolve and validate their bytes before admission, then
execute those exact inputs. There is deliberately no producer-deserialization path.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.identity import canonical_identity

from ..policy import AuthorizationError, BuildRequest
from .contracts import (
    ContainmentStage,
    IsolationLevel,
    IsolationPolicy,
    IsolationRequest,
)


@dataclass(frozen=True, slots=True)
class ProductionBuildBinding:
    """Bind the full isolation request and reviewed runtime closure into a grant.

    Configuration includes command, environment, mounts, finite resource budgets,
    egress and output policy. Host profile includes the qualified kernel or guest
    and provisioned controls. Their content references never imply qualification.
    """

    isolation: IsolationRequest
    policy: IsolationPolicy
    runner_id: str
    runtime: BlobRef
    image: BlobRef
    configuration: BlobRef
    host_profile: BlobRef

    def __post_init__(self) -> None:
        if not isinstance(self.isolation, IsolationRequest):
            raise TypeError("isolation must be an IsolationRequest")
        if not isinstance(self.policy, IsolationPolicy):
            raise TypeError("policy must be an IsolationPolicy")
        if not isinstance(self.runner_id, str) or not self.runner_id.strip():
            raise ValueError("runner_id must name the independently trusted runner")
        for name in ("runtime", "image", "configuration", "host_profile"):
            reference = getattr(self, name)
            if not isinstance(reference, BlobRef) or reference.size <= 0:
                raise ValueError(f"{name} must reference nonempty immutable bytes")
        if self.isolation.stage is not ContainmentStage.BUILD:
            raise AuthorizationError("security.containment_stage_mismatch")
        rule = self.policy.rule_for(self.isolation.stage)
        if rule is None:
            raise AuthorizationError("security.containment_stage_unconfigured")
        required = max(
            rule.minimum_level, self.isolation.requested_level, key=lambda x: x.rank
        )
        if (
            not required.satisfies(IsolationLevel.OS_SANDBOXED)
            or self.isolation.host_yolo_acknowledged
        ):
            raise AuthorizationError("security.production_containment_required")

    @property
    def identity(self) -> str:
        # A domain-separated internal commitment, not a new public wire record.
        return canonical_identity(
            {
                "domain": "literate-ai/production-build-binding/1",
                "isolation": self.isolation.to_dict(),
                "policy": self.policy.to_dict(),
                "runner_id": self.runner_id,
                "runtime": self.runtime.to_dict(),
                "image": self.image.to_dict(),
                "configuration": self.configuration.to_dict(),
                "host_profile": self.host_profile.to_dict(),
            }
        ).uri

    def bind(self, request: BuildRequest) -> BuildRequest:
        """Construct the request to authorize; this never changes an issued grant."""
        if self.isolation.subject_identity != request.source_bundle_digest:
            raise AuthorizationError("security.containment_subject_mismatch")
        return replace(request, sandbox_profile=self.identity)

    def require_request(self, request: BuildRequest) -> None:
        if self.bind(request) != request:
            raise AuthorizationError("security.containment_binding_mismatch")
