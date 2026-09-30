"""Bounded OpenSpec artifact snapshot and requirement/scenario parser.

This adapter deliberately parses the portable subset needed by the neutral kernel. It
does not claim to replace the OpenSpec provider's own strict CLI validation, which is a
separate repository/release gate.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from literate_ai.contracts import (
    ContentIdentity,
    HashAlgorithm,
    SpecificationArtifact,
    SpecificationRequirement,
    SpecificationScenario,
    SpecificationSet,
)

_REQUIREMENT = re.compile(r"^### Requirement:\s+(.+?)\s*$")
_SCENARIO = re.compile(r"^#### Scenario:\s+(.+?)\s*$")
_GIVEN = re.compile(r"^- \*\*GIVEN\*\*\s+(.+?)\s*$")
_WHEN = re.compile(r"^- \*\*WHEN\*\*\s+(.+?)\s*$")
_THEN = re.compile(r"^- \*\*(?:THEN|AND)\*\*\s+(.+?)\s*$")
DEFAULT_MAXIMUM_ARTIFACTS = 256
DEFAULT_MAXIMUM_ARTIFACT_BYTES = 2 * 1024 * 1024
DEFAULT_MAXIMUM_TOTAL_BYTES = 8 * 1024 * 1024
DEFAULT_MAXIMUM_PATH_BYTES = 512


class SpecificationProviderError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class OpenSpecError(SpecificationProviderError):
    pass


def _safe_relative(value: str, *, maximum_path_bytes: int) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or "." in path.parts
        or ".." in path.parts
        or str(path) != value
        or len(value.encode("utf-8")) > maximum_path_bytes
    ):
        raise OpenSpecError("openspec.path_invalid", f"Unsafe artifact path: {value!r}")
    return path


def _content_identity(content: bytes) -> ContentIdentity:
    return ContentIdentity(HashAlgorithm.SHA256, hashlib.sha256(content).hexdigest())


def _slug(value: str) -> str:
    result = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if not result:
        raise OpenSpecError(
            "openspec.heading_invalid", "Heading has no portable identity"
        )
    return result


@dataclass(frozen=True, slots=True)
class LoadedSpecificationArtifacts:
    artifacts: tuple[SpecificationArtifact, ...]
    requirements: tuple[SpecificationRequirement, ...]
    contents: tuple[tuple[str, bytes], ...]


@dataclass(frozen=True, slots=True)
class LoadedSpecification:
    specification_set: SpecificationSet
    contents: tuple[tuple[str, bytes], ...]
    context_document: tuple[str, bytes] | None = None
    error_type: type[SpecificationProviderError] = OpenSpecError

    def require_unchanged(self, root: Path) -> None:
        resolved = root.resolve(strict=True)
        for path, expected in self.contents:
            target = resolved.joinpath(*PurePosixPath(path).parts)
            if (
                target.is_symlink()
                or not target.is_file()
                or not target.resolve(strict=True).is_relative_to(resolved)
            ):
                raise self.error_type(
                    f"{self.specification_set.provider_kind}.artifact_missing",
                    f"Artifact changed or disappeared: {path}",
                )
            try:
                with target.open("rb") as stream:
                    current = stream.read(len(expected) + 1)
            except OSError as exc:
                raise self.error_type(
                    f"{self.specification_set.provider_kind}.artifact_unavailable",
                    f"Artifact could not be re-read: {path}",
                ) from exc
            if current != expected:
                raise self.error_type(
                    f"{self.specification_set.provider_kind}.artifact_changed",
                    f"Artifact changed during operation: {path}",
                )


class OpenSpecProvider:
    provider_id = "specification-provider:openspec@1"

    def __init__(
        self,
        *,
        maximum_artifacts: int = DEFAULT_MAXIMUM_ARTIFACTS,
        maximum_artifact_bytes: int = DEFAULT_MAXIMUM_ARTIFACT_BYTES,
        maximum_total_bytes: int = DEFAULT_MAXIMUM_TOTAL_BYTES,
        maximum_path_bytes: int = DEFAULT_MAXIMUM_PATH_BYTES,
    ) -> None:
        limits = (
            maximum_artifacts,
            maximum_artifact_bytes,
            maximum_total_bytes,
            maximum_path_bytes,
        )
        if any(item < 1 for item in limits):
            raise ValueError("OpenSpec snapshot limits must be positive")
        self.maximum_artifacts = maximum_artifacts
        self.maximum_artifact_bytes = maximum_artifact_bytes
        self.maximum_total_bytes = maximum_total_bytes
        self.maximum_path_bytes = maximum_path_bytes

    def load(
        self,
        root: Path,
        paths: Iterable[str],
        *,
        baseline_id: str | None = None,
        active_change_id: str | None = None,
    ) -> LoadedSpecification:
        snapshot = self.snapshot(root, paths)
        if not snapshot.requirements:
            raise OpenSpecError(
                "openspec.requirements_empty",
                "No Requirement/Scenario contracts were found",
            )
        return LoadedSpecification(
            specification_set=SpecificationSet(
                provider_kind="openspec",
                provider_version="1",
                artifacts=snapshot.artifacts,
                requirements=snapshot.requirements,
                baseline_id=baseline_id,
                active_change_id=active_change_id,
            ),
            contents=snapshot.contents,
        )

    def snapshot(
        self,
        root: Path,
        paths: Iterable[str],
    ) -> LoadedSpecificationArtifacts:
        resolved = root.resolve(strict=True)
        if not resolved.is_dir():
            raise OpenSpecError(
                "openspec.root_invalid", "OpenSpec root is not a directory"
            )
        declared_paths = tuple(paths)
        if len(declared_paths) > self.maximum_artifacts:
            raise OpenSpecError(
                "openspec.artifact_limit", "Too many OpenSpec artifacts were declared"
            )
        contents: list[tuple[str, bytes]] = []
        artifacts: list[SpecificationArtifact] = []
        requirements: list[SpecificationRequirement] = []
        seen: set[str] = set()
        total_bytes = 0
        for declared in declared_paths:
            relative = _safe_relative(
                declared, maximum_path_bytes=self.maximum_path_bytes
            )
            normalized = relative.as_posix()
            if normalized in seen:
                raise OpenSpecError(
                    "openspec.artifact_duplicate",
                    f"Artifact declared twice: {normalized}",
                )
            seen.add(normalized)
            target = resolved.joinpath(*relative.parts)
            if target.is_symlink() or not target.is_file():
                raise OpenSpecError(
                    "openspec.artifact_missing", f"Artifact is missing: {normalized}"
                )
            if not target.resolve(strict=True).is_relative_to(resolved):
                raise OpenSpecError("openspec.path_escape", normalized)
            remaining_total = self.maximum_total_bytes - total_bytes
            read_limit = min(self.maximum_artifact_bytes, remaining_total)
            try:
                with target.open("rb") as stream:
                    content = stream.read(read_limit + 1)
            except OSError as exc:
                raise OpenSpecError(
                    "openspec.artifact_unavailable",
                    f"OpenSpec artifact could not be read: {normalized}",
                ) from exc
            if len(content) > self.maximum_artifact_bytes:
                raise OpenSpecError(
                    "openspec.artifact_size_limit",
                    f"OpenSpec artifact exceeds its size limit: {normalized}",
                )
            if len(content) > remaining_total:
                raise OpenSpecError(
                    "openspec.total_size_limit",
                    "OpenSpec artifacts exceed the total size limit",
                )
            total_bytes += len(content)
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise OpenSpecError(
                    "openspec.artifact_not_utf8", f"Artifact is not UTF-8: {normalized}"
                ) from exc
            contents.append((normalized, content))
            artifacts.append(
                SpecificationArtifact(
                    uri=normalized,
                    identity=_content_identity(content),
                )
            )
            requirements.extend(parse_open_spec_requirements(normalized, text))
        if not artifacts:
            raise OpenSpecError(
                "openspec.artifacts_empty", "No artifacts were declared"
            )
        requirement_ids = [item.requirement_id for item in requirements]
        if len(requirement_ids) != len(set(requirement_ids)):
            raise OpenSpecError(
                "openspec.requirement_duplicate", "Requirement IDs are not unique"
            )
        return LoadedSpecificationArtifacts(
            artifacts=tuple(artifacts),
            requirements=tuple(requirements),
            contents=tuple(contents),
        )


def parse_open_spec_requirements(
    path: str, text: str
) -> tuple[SpecificationRequirement, ...]:
    lines = text.splitlines()
    result: list[SpecificationRequirement] = []
    requirement_title: str | None = None
    requirement_statement: list[str] = []
    scenarios: list[SpecificationScenario] = []
    scenario_title: str | None = None
    given: list[str] = []
    when: list[str] = []
    then: list[str] = []

    def finish_scenario() -> None:
        nonlocal scenario_title, given, when, then
        if scenario_title is None:
            return
        if not when or not then:
            raise OpenSpecError(
                "openspec.scenario_incomplete",
                f"Scenario {scenario_title!r} in {path} requires WHEN and THEN",
            )
        scenarios.append(
            SpecificationScenario(
                scenario_id=(
                    f"{_slug(requirement_title or 'requirement')}."
                    f"{_slug(scenario_title)}"
                ),
                title=scenario_title,
                given=tuple(given),
                when=tuple(when),
                then=tuple(then),
            )
        )
        scenario_title = None
        given, when, then = [], [], []

    def finish_requirement() -> None:
        nonlocal requirement_title, requirement_statement, scenarios
        if requirement_title is None:
            return
        finish_scenario()
        statement = " ".join(
            line.strip() for line in requirement_statement if line.strip()
        )
        if not statement:
            raise OpenSpecError(
                "openspec.requirement_statement_missing",
                f"Requirement {requirement_title!r} in {path} has no statement",
            )
        result.append(
            SpecificationRequirement(
                requirement_id=f"{_slug(path)}.{_slug(requirement_title)}",
                title=requirement_title,
                statement=statement,
                scenarios=tuple(scenarios),
            )
        )
        requirement_title = None
        requirement_statement = []
        scenarios = []

    for line in lines:
        if match := _REQUIREMENT.match(line):
            finish_requirement()
            requirement_title = match.group(1)
            continue
        if requirement_title is None:
            continue
        if match := _SCENARIO.match(line):
            finish_scenario()
            scenario_title = match.group(1)
            continue
        if scenario_title is None:
            if line and not line.startswith("#"):
                requirement_statement.append(line)
            continue
        if match := _GIVEN.match(line):
            given.append(match.group(1))
        elif match := _WHEN.match(line):
            when.append(match.group(1))
        elif match := _THEN.match(line):
            then.append(match.group(1))
    finish_requirement()
    return tuple(result)


LoadedOpenSpec = LoadedSpecification


__all__ = [
    "LoadedOpenSpec",
    "LoadedSpecification",
    "LoadedSpecificationArtifacts",
    "OpenSpecError",
    "OpenSpecProvider",
    "SpecificationProviderError",
    "parse_open_spec_requirements",
]
