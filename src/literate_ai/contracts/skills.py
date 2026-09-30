"""Exact specification-to-source skill contracts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, ClassVar

from ._validation import (
    contract_fields,
    fail,
    parse_tuple,
    string_tuple,
    string_value,
    unique,
)
from .generation_cache import source_cache_model_selector
from .identity import SCHEMA_PREFIX, ContentIdentity, ContentReference
from .versioning import semantic_version

SPECIFICATION_TO_SOURCE_SKILL_SCHEMA = f"{SCHEMA_PREFIX}specification-to-source-skill"
SKILL_REFERENCE_SCHEMA = f"{SCHEMA_PREFIX}skill-reference"
_CODING_CLIS = frozenset({"codex", "claude", "cursor-agent", "opencode"})


def _output_tree(value: str, path: str) -> str:
    raw = string_value(value, path)
    if not raw.startswith("source/") or raw == "source/" or "\\" in raw:
        fail(path, "must be a source/ subdirectory such as source/backend")
    if any(part in ("", ".", "..") for part in raw.split("/")):
        fail(path, "must be a normalized POSIX path under source/")
    return raw


@dataclass(frozen=True, slots=True)
class SkillReference:
    """An exact dependency on one skill revision."""

    skill_id: str
    version: str
    identity: ContentIdentity

    SCHEMA: ClassVar[str] = SKILL_REFERENCE_SCHEMA

    def __post_init__(self) -> None:
        string_value(self.skill_id, "SkillReference.skill_id")
        semantic_version(self.version, "SkillReference.version")
        if not isinstance(self.identity, ContentIdentity):
            fail("SkillReference.identity", "must be a ContentIdentity")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "skill_id": self.skill_id,
            "version": self.version,
            "identity": self.identity.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Any, *, path: str = "SkillReference") -> SkillReference:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset({"skill_id", "version", "identity"}),
        )
        return cls(
            string_value(data["skill_id"], f"{path}.skill_id"),
            semantic_version(data["version"], f"{path}.version"),
            ContentIdentity.from_dict(data["identity"], path=f"{path}.identity"),
        )


@dataclass(frozen=True, slots=True)
class SpecificationToSourceSkill:
    """Portable manifest describing one reviewed conversion technique."""

    skill_id: str
    version: str
    title: str
    stages: tuple[str, ...]
    dependencies: tuple[SkillReference, ...]
    instructions: str
    limitations: tuple[str, ...]
    trust: str
    models: tuple[tuple[str, str], ...] = ()
    output_trees: tuple[str, ...] = ()

    SCHEMA: ClassVar[str] = SPECIFICATION_TO_SOURCE_SKILL_SCHEMA

    def __post_init__(self) -> None:
        string_value(self.skill_id, "SpecificationToSourceSkill.skill_id")
        semantic_version(self.version, "SpecificationToSourceSkill.version")
        string_value(self.title, "SpecificationToSourceSkill.title")
        string_value(
            self.instructions,
            "SpecificationToSourceSkill.instructions",
            max_length=65536,
        )
        string_value(self.trust, "SpecificationToSourceSkill.trust")
        for label, values in (
            ("stages", self.stages),
            ("limitations", self.limitations),
        ):
            if not values:
                fail(f"SpecificationToSourceSkill.{label}", "must not be empty")
            unique(values, f"SpecificationToSourceSkill.{label}")
            for index, item in enumerate(values):
                string_value(item, f"SpecificationToSourceSkill.{label}[{index}]")
        dependency_ids = tuple(item.skill_id for item in self.dependencies)
        unique(
            dependency_ids,
            "SpecificationToSourceSkill.dependencies",
            "skill IDs",
        )
        providers = tuple(provider for provider, _selector in self.models)
        unique(providers, "SpecificationToSourceSkill.models", "coding CLI providers")
        if any(provider not in _CODING_CLIS for provider in providers):
            fail(
                "SpecificationToSourceSkill.models",
                "may select models only for codex, claude, cursor-agent, or opencode",
            )
        for provider, selector in self.models:
            source_cache_model_selector(
                selector,
                path=f"SpecificationToSourceSkill.models[{provider!r}]",
            )
        unique(self.output_trees, "SpecificationToSourceSkill.output_trees")
        for index, tree in enumerate(self.output_trees):
            _output_tree(tree, f"SpecificationToSourceSkill.output_trees[{index}]")

    def model_for(self, coding_cli: str) -> str | None:
        return dict(self.models).get(coding_cli)

    @property
    def dependency_ids(self) -> tuple[str, ...]:
        return tuple(item.skill_id for item in self.dependencies)

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "schema": self.SCHEMA,
            "skill_id": self.skill_id,
            "version": self.version,
            "title": self.title,
            "stages": list(self.stages),
            "dependencies": [item.to_dict() for item in self.dependencies],
            "instructions": self.instructions,
            "limitations": list(self.limitations),
            "trust": self.trust,
        }
        if self.models:
            value["models"] = dict(self.models)
        if self.output_trees:
            value["output_trees"] = list(self.output_trees)
        return value

    @classmethod
    def from_dict(
        cls, value: Any, *, path: str = "SpecificationToSourceSkill"
    ) -> SpecificationToSourceSkill:
        data = contract_fields(
            value,
            path=path,
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "skill_id",
                    "version",
                    "title",
                    "stages",
                    "dependencies",
                    "instructions",
                    "limitations",
                    "trust",
                }
            ),
            optional=frozenset({"models", "output_trees"}),
        )
        raw_models = data.get("models", {})
        if not isinstance(raw_models, dict):
            fail(f"{path}.models", "must be an object")
        return cls(
            skill_id=string_value(data["skill_id"], f"{path}.skill_id"),
            version=semantic_version(data["version"], f"{path}.version"),
            title=string_value(data["title"], f"{path}.title"),
            stages=string_tuple(data["stages"], f"{path}.stages"),
            dependencies=parse_tuple(
                data["dependencies"], f"{path}.dependencies", SkillReference.from_dict
            ),
            instructions=string_value(
                data["instructions"], f"{path}.instructions", max_length=65536
            ),
            limitations=string_tuple(data["limitations"], f"{path}.limitations"),
            trust=string_value(data["trust"], f"{path}.trust"),
            models=tuple(
                sorted(
                    (
                        string_value(provider, f"{path}.models provider"),
                        string_value(selector, f"{path}.models[{provider!r}]"),
                    )
                    for provider, selector in raw_models.items()
                )
            ),
            output_trees=string_tuple(
                data.get("output_trees", []), f"{path}.output_trees"
            ),
        )


@dataclass(frozen=True, slots=True)
class ResolvedSpecificationToSourceSkill:
    """A manifest bound to its exact file bytes and selection source."""

    manifest: SpecificationToSourceSkill
    content_identity: ContentIdentity
    source: str

    def __post_init__(self) -> None:
        if not isinstance(self.manifest, SpecificationToSourceSkill):
            fail(
                "ResolvedSpecificationToSourceSkill.manifest",
                "must be a SpecificationToSourceSkill",
            )
        if not isinstance(self.content_identity, ContentIdentity):
            fail(
                "ResolvedSpecificationToSourceSkill.content_identity",
                "must be a ContentIdentity",
            )
        string_value(self.source, "ResolvedSpecificationToSourceSkill.source")

    @property
    def skill_id(self) -> str:
        return self.manifest.skill_id

    @property
    def version(self) -> str:
        return self.manifest.version

    @property
    def title(self) -> str:
        return self.manifest.title

    @property
    def stages(self) -> tuple[str, ...]:
        return self.manifest.stages

    @property
    def dependencies(self) -> tuple[SkillReference, ...]:
        return self.manifest.dependencies

    @property
    def dependency_ids(self) -> tuple[str, ...]:
        return self.manifest.dependency_ids

    @property
    def instructions(self) -> str:
        return self.manifest.instructions

    @property
    def limitations(self) -> tuple[str, ...]:
        return self.manifest.limitations

    @property
    def trust(self) -> str:
        return self.manifest.trust

    @property
    def models(self) -> tuple[tuple[str, str], ...]:
        return self.manifest.models

    @property
    def output_trees(self) -> tuple[str, ...]:
        return self.manifest.output_trees

    def model_for(self, coding_cli: str) -> str | None:
        return self.manifest.model_for(coding_cli)

    @property
    def identity(self) -> str:
        return self.content_identity.uri

    @property
    def ref(self) -> SkillReference:
        return SkillReference(self.skill_id, self.version, self.content_identity)

    @classmethod
    def from_reference(
        cls,
        reference: ContentReference,
        content: bytes,
        *,
        source: str,
    ) -> ResolvedSpecificationToSourceSkill:
        if reference.kind != "specification-to-source-skill":
            fail(
                "ResolvedSpecificationToSourceSkill.reference.kind",
                "must identify a specification-to-source skill",
            )
        identity = ContentIdentity.parse_uri(
            f"sha256:{hashlib.sha256(content).hexdigest()}"
        )
        if identity != reference.identity:
            fail(
                "ResolvedSpecificationToSourceSkill.reference.identity",
                f"skill identity changed: {reference.uri}",
            )
        if reference.uri.casefold().endswith(".md"):
            # The content reference selects the human-authoring codec while the parsed
            # value still passes through the same strict typed contract.
            from .authoring_markdown import (
                AuthoringMarkdownError,
                parse_authoring_markdown,
                project_typed_agent_skill,
            )

            try:
                value, instructions = parse_authoring_markdown(
                    content, source=reference.uri
                )
                value, instructions = project_typed_agent_skill(
                    value, instructions, source=reference.uri
                )
            except AuthoringMarkdownError:
                fail(
                    "ResolvedSpecificationToSourceSkill",
                    f"skill must be canonical Markdown: {reference.uri}",
                )
            value = dict(value)
            if "instructions" in value:
                fail(
                    "ResolvedSpecificationToSourceSkill",
                    "skill instructions must exist only in the Markdown body: "
                    f"{reference.uri}",
                )
            value["instructions"] = instructions
        else:
            try:
                value = json.loads(content.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError):
                fail(
                    "ResolvedSpecificationToSourceSkill",
                    f"legacy skill must be UTF-8 JSON: {reference.uri}",
                )
        return cls(
            SpecificationToSourceSkill.from_dict(value),
            identity,
            source,
        )

    def to_dict(self) -> dict[str, object]:
        """Return safe provenance for reports, excluding prompt instructions."""

        value: dict[str, object] = {
            "skill_id": self.skill_id,
            "version": self.version,
            "title": self.title,
            "stages": list(self.stages),
            "dependencies": [item.to_dict() for item in self.dependencies],
            "limitations": list(self.limitations),
            "trust": self.trust,
            "identity": self.identity,
            "source": self.source,
        }
        if self.models:
            value["models"] = dict(self.models)
        if self.output_trees:
            value["output_trees"] = list(self.output_trees)
        return value


__all__ = [
    "SKILL_REFERENCE_SCHEMA",
    "SPECIFICATION_TO_SOURCE_SKILL_SCHEMA",
    "ResolvedSpecificationToSourceSkill",
    "SkillReference",
    "SpecificationToSourceSkill",
]
