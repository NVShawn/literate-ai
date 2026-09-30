"""Executable directory-taxonomy authority for agent and generation skills."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from literate_ai.contracts.yaml_subset import YamlSubsetError, load_yaml_subset

_REFERENCE = re.compile(r"`([^`\n]*SKILL\.md)`")


class AgentSkillCatalogError(ValueError):
    """The skill taxonomy is ambiguous, unsafe, or internally inconsistent."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class AgentSkillDependency:
    skill_id: str
    version: str | None = None
    identity: str | None = None


@dataclass(frozen=True, slots=True)
class AgentSkill:
    """One sentinel, the closure it owns, and its inherited context."""

    name: str
    version: str | None
    identity: str
    logical_directory: PurePosixPath
    logical_manifest: PurePosixPath
    manifest_path: Path | None
    dependencies: tuple[AgentSkillDependency, ...]
    ancestors: tuple[str, ...]
    owned_paths: tuple[PurePosixPath, ...]
    content: bytes


class AgentSkillCatalog:
    """Discover and validate filesystem sentinel ownership exactly once."""

    def __init__(
        self,
        *,
        skills: tuple[AgentSkill, ...],
        documents: Mapping[PurePosixPath, bytes],
        physical_paths: Mapping[PurePosixPath, Path],
    ) -> None:
        self.skills = skills
        self._documents = dict(documents)
        self._physical_paths = dict(physical_paths)
        self._by_name = {item.name: item for item in skills}
        self._by_manifest = {item.logical_manifest: item for item in skills}
        self._owners = {path: item for item in skills for path in item.owned_paths}

    @classmethod
    def discover(
        cls,
        project_root: Path,
        *,
        catalog_roots: Iterable[Path],
        root_manifests: Iterable[Path] = (),
        validate_dependencies: bool = True,
        validate_references: bool = False,
    ) -> AgentSkillCatalog:
        """Read bounded real files beneath declared roots without following links."""

        documents, physical = _discover_documents(
            project_root,
            catalog_roots=catalog_roots,
            root_manifests=root_manifests,
        )
        return cls.from_documents(
            documents,
            physical_paths=physical,
            validate_dependencies=validate_dependencies,
            validate_references=validate_references,
        )

    @classmethod
    def discover_manifest_paths(
        cls,
        project_root: Path,
        *,
        catalog_roots: Iterable[Path],
        root_manifests: Iterable[Path] = (),
    ) -> tuple[Path, ...]:
        """Inventory safe sentinel files before domain-specific metadata validation."""

        documents, physical = _discover_documents(
            project_root,
            catalog_roots=catalog_roots,
            root_manifests=root_manifests,
        )
        authorities = _manifest_authorities(documents)
        return tuple(physical[path] for path in sorted(authorities.values(), key=str))

    @classmethod
    def from_documents(
        cls,
        documents: Mapping[str | PurePosixPath, bytes],
        *,
        physical_paths: Mapping[PurePosixPath, Path] | None = None,
        validate_dependencies: bool = True,
        validate_references: bool = False,
    ) -> AgentSkillCatalog:
        """Build the same authority from an installed-resource file map."""

        normalized: dict[PurePosixPath, bytes] = {}
        for configured, content in documents.items():
            path = _logical_path(configured)
            if not isinstance(content, bytes):
                raise AgentSkillCatalogError(
                    "agent_skill.content_invalid",
                    "skill resource content must be bytes",
                )
            existing = normalized.get(path)
            if existing is not None and existing != content:
                raise AgentSkillCatalogError(
                    "agent_skill.path_conflict",
                    f"skill resource path has conflicting content: {path}",
                )
            normalized[path] = content

        authorities = _manifest_authorities(normalized)

        provisional: list[
            tuple[
                str,
                str | None,
                str,
                PurePosixPath,
                tuple[AgentSkillDependency, ...],
                bytes,
            ]
        ] = []
        names: dict[str, PurePosixPath] = {}
        for _directory, manifest in sorted(
            authorities.items(), key=lambda item: str(item[0])
        ):
            content = normalized[manifest]
            name, version, dependencies = _manifest_metadata(content, manifest)
            previous = names.get(name)
            if previous is not None:
                raise AgentSkillCatalogError(
                    "agent_skill.name_ambiguous",
                    f"skill name {name!r} is declared by both "
                    f"{previous} and {manifest}",
                )
            names[name] = manifest
            provisional.append(
                (
                    name,
                    version,
                    "sha256:" + hashlib.sha256(content).hexdigest(),
                    manifest,
                    dependencies,
                    content,
                )
            )

        directory_to_name = {}
        for item in provisional:
            name, _version, _identity, manifest, _dependencies, _content = item
            directory_to_name[manifest.parent] = name
        authority_directories = frozenset(directory_to_name)
        owned_by_directory: dict[PurePosixPath, list[PurePosixPath]] = {
            directory: [] for directory in authority_directories
        }
        for path in normalized:
            owner = _owner_directory(path, authority_directories)
            if owner is not None:
                owned_by_directory[owner].append(path)
        records: list[AgentSkill] = []
        for name, version, identity, manifest, dependencies, content in provisional:
            directory = manifest.parent
            ancestor_directories = tuple(
                candidate
                for candidate in reversed(directory.parents)
                if candidate in authority_directories
            )
            owned = tuple(sorted(owned_by_directory[directory], key=str))
            records.append(
                AgentSkill(
                    name=name,
                    version=version,
                    identity=identity,
                    logical_directory=directory,
                    logical_manifest=manifest,
                    manifest_path=(physical_paths or {}).get(manifest),
                    dependencies=dependencies,
                    ancestors=tuple(
                        directory_to_name[item] for item in ancestor_directories
                    ),
                    owned_paths=owned,
                    content=content,
                )
            )
        catalog = cls(
            skills=tuple(sorted(records, key=lambda item: str(item.logical_manifest))),
            documents=normalized,
            physical_paths=physical_paths or {},
        )
        if validate_dependencies:
            catalog.validate_dependencies()
        if validate_references:
            catalog.validate_references()
        return catalog

    def by_name(self, name: str) -> AgentSkill:
        try:
            return self._by_name[name]
        except KeyError as exc:
            raise AgentSkillCatalogError(
                "agent_skill.not_found", f"skill is not present: {name}"
            ) from exc

    def owner_for(self, path: str | PurePosixPath) -> AgentSkill | None:
        logical = _logical_path(path)
        direct = self._owners.get(logical)
        if direct is not None:
            return direct
        candidates = tuple(
            item
            for item in self.skills
            if _is_relative(logical, item.logical_directory)
        )
        return max(
            candidates, key=lambda item: len(item.logical_directory.parts), default=None
        )

    def descendants(self, skill: AgentSkill | str) -> tuple[AgentSkill, ...]:
        selected = self.by_name(skill) if isinstance(skill, str) else skill
        return tuple(item for item in self.skills if selected.name in item.ancestors)

    def ancestor_chain(self, skill: AgentSkill | str) -> tuple[AgentSkill, ...]:
        selected = self.by_name(skill) if isinstance(skill, str) else skill
        return tuple(self.by_name(name) for name in selected.ancestors)

    def validate_dependencies(self) -> None:
        for skill in self.skills:
            for dependency in skill.dependencies:
                target = self._by_name.get(dependency.skill_id)
                if target is None:
                    raise AgentSkillCatalogError(
                        "agent_skill.dependency_missing",
                        f"{skill.name} requires missing skill {dependency.skill_id}",
                    )
                if (
                    dependency.version is not None
                    and target.version != dependency.version
                ):
                    raise AgentSkillCatalogError(
                        "agent_skill.dependency_version_stale",
                        f"{skill.name} pins another version of {dependency.skill_id}",
                    )
                if (
                    dependency.identity is not None
                    and target.identity != dependency.identity
                ):
                    raise AgentSkillCatalogError(
                        "agent_skill.dependency_identity_stale",
                        f"{skill.name} pins stale content for {dependency.skill_id}",
                    )

    def unresolved_references(self) -> tuple[tuple[str, str], ...]:
        manifests = set(self._by_manifest)
        unresolved: list[tuple[str, str]] = []
        for skill in self.skills:
            try:
                text = skill.content.decode("utf-8")
            except UnicodeDecodeError:
                continue
            for token in _REFERENCE.findall(text):
                if "://" in token:
                    continue
                target = _resolve_reference(skill.logical_directory, token)
                if target not in manifests:
                    unresolved.append((skill.name, token))
        return tuple(sorted(set(unresolved)))

    def validate_references(self) -> None:
        unresolved = self.unresolved_references()
        if unresolved:
            owner, reference = unresolved[0]
            raise AgentSkillCatalogError(
                "agent_skill.reference_missing",
                f"{owner} references unavailable skill path {reference}",
            )

    def impacted(self, paths: Iterable[str | PurePosixPath]) -> tuple[AgentSkill, ...]:
        selected: set[str] = set()
        for configured in paths:
            logical = _logical_path(configured)
            owner = self.owner_for(logical)
            if owner is None:
                continue
            selected.add(owner.name)
            if logical == owner.logical_manifest:
                selected.update(item.name for item in self.descendants(owner))

        reverse: dict[str, set[str]] = {}
        for skill in self.skills:
            for dependency in skill.dependencies:
                reverse.setdefault(dependency.skill_id, set()).add(skill.name)
        pending = deque(selected)
        while pending:
            current = pending.popleft()
            for dependent in reverse.get(current, ()):
                if dependent not in selected:
                    selected.add(dependent)
                    pending.append(dependent)
        return tuple(item for item in self.skills if item.name in selected)

    def materialize(self, skill: AgentSkill | str, destination: Path) -> Path:
        """Copy owned closure plus explicit ancestor manifests for evaluation."""

        selected = self.by_name(skill) if isinstance(skill, str) else skill
        target = Path(destination) / selected.name
        if target.exists() or target.is_symlink():
            raise AgentSkillCatalogError(
                "agent_skill.projection_exists", "skill projection already exists"
            )
        target.mkdir(parents=True)
        for logical in selected.owned_paths:
            relative = logical.relative_to(selected.logical_directory)
            output = target.joinpath(*relative.parts)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(self._documents[logical])
        ancestors = self.ancestor_chain(selected)
        if ancestors:
            context = target / ".literate-ancestors"
            for index, ancestor in enumerate(ancestors):
                output = (
                    context
                    / f"{index:04d}-{ancestor.name}"
                    / ancestor.logical_manifest.name
                )
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(self._documents[ancestor.logical_manifest])
        return target


