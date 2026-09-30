"""MCP resource projection for skills, documentation, and static assets."""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlsplit

from literate_ai.projects import (
    discover_project,
    documentation_files,
    project_skill_catalog,
)

_MAXIMUM_RESOURCE_BYTES = 2 * 1024 * 1024


class McpResourceError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class McpResource:
    uri: str
    name: str
    description: str
    mime_type: str
    content: bytes

    def metadata(self) -> dict[str, str]:
        return {
            "uri": self.uri,
            "name": self.name,
            "description": self.description,
            "mimeType": self.mime_type,
        }


def project_mcp_resources(selected: Path) -> tuple[McpResource, ...]:
    project = discover_project(selected)
    if project is None:
        raise McpResourceError(
            "mcp_resource.project_not_found", "no Literate AI project was found"
        )
    resources: list[McpResource] = []
    if project.roots("skill"):
        catalog = project_skill_catalog(project)
        for skill in catalog.skills:
            context = b"\n\n".join(
                ancestor.content for ancestor in catalog.ancestor_chain(skill)
            )
            content = context + (b"\n\n" if context else b"") + skill.content
            resources.append(
                McpResource(
                    f"litai://skill/{quote(skill.name, safe='')}",
                    skill.name,
                    "Skill authority with inherited ancestor context",
                    "text/markdown",
                    content,
                )
            )
            for owned in skill.owned_paths:
                if owned == skill.logical_manifest:
                    continue
                relative = owned.relative_to(skill.logical_directory).as_posix()
                content = Path(project.root, *owned.parts).read_bytes()
                resources.append(
                    McpResource(
                        "litai://skill-resource/"
                        f"{quote(skill.name, safe='')}/{quote(relative, safe='/')}",
                        f"{skill.name}/{relative}",
                        "Static resource owned by the skill sentinel",
                        mimetypes.guess_type(relative)[0] or "application/octet-stream",
                        content,
                    )
                )
    for path in documentation_files(project):
        relative = path.relative_to(project.root).as_posix()
        content = path.read_bytes()
        resources.append(
            McpResource(
                f"litai://documentation/{quote(relative, safe='/')}",
                relative,
                "Project documentation authority",
                mimetypes.guess_type(relative)[0] or "text/plain",
                content,
            )
        )
    resources.extend(_packaged_static_resources())
    return tuple(sorted(resources, key=lambda item: item.uri))


def read_project_mcp_resource(selected: Path, uri: str) -> McpResource:
    parsed = urlsplit(uri)
    if parsed.scheme != "litai" or parsed.query or parsed.fragment:
        raise McpResourceError("mcp_resource.uri_invalid", "resource URI is invalid")
    canonical = f"litai://{parsed.netloc}{parsed.path}"
    matches = tuple(
        item for item in project_mcp_resources(selected) if item.uri == canonical
    )
    if len(matches) != 1:
        raise McpResourceError("mcp_resource.not_found", "resource is not available")
    resource = matches[0]
    if len(resource.content) > _MAXIMUM_RESOURCE_BYTES:
        raise McpResourceError("mcp_resource.size_limit", "resource exceeds read limit")
    return resource


def _packaged_static_resources() -> tuple[McpResource, ...]:
    root = files("literate_ai.project_template")
    selected = (
        "SKILL.md",
        "docs/user/test-matrix.md",
        "literate.test.example.json",
        "literate.workers.example.json",
    )
    resources = []
    for relative in selected:
        content = root.joinpath(*PurePosixPath(relative).parts).read_bytes()
        resources.append(
            McpResource(
                f"litai://static/project-template/{quote(relative, safe='/')}",
                f"project-template/{unquote(relative)}",
                "Packaged initialized-project static resource",
                mimetypes.guess_type(relative)[0] or "application/octet-stream",
                content,
            )
        )
    return tuple(resources)


__all__ = [
    "McpResource",
    "McpResourceError",
    "project_mcp_resources",
    "read_project_mcp_resource",
]
