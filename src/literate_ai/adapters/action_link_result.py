"""Exact LINK result descriptors; evidence reopening remains independently required."""

from dataclasses import dataclass

from literate_ai.adapters.action_dispatch_wire import (
    MAX_ACTION_RECORD_BYTES,
    ActionWireError,
    record_identity,
)
from literate_ai.adapters.action_link_record import LinkWorkerInput
from literate_ai.contracts import ContentIdentity, canonical_json_bytes
from literate_ai.contracts.executable_components.artifacts import ComponentBuildManifest


@dataclass(frozen=True)
class LinkWorkerResult:
    input_identity: ContentIdentity
    acceptance_result_identity: ContentIdentity
    manifest: ComponentBuildManifest
    dependency_links: tuple[tuple[str, ContentIdentity], ...]

    def to_bytes(self):
        return canonical_json_bytes(
            {
                "schema": "literate-ai/link-worker-result@1",
                "input_identity": self.input_identity.uri,
                "acceptance_result_identity": self.acceptance_result_identity.uri,
                "manifest": self.manifest.to_dict(),
                "dependency_links": [
                    {"action_id": action, "result_identity": identity.uri}
                    for action, identity in self.dependency_links
                ],
            }
        )

    @classmethod
    def expected(cls, input_record, input_identity, deadline):
        """Derive the descriptor, without claiming its underlying proof is verified."""
        value = LinkWorkerInput.admit(input_record, input_identity, deadline)
        return cls(
            input_identity,
            record_identity(value.acceptance_result.to_bytes()),
            value.manifest,
            value.dependency_links,
        )

    @classmethod
    def admit(cls, content, identity, *, input_record, input_identity, deadline):
        deadline.remaining()
        if (
            not isinstance(content, bytes)
            or len(content) > MAX_ACTION_RECORD_BYTES
            or not isinstance(identity, ContentIdentity)
            or record_identity(content) != identity
        ):
            raise ActionWireError(
                "action_link.result_invalid", "LINK result custody refused"
            )
        expected = cls.expected(input_record, input_identity, deadline)
        # Exact recomputation simultaneously checks canonical JSON, closed fields,
        # manifest realization and the ordered dependency result identities.
        if content != expected.to_bytes():
            raise ActionWireError(
                "action_link.result_invalid", "LINK result differs from accepted input"
            )
        deadline.remaining()
        return expected