def _discover_documents(
    project_root: Path,
    *,
    catalog_roots: Iterable[Path],
    root_manifests: Iterable[Path],
) -> tuple[dict[PurePosixPath, bytes], dict[PurePosixPath, Path]]:
    root = _real_directory(project_root, label="project root")
    documents: dict[PurePosixPath, bytes] = {}
    physical: dict[PurePosixPath, Path] = {}
    for configured in catalog_roots:
        catalog_root = _real_directory(configured, label="skill catalog root")
        if not catalog_root.is_relative_to(root):
            raise AgentSkillCatalogError(
                "agent_skill.catalog_escape",
                "skill catalog root escapes the project",
            )
        for current, directory_names, file_names in os.walk(
            catalog_root, followlinks=False
        ):
            current_path = Path(current)
            for directory_name in tuple(directory_names):
                candidate = current_path / directory_name
                if candidate.is_symlink():
                    raise AgentSkillCatalogError(
                        "agent_skill.symlink",
                        "skill catalogs cannot contain symbolic links",
                    )
            for file_name in file_names:
                candidate = current_path / file_name
                if candidate.is_symlink() or not candidate.is_file():
                    raise AgentSkillCatalogError(
                        "agent_skill.resource_invalid",
                        "skill resources must be regular non-symlink files",
                    )
                _add_document(root, candidate, documents, physical)
    for configured in root_manifests:
        manifest = Path(configured)
        if manifest.is_symlink() or not manifest.is_file():
            raise AgentSkillCatalogError(
                "agent_skill.manifest_invalid",
                "root skill manifest must be a regular non-symlink file",
            )
        resolved = manifest.resolve(strict=True)
        if not resolved.is_relative_to(root):
            raise AgentSkillCatalogError(
                "agent_skill.manifest_escape",
                "root skill manifest escapes the project",
            )
        _add_document(root, resolved, documents, physical)
    return documents, physical


