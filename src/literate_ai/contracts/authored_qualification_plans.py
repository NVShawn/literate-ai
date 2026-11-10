"""Read-only authored qualification proposals, never saved execution grants."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from ._validation import contract_fields, fail, int_value, list_value, string_value
from .executable_components import ComponentExecutionPlan
from .identity import ContentIdentity, contract_identity
from .paths import canonical_relative_posix_path
from .qualification_origins import AuthoredQualificationOrigin


@dataclass(frozen=True, slots=True)
class AuthoredQualificationProjectReview:
    project_configuration_identity: ContentIdentity
    reviewed_authority_identity: ContentIdentity

    SCHEMA: ClassVar[str] = (
        "urn:literate-ai:schema:v2:authored-qualification-project-review"
    )

    def __post_init__(self):
        for name in ("project_configuration_identity", "reviewed_authority_identity"):
            if not isinstance(getattr(self, name), ContentIdentity):
                fail(name, "must be a ContentIdentity")

    def to_dict(self):
        return {
            "schema": self.SCHEMA,
            "project_configuration_identity": (
                self.project_configuration_identity.to_dict()
            ),
            "reviewed_authority_identity": self.reviewed_authority_identity.to_dict(),
        }

    @classmethod
    def from_dict(cls, value):
        data = contract_fields(
            value,
            path=cls.__name__,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {"project_configuration_identity", "reviewed_authority_identity"}
            ),
        )
        return cls(
            ContentIdentity.from_dict(data["project_configuration_identity"]),
            ContentIdentity.from_dict(data["reviewed_authority_identity"]),
        )


@dataclass(frozen=True, slots=True)
class AuthoredQualificationPlan:
    origin: AuthoredQualificationOrigin
    project_reviews: tuple[AuthoredQualificationProjectReview, ...]
    profile_identity: ContentIdentity
    oracle_set_identity: ContentIdentity
    execution_plan: ComponentExecutionPlan
    coding_provider_binding_identity: ContentIdentity
    driver_identity: ContentIdentity
    lifecycle_policy_identity: ContentIdentity
    framework_distribution_identity: ContentIdentity
    output_root: str
    minimum_clean_runs: int

    SCHEMA: ClassVar[str] = "urn:literate-ai:schema:v2:authored-qualification-plan"
    _IDENTITIES: ClassVar[tuple[str, ...]] = (
        "profile_identity",
        "oracle_set_identity",
        "coding_provider_binding_identity",
        "driver_identity",
        "lifecycle_policy_identity",
        "framework_distribution_identity",
    )

    def __post_init__(self):
        if not isinstance(self.origin, AuthoredQualificationOrigin):
            fail("origin", "must be authored qualification origin")
        for name in self._IDENTITIES:
            if not isinstance(getattr(self, name), ContentIdentity):
                fail(name, "must be a ContentIdentity")
        if not isinstance(self.execution_plan, ComponentExecutionPlan):
            fail("execution_plan", "must be an actual ComponentExecutionPlan")
        if (
            self.execution_plan.component_lock_identity
            != self.origin.component_lock_identity
        ):
            fail("execution_plan", "must select the original complete authored lock")
        if (
            not isinstance(self.project_reviews, tuple)
            or not self.project_reviews
            or any(
                not isinstance(r, AuthoredQualificationProjectReview)
                for r in self.project_reviews
            )
        ):
            fail("project_reviews", "must be a nonempty typed tuple")
        keys = tuple(
            (r.project_configuration_identity.uri, r.reviewed_authority_identity.uri)
            for r in self.project_reviews
        )
        if keys != tuple(sorted(set(keys))):
            fail("project_reviews", "must be unique and in canonical order")
        if self.origin.project_configuration_identity not in {
            r.project_configuration_identity for r in self.project_reviews
        }:
            fail("project_reviews", "must include the selected root project's review")
        path = canonical_relative_posix_path(
            string_value(self.output_root, "output_root"),
            label="qualification output root",
        )
        if path.parts[:2] != ("litai", "qualification"):
            fail("output_root", "must be inside litai/qualification")
        int_value(self.minimum_clean_runs, "minimum_clean_runs", minimum=2)

    @property
    def identity(self):
        return contract_identity(self)

    def to_dict(self):
        return {
            "schema": self.SCHEMA,
            "origin": self.origin.to_dict(),
            "project_reviews": [r.to_dict() for r in self.project_reviews],
            **{name: getattr(self, name).to_dict() for name in self._IDENTITIES},
            "execution_plan": self.execution_plan.to_dict(),
            "output_root": self.output_root,
            "minimum_clean_runs": self.minimum_clean_runs,
        }

    @classmethod
    def from_dict(cls, value):
        data = contract_fields(
            value,
            path=cls.__name__,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "origin",
                    "project_reviews",
                    "execution_plan",
                    "output_root",
                    "minimum_clean_runs",
                    *cls._IDENTITIES,
                }
            ),
        )
        return cls(
            origin=AuthoredQualificationOrigin.from_dict(data["origin"]),
            project_reviews=tuple(
                AuthoredQualificationProjectReview.from_dict(r)
                for r in list_value(data["project_reviews"], "project_reviews")
            ),
            **{name: ContentIdentity.from_dict(data[name]) for name in cls._IDENTITIES},
            execution_plan=ComponentExecutionPlan.from_dict(data["execution_plan"]),
            output_root=data["output_root"],
            minimum_clean_runs=data["minimum_clean_runs"],
        )
