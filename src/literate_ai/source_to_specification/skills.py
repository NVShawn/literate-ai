"""Declarative skill loading and deterministic skill-set resolution."""

from __future__ import annotations

import hashlib
import importlib.resources
import json
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

from literate_ai.application.agent_skill_catalog import (
    AgentSkillCatalog,
    AgentSkillCatalogError,
)
from literate_ai.contracts.authoring_markdown import (
    AuthoringMarkdownError,
    parse_authoring_markdown,
    project_typed_agent_skill,
)

from .contracts import SkillRef, SpecAuthoringSkill, SpecAuthoringSkillSet
from .errors import SourceToSpecificationError

_MANIFEST_KEYS = {
    "schema",
    "skill_id",
    "version",
    "title",
    "capabilities",
    "facets",
    "evidence_kinds",
    "model_capabilities",
    "dependencies",
    "after",
    "limitations",
    "trust_classification",
    "prompt_template",
    "extensions",
    "models",
}
_BUILTIN_ORDER = (
    "architecture",
    "api-surface",
    "behavior-state",
    "tests",
    "security",
    "operations",
)
_BUILTIN_LANGUAGE_SKILLS = (
    ("python", frozenset({"python"}), "language-python"),
    ("cpp", frozenset({"cpp"}), "language-cpp"),
    ("rust", frozenset({"rust"}), "language-rust"),
    (
        "javascript",
        frozenset({"javascript", "typescript"}),
        "language-javascript",
    ),
)


class SkillCatalog(Mapping[str, SpecAuthoringSkill]):
    """Version-aware skill catalog with fail-closed legacy ID lookup."""

    def __init__(self) -> None:
        self._by_ref: dict[SkillRef, SpecAuthoringSkill] = {}
        self._by_id: dict[str, list[SpecAuthoringSkill]] = {}

    def add(self, skill: SpecAuthoringSkill) -> None:
        existing = self._by_ref.get(skill.ref)
        if existing is not None and existing != skill:
            raise SourceToSpecificationError(
                "skill.identity_conflict",
                f"conflicting skill metadata for exact ref: {skill.skill_id}",
            )
        if existing is not None:
            return
        self._by_ref[skill.ref] = skill
        self._by_id.setdefault(skill.skill_id, []).append(skill)

    def exact(self, reference: SkillRef) -> SpecAuthoringSkill:
        try:
            return self._by_ref[reference]
        except KeyError as exc:
            raise SourceToSpecificationError(
                "skill.not_found", f"skill ref is not installed: {reference}"
            ) from exc

    def revisions(self, skill_id: str) -> tuple[SpecAuthoringSkill, ...]:
        return tuple(self._by_id.get(skill_id, ()))

    def __getitem__(self, skill_id: str) -> SpecAuthoringSkill:
        candidates = self.revisions(skill_id)
        if not candidates:
            raise KeyError(skill_id)
        if len(candidates) != 1:
            raise SourceToSpecificationError(
                "skill.legacy_id_ambiguous",
                f"skill {skill_id!r} requires an exact SkillRef",
            )
        return candidates[0]

    def __iter__(self) -> Iterator[str]:
        return iter(self._by_id)

    def __len__(self) -> int:
        return len(self._by_id)


def _string_tuple(raw: dict[str, Any], key: str) -> tuple[str, ...]:
    value = raw.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SourceToSpecificationError(
            "skill.invalid_manifest", f"{key} must be an array of strings"
        )
    return tuple(value)