def _manifest_authorities(
    documents: Mapping[PurePosixPath, bytes],
) -> dict[PurePosixPath, PurePosixPath]:
    authorities: dict[PurePosixPath, PurePosixPath] = {}
    for path in documents:
        if path.name not in {"SKILL.md", "skill.json"}:
            continue
        previous = authorities.get(path.parent)
        if previous is not None:
            raise AgentSkillCatalogError(
                "agent_skill.authority_ambiguous",
                f"skill directory contains both SKILL.md and skill.json: {path.parent}",
            )
        authorities[path.parent] = path
    if not authorities:
        raise AgentSkillCatalogError(
            "agent_skill.catalog_empty", "skill catalog contains no manifests"
        )
    return authorities


def _real_directory(path: Path, *, label: str) -> Path:
    configured = Path(path)
    if configured.is_symlink():
        raise AgentSkillCatalogError(
            "agent_skill.symlink", f"{label} cannot be a symbolic link"
        )
    try:
        resolved = configured.resolve(strict=True)
    except OSError as exc:
        raise AgentSkillCatalogError(
            "agent_skill.catalog_unavailable", f"{label} is unavailable"
        ) from exc
    if not resolved.is_dir():
        raise AgentSkillCatalogError(
            "agent_skill.catalog_unavailable", f"{label} is not a directory"
        )
    return resolved


