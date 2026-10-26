"""Immutable semantic inputs and a separate current payload re-admission subject.

These documents are input authority, never evidence that a graph is complete,
that a payload is compatible, or that native qualification actually ran.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.identity import (
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.contracts.paths import canonical_relative_posix_path
from literate_ai.contracts.retained_project import RetainedProjectMember
from literate_ai.contracts.retained_project_lifecycle import (
    RetainedProjectAction,
    RetainedProjectPlan,
)
from literate_ai.security import BuildRequest

SEMANTIC_INPUT_FIELDS = frozenset(
    {
        "schema",
        "payload_scope",
        "phase",
        "target",
        "compiler",
        "toolchain",
        "sysroot",
        "base_build_abi",
        "path_contract",
        "command",
        "working_directory",
        "environment",
        "members",
        "member_owners",
        "tools",
        "build_dependencies",
        "generated_inputs",
        "options",
        "flavor",
        "outputs",
        "output_format",
    }
)


def _text(value):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4096
        or any(ord(c) < 32 for c in value)
    ):
        raise ValueError("semantic payload requires bounded nonempty text")


def _identity(value):
    ContentIdentity.parse_uri(value)


def _ordered(values, key):
    if not isinstance(values, list) or len(values) > 100_000:
        raise ValueError("semantic payload requires bounded arrays")
    names = [key(item) for item in values]
    if names != sorted(set(names)):
        raise ValueError("semantic payload inventory must be canonical and unique")


@dataclass(frozen=True, slots=True)
class SemanticBuildInputs:
    content: bytes

    def __post_init__(self):
        if not isinstance(self.content, bytes) or len(self.content) > 16 * 1024 * 1024:
            raise ValueError("semantic build inputs require bounded immutable bytes")
        value = json.loads(self.content)
        if (
            not isinstance(value, dict)
            or set(value) != SEMANTIC_INPUT_FIELDS
            or value["schema"] != "literate-ai/semantic-build-inputs@1"
            or canonical_json_bytes(value) != self.content
        ):
            raise ValueError(
                "semantic build inputs require the complete canonical schema"
            )
        if value["payload_scope"] not in {"userland", "kernel"} or value[
            "phase"
        ] not in {"compile", "link", "build"}:
            raise ValueError("semantic build payload scope or phase invalid")
        for field in ("target", "output_format", "working_directory"):
            _text(value[field])
        for field in (
            "compiler",
            "toolchain",
            "sysroot",
            "base_build_abi",
            "path_contract",
        ):
            _identity(value[field])
        command = value["command"]
        if not isinstance(command, list) or not 1 <= len(command) <= 4096:
            raise ValueError("semantic command must retain exact argv")
        for item in command:
            _text(item)
        environment = value["environment"]
        _ordered(environment, lambda item: item["name"])
        for item in environment:
            if set(item) != {"name", "value"} or not isinstance(item["value"], str):
                raise ValueError(
                    "semantic environment must retain exact allowlisted values"
                )
            _text(item["name"])
        members = value["members"]
        _ordered(members, lambda item: item["path"])
        if not members:
            raise ValueError(
                "semantic build requires its exact source/shared member closure"
            )
        for item in members:
            RetainedProjectMember.from_dict(item)
        owners = value["member_owners"]
        if not isinstance(owners, dict) or set(owners) != {
            item["path"] for item in members
        }:
            raise ValueError("every semantic member requires one producer owner")
        for owner in owners.values():
            _text(owner)
        tools = value["tools"]
        _ordered(tools, lambda item: item["name"])
        if not tools:
            raise ValueError("semantic build requires the complete tool closure")
        for item in tools:
            if set(item) != {"name", "path", "identity"}:
                raise ValueError("semantic tool fields invalid")
            _text(item["name"])
            _text(item["path"])
            _identity(item["identity"])
        if not any(item["identity"] == value["compiler"] for item in tools):
            raise ValueError("semantic compiler requires exact tool-closure ownership")
        for field in ("build_dependencies", "generated_inputs"):
            _ordered(value[field], lambda item: (item["producer"], item["output"]))
            for item in value[field]:
                if set(item) != {"producer", "output", "identity"}:
                    raise ValueError("semantic producer edge fields invalid")
                _text(item["producer"])
                if "#" not in item["producer"]:
                    raise ValueError("semantic producer must name UNIT#ACTION")
                canonical_relative_posix_path(item["output"], label="producer output")
                _identity(item["identity"])
        if (
            not isinstance(value["options"], dict)
            or value["flavor"] is not None
            and not isinstance(value["flavor"], str)
        ):
            raise ValueError("semantic options/flavor invalid")
        _ordered(value["outputs"], lambda item: item)
        if not value["outputs"]:
            raise ValueError("semantic action requires declared outputs")
        for output in value["outputs"]:
            canonical_relative_posix_path(output, label="semantic output")

    @classmethod
    def from_dict(cls, value):
        return cls(canonical_json_bytes(value))

    def to_dict(self):
        return json.loads(self.content)

    @property
    def identity(self):
        return canonical_identity(self.to_dict())

    @property
    def reference(self):
        return BlobRef(
            self.identity.digest, len(self.content), media_type="application/json"
        )


@dataclass(frozen=True, slots=True)
class SemanticPayloadReadmissionRequest:
    request_identity: ContentIdentity
    plan: RetainedProjectPlan
    action_id: str
    admission_policy_identity: ContentIdentity
    semantic_inputs: BlobRef
    qualification_inputs: BlobRef
    payload: BlobRef
    prior_result: BlobRef

    def __post_init__(self):
        if not isinstance(self.request_identity, ContentIdentity) or not isinstance(
            self.plan, RetainedProjectPlan
        ):
            raise TypeError(
                "payload re-admission requires a typed current request "
                "and retained plan"
            )
        if any(
            not isinstance(value, BlobRef)
            for value in (
                self.semantic_inputs,
                self.qualification_inputs,
                self.payload,
                self.prior_result,
            )
        ):
            raise TypeError(
                "payload re-admission requires exact immutable byte references"
            )
        if not isinstance(self.admission_policy_identity, ContentIdentity):
            raise TypeError("payload re-admission requires a pinned current policy")
        actions = [
            action
            for action in self.plan.profile.actions
            if action.action_id == self.action_id
        ]
        if len(actions) != 1 or actions[0].stage != "build":
            raise ValueError("payload re-admission requires one current build action")

    def to_dict(self):
        return {
            "schema": "literate-ai/semantic-payload-readmission-request@1",
            "request_identity": self.request_identity.uri,
            "current_plan_identity": self.plan.identity.uri,
            "current_manifest_identity": self.plan.manifest_identity.uri,
            "current_profile_identity": self.plan.profile.identity.uri,
            "action_id": self.action_id,
            "admission_policy_identity": self.admission_policy_identity.uri,
            "semantic_inputs": self.semantic_inputs.to_dict(),
            "qualification_inputs": self.qualification_inputs.to_dict(),
            "payload": self.payload.to_dict(),
            "prior_result": self.prior_result.to_dict(),
        }

    @property
    def identity(self):
        return canonical_identity(self.to_dict())

    @property
    def reference(self):
        content = canonical_json_bytes(self.to_dict())
        return BlobRef(
            self.identity.digest, len(content), media_type="application/json"
        )

    def authorization_request(self, inputs: SemanticBuildInputs):
        if inputs.reference != self.semantic_inputs:
            raise ValueError("payload re-admission semantic inputs differ")
        return BuildRequest(
            effective_revision_digest=self.plan.identity.uri,
            source_bundle_digest=self.identity.uri,
            builder_id="semantic-payload-readmission-v1",
            toolchain_digest=inputs.to_dict()["toolchain"],
            sandbox_profile="retained-artifact-admission",
            requested_privileges=("admit-retained-payload",),
            allowed_outputs=("readmission-receipt",),
        )


@dataclass(frozen=True, slots=True)
class BinaryInputAdoptionAction:
    source_action_id: str
    action: RetainedProjectAction

    def __post_init__(self):
        _text(self.source_action_id)
        if (
            not isinstance(self.action, RetainedProjectAction)
            or self.action.stage != "acquire"
            or not self.action.outputs
            or self.action.action_id == self.source_action_id
        ):
            raise ValueError(
                "binary input requires a distinct acquire action with output custody"
            )

    def to_dict(self):
        return {
            "schema": "literate-ai/binary-input-adoption-action@1",
            "source_action_id": self.source_action_id,
            "action": self.action.to_dict(),
        }

    @classmethod
    def from_dict(cls, value):
        if (
            not isinstance(value, dict)
            or set(value) != {"schema", "source_action_id", "action"}
            or value["schema"] != "literate-ai/binary-input-adoption-action@1"
        ):
            raise ValueError("binary input action fields invalid")
        return cls(
            value["source_action_id"], RetainedProjectAction.from_dict(value["action"])
        )


@dataclass(frozen=True, slots=True)
class PayloadAdmissionPolicy:
    evidence_trust_policy_identity: ContentIdentity
    provenance_workflows: tuple[ContentIdentity, ...]
    qualification_workflows: tuple[ContentIdentity, ...]
    allowed_modes: tuple[str, ...]
    maximum_payload_bytes: int
    allowed_unknown_original_inputs: tuple[str, ...] = ()
    binary_input_actions: tuple[BinaryInputAdoptionAction, ...] = ()

    def __post_init__(self):
        if not isinstance(self.evidence_trust_policy_identity, ContentIdentity):
            raise TypeError("payload policy requires independently pinned trust")
        for values in (self.provenance_workflows, self.qualification_workflows):
            if (
                not isinstance(values, tuple)
                or not values
                or any(not isinstance(item, ContentIdentity) for item in values)
                or tuple(item.uri for item in values)
                != tuple(sorted({item.uri for item in values}))
            ):
                raise ValueError("payload policy requires canonical pinned workflows")
        if (
            not isinstance(self.allowed_modes, tuple)
            or self.allowed_modes != tuple(sorted(set(self.allowed_modes)))
            or not set(self.allowed_modes)
            <= {"semantic-key-reuse", "signed-release-binary-adoption"}
        ):
            raise ValueError(
                "payload admission mode requires explicit policy selection"
            )
        if (
            type(self.maximum_payload_bytes) is not int
            or not 1 <= self.maximum_payload_bytes < 2**63
        ):
            raise ValueError(
                "payload admission requires an explicit positive byte bound"
            )

        unknown = self.allowed_unknown_original_inputs
        if (
            not isinstance(unknown, tuple)
            or unknown != tuple(sorted(set(unknown)))
            or not set(unknown)
            <= {
                "original-compiler",
                "original-toolchain",
                "original-command",
                "original-build-environment",
            }
        ):
            raise ValueError(
                "only explicit original execution-provenance unknowns may be allowed"
            )
        if unknown and "signed-release-binary-adoption" not in self.allowed_modes:
            raise ValueError(
                "unknown original provenance requires binary adoption policy"
            )

        actions = self.binary_input_actions
        if (
            not isinstance(actions, tuple)
            or any(
                not isinstance(action, BinaryInputAdoptionAction) for action in actions
            )
            or tuple(action.action.action_id for action in actions)
            != tuple(sorted({action.action.action_id for action in actions}))
        ):
            raise ValueError(
                "binary adoption actions must be typed, canonical and unique"
            )
        if "signed-release-binary-adoption" in self.allowed_modes and not actions:
            raise ValueError(
                "binary adoption requires explicit alternate action ownership"
            )

    def to_dict(self):
        return {
            "schema": "literate-ai/payload-admission-policy@1",
            "evidence_trust_policy_identity": self.evidence_trust_policy_identity.uri,
            "provenance_workflows": [item.uri for item in self.provenance_workflows],
            "qualification_workflows": [
                item.uri for item in self.qualification_workflows
            ],
            "allowed_modes": list(self.allowed_modes),
            "maximum_payload_bytes": self.maximum_payload_bytes,
            "allowed_unknown_original_inputs": list(
                self.allowed_unknown_original_inputs
            ),
            "binary_input_actions": [
                action.to_dict() for action in self.binary_input_actions
            ],
        }

    @property
    def identity(self):
        return canonical_identity(self.to_dict())

    @classmethod
    def from_dict(cls, value):
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "schema",
                "evidence_trust_policy_identity",
                "provenance_workflows",
                "qualification_workflows",
                "allowed_modes",
                "maximum_payload_bytes",
                "allowed_unknown_original_inputs",
                "binary_input_actions",
            }
            or value["schema"] != "literate-ai/payload-admission-policy@1"
        ):
            raise ValueError("payload admission policy fields invalid")
        return cls(
            ContentIdentity.parse_uri(value["evidence_trust_policy_identity"]),
            tuple(
                ContentIdentity.parse_uri(item)
                for item in value["provenance_workflows"]
            ),
            tuple(
                ContentIdentity.parse_uri(item)
                for item in value["qualification_workflows"]
            ),
            tuple(value["allowed_modes"]),
            value["maximum_payload_bytes"],
            tuple(value["allowed_unknown_original_inputs"]),
            tuple(
                BinaryInputAdoptionAction.from_dict(action)
                for action in value["binary_input_actions"]
            ),
        )


@dataclass(frozen=True, slots=True)
class NativePayloadQualificationInputs:
    content: bytes

    def __post_init__(self):
        if not isinstance(self.content, bytes) or len(self.content) > 16 * 1024 * 1024:
            raise ValueError(
                "native qualification inputs require bounded immutable bytes"
            )
        value = json.loads(self.content)
        names = {
            "schema",
            "profile_identity",
            "test_policy_identity",
            "application_policy_identity",
            "target",
            "base_version",
            "base_artifact",
            "base_runtime_abi",
            "runtime_dependencies",
            "package_scripts_identity",
        }
        if (
            not isinstance(value, dict)
            or set(value) != names
            or value["schema"] != "literate-ai/native-payload-qualification-inputs@1"
            or canonical_json_bytes(value) != self.content
        ):
            raise ValueError("native qualification requires complete canonical inputs")
        for field in (
            "profile_identity",
            "test_policy_identity",
            "application_policy_identity",
            "base_runtime_abi",
            "package_scripts_identity",
        ):
            _identity(value[field])
        _text(value["target"])
        _text(value["base_version"])
        BlobRef.from_dict(value["base_artifact"])
        dependencies = value["runtime_dependencies"]
        _ordered(dependencies, lambda item: item["unit"])
        for item in dependencies:
            if set(item) != {
                "unit",
                "selected_version",
                "version_constraint",
                "soname_contract",
                "artifact",
                "scripts_identity",
            }:
                raise ValueError("runtime qualification dependency fields invalid")
            for field in ("unit", "selected_version", "version_constraint"):
                _text(item[field])
            _identity(item["soname_contract"])
            _identity(item["scripts_identity"])
            BlobRef.from_dict(item["artifact"])

    @classmethod
    def from_dict(cls, value):
        return cls(canonical_json_bytes(value))

    def to_dict(self):
        return json.loads(self.content)

    @property
    def identity(self):
        return canonical_identity(self.to_dict())

    @property
    def reference(self):
        return BlobRef(
            self.identity.digest, len(self.content), media_type="application/json"
        )


KNOWN_ORIGINAL_SOURCE_FIELDS = frozenset(
    {
        "target",
        "base_build_abi",
        "members",
        "member_owners",
        "build_dependencies",
        "generated_inputs",
        "options",
        "flavor",
    }
)


@dataclass(frozen=True, slots=True)
class KnownOriginalBinaryFacts:
    content: bytes

    def __post_init__(self):
        if not isinstance(self.content, bytes) or len(self.content) > 16 * 1024 * 1024:
            raise ValueError("known original facts require bounded immutable bytes")
        value = json.loads(self.content)
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "schema",
                "known_source_inputs",
                "known_command",
                "known_environment",
                "known_tools",
                "unknown_original_inputs",
            }
            or value["schema"] != "literate-ai/signed-binary-known-producer-facts@1"
            or canonical_json_bytes(value) != self.content
        ):
            raise ValueError("known original facts require complete canonical fields")
        known = value["known_source_inputs"]
        if not isinstance(known, dict) or set(known) != KNOWN_ORIGINAL_SOURCE_FIELDS:
            raise ValueError(
                "original source/header/static/generated/options facts must be explicit"
            )
        unknown = value["unknown_original_inputs"]
        _ordered(unknown, lambda item: item)
        if not set(unknown) <= {
            "original-compiler",
            "original-toolchain",
            "original-command",
            "original-build-environment",
        }:
            raise ValueError(
                "missing ownership/header/graph edges cannot be hidden "
                "as compiler provenance"
            )
        for field, label in (
            ("known_command", "original-command"),
            ("known_environment", "original-build-environment"),
            ("known_tools", "original-toolchain"),
        ):
            if value[field] is None and label not in unknown:
                raise ValueError(
                    "missing original execution facts must stay explicitly unknown"
                )
            if value[field] is not None and not isinstance(value[field], list):
                raise ValueError(
                    "known original execution facts require arrays or explicit unknowns"
                )

    @classmethod
    def from_dict(cls, value):
        return cls(canonical_json_bytes(value))

    def to_dict(self):
        return json.loads(self.content)

    @property
    def reference(self):
        identity = canonical_identity(self.to_dict())
        return BlobRef(
            identity.digest, len(self.content), media_type="application/json"
        )


@dataclass(frozen=True, slots=True)
class SignedReleaseBinaryAdoptionRequest:
    request_identity: ContentIdentity
    plan: RetainedProjectPlan
    action_id: str
    source_action_id: str
    admission_policy_identity: ContentIdentity
    current_build_inputs: BlobRef
    qualification_inputs: BlobRef
    payload: BlobRef
    signed_release: BlobRef
    signed_catalogue: BlobRef
    known_producer_facts: BlobRef
    unknown_original_inputs: tuple[str, ...]

    def __post_init__(self):
        if (
            not isinstance(self.request_identity, ContentIdentity)
            or not isinstance(self.plan, RetainedProjectPlan)
            or not isinstance(self.admission_policy_identity, ContentIdentity)
        ):
            raise TypeError(
                "binary adoption requires typed current request/plan/policy"
            )
        if any(
            not isinstance(value, BlobRef)
            for value in (
                self.current_build_inputs,
                self.qualification_inputs,
                self.payload,
                self.signed_release,
                self.signed_catalogue,
                self.known_producer_facts,
            )
        ):
            raise TypeError("binary adoption requires exact immutable input references")
        actions = [
            action
            for action in self.plan.profile.actions
            if action.action_id == self.source_action_id
        ]
        if (
            len(actions) != 1
            or actions[0].stage != "build"
            or self.action_id == self.source_action_id
            or not actions[0].outputs
        ):
            raise ValueError(
                "binary input adoption must bind the prospective source build "
                "through a distinct policy-owned acquire action"
            )
        unknown = self.unknown_original_inputs
        if (
            not isinstance(unknown, tuple)
            or unknown != tuple(sorted(set(unknown)))
            or not set(unknown)
            <= {
                "original-compiler",
                "original-toolchain",
                "original-command",
                "original-build-environment",
            }
        ):
            raise ValueError(
                "binary adoption unknowns must remain explicit and bounded"
            )

    def to_dict(self):
        return {
            "schema": "literate-ai/signed-release-binary-adoption-request@1",
            "request_identity": self.request_identity.uri,
            "current_plan_identity": self.plan.identity.uri,
            "current_manifest_identity": self.plan.manifest_identity.uri,
            "current_profile_identity": self.plan.profile.identity.uri,
            "action_id": self.action_id,
            "source_action_id": self.source_action_id,
            "admission_policy_identity": self.admission_policy_identity.uri,
            "current_build_inputs": self.current_build_inputs.to_dict(),
            "qualification_inputs": self.qualification_inputs.to_dict(),
            "payload": self.payload.to_dict(),
            "signed_release": self.signed_release.to_dict(),
            "signed_catalogue": self.signed_catalogue.to_dict(),
            "known_producer_facts": self.known_producer_facts.to_dict(),
            "unknown_original_inputs": list(self.unknown_original_inputs),
        }

    @property
    def identity(self):
        return canonical_identity(self.to_dict())

    @property
    def reference(self):
        content = canonical_json_bytes(self.to_dict())
        return BlobRef(
            self.identity.digest, len(content), media_type="application/json"
        )

    def authorization_request(self, inputs: SemanticBuildInputs):
        if inputs.reference != self.current_build_inputs:
            raise ValueError("binary adoption current build inputs differ")
        return BuildRequest(
            effective_revision_digest=self.plan.identity.uri,
            source_bundle_digest=self.identity.uri,
            builder_id="signed-release-binary-adoption-v1",
            toolchain_digest=inputs.to_dict()["toolchain"],
            sandbox_profile="retained-artifact-admission",
            requested_privileges=("adopt-signed-release-binary",),
            allowed_outputs=("binary-input-adoption-receipt",),
        )