def _load_skill_manifest_content(content: bytes, source: str) -> SpecAuthoringSkill:
    """Validate one manifest and pin its exact bytes."""

    if content.startswith(b"---\n"):
        try:
            raw, prompt_template = parse_authoring_markdown(content, source=source)
            raw, prompt_template = project_typed_agent_skill(
                raw, prompt_template, source=source
            )
        except AuthoringMarkdownError as exc:
            raise SourceToSpecificationError(
                "skill.invalid_manifest", f"could not read skill manifest: {source}"
            ) from exc
        if "prompt_template" in raw:
            raise SourceToSpecificationError(
                "skill.duplicate_prompt_authority",
                "prompt_template must exist only in the SKILL.md body",
            )
        raw["prompt_template"] = prompt_template
    else:
        try:
            raw = json.loads(content)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise SourceToSpecificationError(
                "skill.invalid_manifest", f"could not read skill manifest: {source}"
            ) from exc
    if not isinstance(raw, dict):
        raise SourceToSpecificationError(
            "skill.invalid_manifest", "skill manifest must be a JSON object"
        )
    unknown = set(raw) - _MANIFEST_KEYS
    if unknown:
        raise SourceToSpecificationError(
            "skill.unknown_field",
            f"unknown skill manifest fields: {', '.join(sorted(unknown))}",
        )
    required = {
        "schema",
        "skill_id",
        "version",
        "title",
        "capabilities",
        "facets",
        "evidence_kinds",
    }
    missing = required - raw.keys()
    if missing:
        raise SourceToSpecificationError(
            "skill.missing_field",
            f"missing skill manifest fields: {', '.join(sorted(missing))}",
        )
    if raw["schema"] != SpecAuthoringSkill.schema:
        raise SourceToSpecificationError(
            "skill.unsupported_schema",
            f"unsupported skill schema: {raw['schema']!r}",
        )
    scalar_keys = ("skill_id", "version", "title")
    if not all(isinstance(raw[key], str) for key in scalar_keys):
        raise SourceToSpecificationError(
            "skill.invalid_manifest", "skill scalar fields must be strings"
        )
    extensions = raw.get("extensions", {})
    if not isinstance(extensions, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in extensions.items()
    ):
        raise SourceToSpecificationError(
            "skill.invalid_manifest", "extensions must be a string mapping"
        )
    models = raw.get("models", {})
    if not isinstance(models, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in models.items()
    ):
        raise SourceToSpecificationError(
            "skill.invalid_manifest", "models must be a string mapping"
        )
    for key in ("trust_classification", "prompt_template"):
        if key in raw and not isinstance(raw[key], str):
            raise SourceToSpecificationError(
                "skill.invalid_manifest", f"{key} must be a string"
            )
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    return SpecAuthoringSkill(
        skill_id=raw["skill_id"],
        version=raw["version"],
        content_digest=digest,
        title=raw["title"],
        capabilities=_string_tuple(raw, "capabilities"),
        facets=_string_tuple(raw, "facets"),
        evidence_kinds=_string_tuple(raw, "evidence_kinds"),
        model_capabilities=_string_tuple(raw, "model_capabilities"),
        dependencies=_string_tuple(raw, "dependencies"),
        after=_string_tuple(raw, "after"),
        limitations=_string_tuple(raw, "limitations"),
        trust_classification=raw.get("trust_classification", "builtin-reviewed"),
        prompt_template=raw.get("prompt_template", ""),
        extensions=extensions,
        models=models,
    )


def load_skill_manifest(path: str | Path) -> SpecAuthoringSkill:
    """Load one canonical Markdown or legacy JSON manifest and pin exact bytes."""

    manifest_path = Path(path)
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise SourceToSpecificationError(
            "skill.manifest_unavailable",
            f"skill manifest is not a regular file: {path}",
        )
    try:
        content = manifest_path.read_bytes()
    except OSError as exc:
        raise SourceToSpecificationError(
            "skill.invalid_manifest", f"could not read skill manifest: {path}"
        ) from exc
    return _load_skill_manifest_content(content, str(path))


def _add_skill(catalog: SkillCatalog, skill: SpecAuthoringSkill) -> None:
    catalog.add(skill)


def load_skill_catalog(root: str | Path) -> SkillCatalog:
    """Load the complete nested taxonomy through the shared catalog authority."""

    root_path = Path(root)
    if not root_path.is_dir():
        raise SourceToSpecificationError(
            "skill.catalog_unavailable", f"skill catalog is not a directory: {root}"
        )
    try:
        authority = AgentSkillCatalog.discover(
            root_path.parent,
            catalog_roots=(root_path,),
            validate_dependencies=True,
        )
    except AgentSkillCatalogError as exc:
        raise SourceToSpecificationError(exc.code, exc.message) from exc
    return load_skill_manifests(
        item.manifest_path
        for item in authority.skills
        if item.manifest_path is not None
    )


def load_skill_manifests(paths: Iterable[str | Path]) -> SkillCatalog:
    """Load one unambiguous catalog from an explicit manifest path set."""

    catalog = SkillCatalog()
    sources_by_id: dict[str, Path] = {}
    for configured in paths:
        path = Path(configured)
        skill = load_skill_manifest(path)
        previous = sources_by_id.get(skill.skill_id)
        if previous is not None:
            raise SourceToSpecificationError(
                "skill.catalog_id_ambiguous",
                f"skill ID {skill.skill_id!r} is declared by both "
                f"{previous} and {path}",
            )
        sources_by_id[skill.skill_id] = path
        _add_skill(catalog, skill)
    if not catalog:
        raise SourceToSpecificationError(
            "skill.catalog_empty", "skill catalog contains no manifests"
        )
    return catalog


