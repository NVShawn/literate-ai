"""Versioned, immutable policy authority for the Standard project lifecycle."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from importlib.resources import files
from typing import Any, ClassVar

from ._validation import (
    contract_fields,
    fail,
    int_value,
    parse_tuple,
    string_tuple,
    string_value,
    unique,
)
from .identity import ContentIdentity, contract_identity
from .projects import (
    MINIMUM_PROJECT_REBUILD_PHASES,
    PROJECT_LIFECYCLE_EXTENSION_PHASES,
)
from .testing import (
    PROJECT_TEST_EVIDENCE_KINDS,
    PROJECT_TEST_RUNNER_EVIDENCE_KIND,
)
from .versioning import semantic_version

STANDARD_LIFECYCLE_POLICY_SCHEMA = "urn:literate-ai:schema:v2:standard-lifecycle-policy"
STANDARD_LIFECYCLE_EXTENSION_EVIDENCE_SCHEMA = (
    "urn:literate-ai:schema:v2:standard-lifecycle-extension-evidence"
)
CURRENT_STANDARD_LIFECYCLE_POLICY_RESOURCE = "standard-lifecycle-v1.json"

# This is semantic receipt policy, not an implementation detail of one CLI adapter.
# Keep it canonical and content-identify it through StandardLifecyclePolicy.
STANDARD_FULL_REBUILD_EVIDENCE_KINDS = tuple(
    sorted(
        {
            "acceptance-result",
            "build-result",
            "source-intelligence",
            "generation-provenance",
            "lifecycle-command",
            "lifecycle-plan",
            "lifecycle-request",
            "observation-result",
            "resolved-sbom",
            "security-scan-report",
            "source-cache-decision",
            "source-cache-lifecycle",
            "source-sbom",
            "test-report",
            "test-runner",
            "workspace-admission",
        }
    )
)

_POLICY_ID = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")


def _canonical_evidence_kinds(value: object, path: str) -> tuple[str, ...]:
    kinds = string_tuple(value, path)
    if not kinds:
        fail(path, "must not be empty")
    unique(kinds, path, "evidence kinds")
    if kinds != tuple(sorted(kinds)):
        fail(path, "must use canonical evidence-kind order")
    unknown = set(kinds) - PROJECT_TEST_EVIDENCE_KINDS
    if unknown:
        fail(path, "contains unsupported evidence kinds: " + ", ".join(sorted(unknown)))
    return kinds


@dataclass(frozen=True, slots=True)
class StandardLifecycleExtensionEvidence:
    """Receipt evidence required when one optional lifecycle phase is selected."""

    phase: str
    evidence_kind: str

    SCHEMA: ClassVar[str] = STANDARD_LIFECYCLE_EXTENSION_EVIDENCE_SCHEMA

    def __post_init__(self) -> None:
        string_value(self.phase, "StandardLifecycleExtensionEvidence.phase")
        if self.phase not in PROJECT_LIFECYCLE_EXTENSION_PHASES:
            fail(
                "StandardLifecycleExtensionEvidence.phase",
                "must be a supported project lifecycle extension",
            )
        string_value(
            self.evidence_kind,
            "StandardLifecycleExtensionEvidence.evidence_kind",
        )
        if self.evidence_kind not in PROJECT_TEST_EVIDENCE_KINDS:
            fail(
                "StandardLifecycleExtensionEvidence.evidence_kind",
                "must be a supported project test evidence kind",
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "phase": self.phase,
            "evidence_kind": self.evidence_kind,
        }

    @classmethod
    def from_dict(
        cls,
        value: Any,
        *,
        path: str = "StandardLifecycleExtensionEvidence",
    ) -> StandardLifecycleExtensionEvidence:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"phase", "evidence_kind"}),
        )
        return cls(
            phase=string_value(data["phase"], f"{path}.phase"),
            evidence_kind=string_value(data["evidence_kind"], f"{path}.evidence_kind"),
        )


@dataclass(frozen=True, slots=True)
class StandardLifecyclePolicy:
    """Exact Standard phase and receipt policy selected by a project binding."""

    policy_id: str
    policy_version: str
    phases: tuple[str, ...]
    required_evidence_kinds: tuple[str, ...]
    extension_evidence: tuple[StandardLifecycleExtensionEvidence, ...]
    minimum_test_count: int

    SCHEMA: ClassVar[str] = STANDARD_LIFECYCLE_POLICY_SCHEMA

    def __post_init__(self) -> None:
        string_value(self.policy_id, "StandardLifecyclePolicy.policy_id")
        if _POLICY_ID.fullmatch(self.policy_id) is None:
            fail(
                "StandardLifecyclePolicy.policy_id",
                "must be a normalized lowercase policy identifier",
            )
        semantic_version(
            self.policy_version,
            "StandardLifecyclePolicy.policy_version",
        )
        if not isinstance(self.phases, tuple):
            fail("StandardLifecyclePolicy.phases", "must be a tuple")
        unique(self.phases, "StandardLifecyclePolicy.phases", "lifecycle phases")
        prefix = MINIMUM_PROJECT_REBUILD_PHASES[:-1]
        suffix = MINIMUM_PROJECT_REBUILD_PHASES[-1:]
        if self.phases[: len(prefix)] != prefix or self.phases[-1:] != suffix:
            fail(
                "StandardLifecyclePolicy.phases",
                "must preserve the complete Standard lifecycle phase order",
            )
        extensions = self.phases[len(prefix) : -1]
        expected_extensions = tuple(
            phase for phase in PROJECT_LIFECYCLE_EXTENSION_PHASES if phase in extensions
        )
        if extensions != expected_extensions:
            fail(
                "StandardLifecyclePolicy.phases",
                "extensions must be supported and use canonical phase order",
            )
        _canonical_evidence_kinds(
            self.required_evidence_kinds,
            "StandardLifecyclePolicy.required_evidence_kinds",
        )
        if PROJECT_TEST_RUNNER_EVIDENCE_KIND not in self.required_evidence_kinds:
            fail(
                "StandardLifecyclePolicy.required_evidence_kinds",
                f"must include {PROJECT_TEST_RUNNER_EVIDENCE_KIND!r}",
            )
        if not isinstance(self.extension_evidence, tuple):
            fail("StandardLifecyclePolicy.extension_evidence", "must be a tuple")
        for index, item in enumerate(self.extension_evidence):
            if not isinstance(item, StandardLifecycleExtensionEvidence):
                fail(
                    f"StandardLifecyclePolicy.extension_evidence[{index}]",
                    "must be StandardLifecycleExtensionEvidence",
                )
        extension_phases = tuple(item.phase for item in self.extension_evidence)
        if extension_phases != PROJECT_LIFECYCLE_EXTENSION_PHASES:
            fail(
                "StandardLifecyclePolicy.extension_evidence",
                "must cover every supported extension in canonical phase order",
            )
        unique(
            tuple(item.evidence_kind for item in self.extension_evidence),
            "StandardLifecyclePolicy.extension_evidence",
            "extension evidence kinds",
        )
        int_value(
            self.minimum_test_count,
            "StandardLifecyclePolicy.minimum_test_count",
            minimum=1,
        )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def evidence_kind_for_extension(self, phase: str) -> str:
        for item in self.extension_evidence:
            if item.phase == phase:
                return item.evidence_kind
        fail(
            "StandardLifecyclePolicy.extension_evidence",
            f"has no evidence binding for extension {phase!r}",
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "phases": list(self.phases),
            "required_evidence_kinds": list(self.required_evidence_kinds),
            "extension_evidence": [item.to_dict() for item in self.extension_evidence],
            "minimum_test_count": self.minimum_test_count,
        }

    @classmethod
    def from_dict(
        cls,
        value: Any,
        *,
        path: str = "StandardLifecyclePolicy",
    ) -> StandardLifecyclePolicy:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "policy_id",
                    "policy_version",
                    "phases",
                    "required_evidence_kinds",
                    "extension_evidence",
                    "minimum_test_count",
                }
            ),
        )
        return cls(
            policy_id=string_value(data["policy_id"], f"{path}.policy_id"),
            policy_version=string_value(
                data["policy_version"], f"{path}.policy_version"
            ),
            phases=string_tuple(data["phases"], f"{path}.phases"),
            required_evidence_kinds=_canonical_evidence_kinds(
                data["required_evidence_kinds"],
                f"{path}.required_evidence_kinds",
            ),
            extension_evidence=parse_tuple(
                data["extension_evidence"],
                f"{path}.extension_evidence",
                StandardLifecycleExtensionEvidence.from_dict,
            ),
            minimum_test_count=int_value(
                data["minimum_test_count"],
                f"{path}.minimum_test_count",
                minimum=1,
            ),
        )


def load_current_standard_lifecycle_policy() -> StandardLifecyclePolicy:
    """Load and validate the immutable Standard policy shipped in this distribution."""

    resource = files("literate_ai.standard_policies").joinpath(
        CURRENT_STANDARD_LIFECYCLE_POLICY_RESOURCE
    )
    try:
        raw = json.loads(resource.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "installed Standard lifecycle policy is unavailable"
        ) from exc
    return StandardLifecyclePolicy.from_dict(raw)


__all__ = [
    "CURRENT_STANDARD_LIFECYCLE_POLICY_RESOURCE",
    "STANDARD_FULL_REBUILD_EVIDENCE_KINDS",
    "STANDARD_LIFECYCLE_EXTENSION_EVIDENCE_SCHEMA",
    "STANDARD_LIFECYCLE_POLICY_SCHEMA",
    "StandardLifecycleExtensionEvidence",
    "StandardLifecyclePolicy",
    "load_current_standard_lifecycle_policy",
]
