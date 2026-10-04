"""Deterministic Standard authorization from controller-admitted inputs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from literate_ai.application.standard_project_lifecycle import (
    StandardBuildAuthorization,
    StandardComponentBuildIntent,
)
from literate_ai.contracts.identity import ContentIdentity
from literate_ai.security import BuildAuthorization, SecurityProfile


@dataclass(frozen=True, slots=True)
class StandardAuthorizationInputs:
    intent: StandardComponentBuildIntent
    index: ContentIdentity
    issued_at: datetime

    def authorize(self) -> StandardBuildAuthorization:
        """Preserve the local policy; this function does not admit its own inputs."""
        intent = self.intent
        grant = BuildAuthorization(
            authorization_id=f"local:{intent.identity.digest}",
            classification_digest=self.index.uri,
            request_digest=intent.build_request_identity.uri,
            effective_revision_digest=intent.component_revision.uri,
            actor="local-standard-lifecycle",
            reason="execute explicitly configured local Component commands",
            profile=SecurityProfile.CONSTRAINED,
            privileges=intent.build_request.requested_privileges,
            issued_at=self.issued_at,
            expires_at=self.issued_at + timedelta(minutes=30),
        )
        return StandardBuildAuthorization(
            intent.identity, intent.build_request_identity, self.index, grant
        )