def load_builtin_skill_catalog() -> SkillCatalog:
    """Load the immutable built-in manifests from the installed distribution."""

    root = importlib.resources.files(f"{__package__}.builtin_skills")
    catalog = SkillCatalog()
    for directory in sorted(root.iterdir(), key=lambda item: item.name):
        if not directory.is_dir():
            continue
        markdown = directory.joinpath("SKILL.md")
        legacy = directory.joinpath("skill.json")
        if markdown.is_file() and legacy.is_file():
            raise SourceToSpecificationError(
                "skill.authority_ambiguous",
                f"built-in skill contains both SKILL.md and skill.json: {directory}",
            )
        manifest = markdown if markdown.is_file() else legacy
        if not manifest.is_file():
            continue
        try:
            content = manifest.read_bytes()
        except OSError as exc:
            raise SourceToSpecificationError(
                "skill.invalid_manifest",
                f"could not read built-in skill manifest: {manifest}",
            ) from exc
        _add_skill(
            catalog,
            _load_skill_manifest_content(content, f"built-in:{directory.name}"),
        )
    if not catalog:
        raise SourceToSpecificationError(
            "skill.catalog_empty", "built-in skill catalog contains no manifests"
        )
    return catalog


def resolve_skill_set(
    skill_set: SpecAuthoringSkillSet,
    catalog: Mapping[str, SpecAuthoringSkill] | SkillCatalog,
    *,
    required_capabilities: tuple[str, ...] = (),
) -> tuple[SpecAuthoringSkill, ...]:
    """Resolve exact refs and enforce dependencies, ordering, and capabilities."""

    resolved: list[SpecAuthoringSkill] = []
    positions = {item.skill_id: index for index, item in enumerate(skill_set.skills)}
    for reference in skill_set.skills:
        if isinstance(catalog, SkillCatalog):
            skill = catalog.exact(reference)
        else:
            skill = catalog.get(reference.skill_id)
            if skill is None:
                skill = catalog.get(f"{reference.skill_id}@{reference.version}")
            if skill is None:
                raise SourceToSpecificationError(
                    "skill.not_found",
                    f"skill is not installed: {reference.skill_id}",
                )
        if skill.ref != reference:
            raise SourceToSpecificationError(
                "skill.identity_mismatch",
                f"skill identity does not match its pinned reference: {skill.skill_id}",
            )
        exact_dependencies = {item.skill_id: item for item in skill.dependency_refs}
        for dependency_id, dependency_ref in exact_dependencies.items():
            selected_ref = next(
                (item for item in skill_set.skills if item.skill_id == dependency_id),
                None,
            )
            if selected_ref is not None and selected_ref != dependency_ref:
                raise SourceToSpecificationError(
                    "skill.dependency_identity_mismatch",
                    f"{skill.skill_id} requires exact dependency {dependency_ref}",
                )
        required_dependency_ids = set(skill.dependencies) | set(exact_dependencies)
        missing = required_dependency_ids - positions.keys()
        if missing:
            raise SourceToSpecificationError(
                "skill.dependency_missing",
                f"{skill.skill_id} requires: {', '.join(sorted(missing))}",
            )
        must_precede = required_dependency_ids | set(skill.after)
        late = {
            item
            for item in must_precede
            if item in positions and positions[item] >= positions[skill.skill_id]
        }
        if late:
            raise SourceToSpecificationError(
                "skill.order_invalid",
                f"{skill.skill_id} must run after: {', '.join(sorted(late))}",
            )
        resolved.append(skill)
    capabilities = {
        capability for skill in resolved for capability in skill.capabilities
    }
    missing_capabilities = set(required_capabilities) - capabilities
    if missing_capabilities:
        raise SourceToSpecificationError(
            "skill.capability_missing",
            "skill set does not provide: " + ", ".join(sorted(missing_capabilities)),
        )
    return tuple(resolved)


def builtin_skill_set(
    catalog: Mapping[str, SpecAuthoringSkill],
    *,
    languages: Iterable[str] = (),
) -> SpecAuthoringSkillSet:
    """Return common skills plus exact translators for detected languages."""

    detected = frozenset(languages)
    language_skills = tuple(
        skill_id
        for _label, aliases, skill_id in _BUILTIN_LANGUAGE_SKILLS
        if detected & aliases
    )
    selected_ids = (*_BUILTIN_ORDER, *language_skills)
    missing = set(selected_ids) - catalog.keys()
    if missing:
        raise SourceToSpecificationError(
            "skill.builtin_missing",
            f"built-in skill catalog is missing: {', '.join(sorted(missing))}",
        )
    language_labels = tuple(
        label
        for label, _aliases, skill_id in _BUILTIN_LANGUAGE_SKILLS
        if skill_id in language_skills
    )
    return SpecAuthoringSkillSet(
        skill_set_id=(
            "builtin-complete"
            if not language_labels
            else "builtin-complete+" + "+".join(language_labels)
        ),
        version="1.0.0",
        skills=tuple(catalog[item].ref for item in selected_ids),
    )


__all__ = [
    "SkillCatalog",
    "builtin_skill_set",
    "load_builtin_skill_catalog",
    "load_skill_catalog",
    "load_skill_manifests",
    "load_skill_manifest",
    "resolve_skill_set",
]