def _add_document(
    root: Path,
    path: Path,
    documents: dict[PurePosixPath, bytes],
    physical: dict[PurePosixPath, Path],
) -> None:
    logical = PurePosixPath(path.relative_to(root).as_posix())
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise AgentSkillCatalogError(
            "agent_skill.resource_unavailable", "skill resource cannot be read"
        ) from exc
    existing = documents.get(logical)
    if existing is not None and existing != content:
        raise AgentSkillCatalogError(
            "agent_skill.path_conflict", f"skill resource conflicts at {logical}"
        )
    documents[logical] = content
    physical[logical] = path


def _logical_path(value: str | PurePosixPath) -> PurePosixPath:
    raw = str(value).replace("\\", "/")
    path = PurePosixPath(raw)
    if (
        path.is_absolute()
        or not raw
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        if raw == ".":
            return PurePosixPath(".")
        raise AgentSkillCatalogError(
            "agent_skill.path_invalid", f"skill path is not canonical: {raw!r}"
        )
    return path


def _manifest_metadata(
    content: bytes, source: PurePosixPath
) -> tuple[str, str | None, tuple[AgentSkillDependency, ...]]:
    if source.name == "skill.json":
        try:
            frontmatter = json.loads(content.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise AgentSkillCatalogError(
                "agent_skill.manifest_invalid", f"invalid legacy manifest: {source}"
            ) from exc
    else:
        try:
            text = content.decode("utf-8")
            lines = text.splitlines()
            if not lines or lines[0] != "---":
                raise AgentSkillCatalogError(
                    "agent_skill.frontmatter_missing",
                    f"skill manifest has no frontmatter: {source}",
                )
            close = lines.index("---", 1)
            frontmatter = load_yaml_subset("\n".join(lines[1:close]))
        except (UnicodeError, ValueError, YamlSubsetError) as exc:
            raise AgentSkillCatalogError(
                "agent_skill.manifest_invalid", f"invalid skill manifest: {source}"
            ) from exc
    if not isinstance(frontmatter, dict):
        raise AgentSkillCatalogError(
            "agent_skill.manifest_invalid", f"manifest is not an object: {source}"
        )
    name = frontmatter.get("name", frontmatter.get("skill_id"))
    typed_name = frontmatter.get("skill_id")
    if (
        not isinstance(name, str)
        or not name
        or (typed_name is not None and typed_name != name)
    ):
        raise AgentSkillCatalogError(
            "agent_skill.name_invalid",
            f"manifest has no stable matching name: {source}",
        )
    version = frontmatter.get("version")
    if version is not None and (not isinstance(version, str) or not version):
        raise AgentSkillCatalogError(
            "agent_skill.version_invalid", f"manifest has an invalid version: {source}"
        )
    dependencies = _dependencies(frontmatter.get("dependencies", []), source)
    return name, version, dependencies


def _dependencies(raw: Any, source: PurePosixPath) -> tuple[AgentSkillDependency, ...]:
    if not isinstance(raw, list):
        raise AgentSkillCatalogError(
            "agent_skill.dependencies_invalid",
            f"dependencies must be an array: {source}",
        )
    parsed: list[AgentSkillDependency] = []
    for dependency in raw:
        if isinstance(dependency, str) and dependency:
            parsed.append(AgentSkillDependency(dependency))
            continue
        if not isinstance(dependency, dict):
            raise AgentSkillCatalogError(
                "agent_skill.dependencies_invalid", f"invalid dependency in {source}"
            )
        skill_id = dependency.get("skill_id")
        version = dependency.get("version")
        identity = dependency.get("identity")
        digest = identity.get("digest") if isinstance(identity, dict) else None
        algorithm = identity.get("algorithm") if isinstance(identity, dict) else None
        if (
            not isinstance(skill_id, str)
            or not skill_id
            or not isinstance(version, str)
            or not version
            or algorithm != "sha256"
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise AgentSkillCatalogError(
                "agent_skill.dependencies_invalid",
                f"invalid typed dependency in {source}",
            )
        parsed.append(AgentSkillDependency(skill_id, version, f"sha256:{digest}"))
    return tuple(parsed)


def _owner_directory(
    path: PurePosixPath, directories: frozenset[PurePosixPath]
) -> PurePosixPath | None:
    return next((parent for parent in path.parents if parent in directories), None)


def _is_relative(path: PurePosixPath, parent: PurePosixPath) -> bool:
    if parent == PurePosixPath("."):
        return True
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _resolve_reference(directory: PurePosixPath, token: str) -> PurePosixPath:
    raw = token.replace("\\", "/")
    parts = [] if raw.startswith("skills/") else list(directory.parts)
    for part in PurePosixPath(raw).parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return PurePosixPath(*parts)


__all__ = [
    "AgentSkill",
    "AgentSkillCatalog",
    "AgentSkillCatalogError",
    "AgentSkillDependency",
]
