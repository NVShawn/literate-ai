"""Independent, project-bound inputs to public retained-evidence verification."""

from dataclasses import dataclass
from typing import ClassVar

from literate_ai.contracts._validation import contract_fields, fields, list_value
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.identity import ContentIdentity, canonical_identity

from .graph import EvidenceRunRequirement
from .records import EvidenceArtifact
from .trust import RunEvidenceExpectation


@dataclass(frozen=True, slots=True)
class EvidenceVerificationPlan:
    """Verifier authority supplied independently of producer envelopes and locators."""

    project_authority: ContentIdentity
    root: BlobRef
    requirements: tuple[EvidenceRunRequirement, ...]

    SCHEMA: ClassVar[str] = "urn:literate-ai:schema:v2:evidence-verification-plan"

    def __post_init__(self):
        if (
            not isinstance(self.project_authority, ContentIdentity)
            or not isinstance(self.root, BlobRef)
            or not isinstance(self.requirements, tuple)
            or not 1 <= len(self.requirements) <= 1024
            or any(not isinstance(r, EvidenceRunRequirement) for r in self.requirements)
        ):
            raise ValueError("evidence.plan.invalid")
        keys = tuple(r.envelope.identity for r in self.requirements)
        if keys != tuple(sorted(set(keys))) or self.root not in tuple(
            r.envelope for r in self.requirements
        ):
            raise ValueError("evidence.plan.invalid")

    @property
    def identity(self):
        return canonical_identity(self.to_dict())

    def to_dict(self):
        return {
            "schema": self.SCHEMA,
            "project_authority": self.project_authority.to_dict(),
            "root": self.root.to_dict(),
            "requirements": [r.to_dict() for r in self.requirements],
        }

    @classmethod
    def from_dict(cls, value):
        data = contract_fields(
            value,
            path="EvidenceVerificationPlan",
            schema_uri=cls.SCHEMA,
            required=frozenset({"project_authority", "root", "requirements"}),
        )
        requirements = []
        values = list_value(data["requirements"], "requirements")
        if not 1 <= len(values) <= 1024:
            raise ValueError("evidence.plan.invalid")
        for value in values:
            r = fields(
                value,
                path="requirement",
                required=frozenset(
                    {"envelope", "expectation", "children", "artifacts"}
                ),
            )
            artifacts = []
            for item in list_value(r["artifacts"], "artifacts"):
                pair = list_value(item, "artifact")
                if len(pair) != 2:
                    raise ValueError("evidence.plan.invalid")
                artifacts.append((pair[0], BlobRef.from_dict(pair[1])))
            requirements.append(
                EvidenceRunRequirement(
                    BlobRef.from_dict(r["envelope"]),
                    RunEvidenceExpectation.from_dict(r["expectation"]),
                    tuple(
                        EvidenceArtifact.from_dict(c)
                        for c in list_value(r["children"], "children")
                    ),
                    tuple(artifacts),
                )
            )
        return cls(
            ContentIdentity.from_dict(data["project_authority"]),
            BlobRef.from_dict(data["root"]),
            tuple(requirements),
        )
