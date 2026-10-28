"""Validate declared work prerequisites and implicit release-phase ordering.

Edges point from an obligation to what must finish first. This module deliberately
does not infer dependencies from arbitrary narrative or checkbox prose.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

_ID = r"[A-Z][A-Z0-9-]{2,63}"
_HEADING = re.compile(rf"^### \[[ x~]\] (?P<id>{_ID})(?=\s|$).*$", re.M)
_FIELD = re.compile(
    r"^- \*\*(Depends on|Completion prerequisites|Release target):\*\*[ \t]*"
    r"(.*(?:\n[ \t]+\S.*)*)$",
    re.M,
)
_VERSION = r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?"
_PHASES = ("ready", "prepared", "checked", "published", "verified", "synchronized")
_PREDECESSOR = dict(zip(_PHASES[1:], _PHASES[:-1], strict=True))
# Standalone `release qualify` accepts clean HEAD without prepare/readiness.
_PHASES += ("qualified",)
_RELEASE = re.compile(rf"release:({_VERSION}):({'|'.join(_PHASES)})\Z")


class RoadmapDependencyError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _visible(content: str) -> str:
    lines = []
    fence = None
    in_comment = False
    for line in content.splitlines():
        if fence is not None:
            if re.fullmatch(
                rf" {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*", line
            ):
                fence = None
            lines.append("")
            continue
        # A literal comment opener in a fenced example cannot hide later work.
        visible = []
        position = 0
        while position < len(line):
            if in_comment:
                end = line.find("-->", position)
                if end < 0:
                    break
                position = end + 3
                in_comment = False
            else:
                start = line.find("<!--", position)
                if start < 0:
                    visible.append(line[position:])
                    break
                visible.append(line[position:start])
                position = start + 4
                in_comment = True
        cleaned = "".join(visible)
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", cleaned)
        if marker and (marker[1][0] != "`" or "`" not in marker[2]):
            fence = marker[1]
            lines.append("")
        else:
            lines.append(cleaned)
    return "\n".join(lines)


def validate_roadmap_dependencies(
    content: str, manifest: Mapping[str, object] | None = None
) -> None:
    """Reject malformed typed references and cycles, irrespective of completion.

    Legacy Depends on fields contribute references to *existing* work IDs only;
    their free text is not interpreted. Completion prerequisites is strict: comma
    separated existing IDs or release:VERSION:PHASE, or exactly 'none'.
    Manifest membership and exact Release target fields both create ready->goal
    edges, so omitting one declaration cannot conceal a release-gate cycle.
    """
    visible = _visible(content)
    headings = list(_HEADING.finditer(visible))
    graph: dict[str, set[str]] = {}
    sections = {}
    for heading in headings:
        work_id = heading["id"]
        graph.setdefault(work_id, set())
        following = re.search(r"^#{1,3} ", visible[heading.end() :], re.M)
        end = heading.end() + following.start() if following else len(visible)
        sections.setdefault(work_id, []).append(visible[heading.end() : end])
    releases: set[str] = set()
    strict: list[tuple[str, str]] = []
    for work_id, bodies in sections.items():
        fields = []
        for section in bodies:
            fields.extend(_FIELD.findall(section))
        for field, value in fields:
            value = value.strip()
            if field == "Release target":
                if re.fullmatch(_VERSION, value):
                    releases.add(value)
                    graph.setdefault(f"release:{value}:ready", set()).add(work_id)
            elif field == "Completion prerequisites":
                if "\n" in value:
                    raise RoadmapDependencyError(
                        "roadmap.dependency_field_invalid",
                        f"{work_id}: Completion prerequisites must use one line",
                    )
                if value != "none":
                    strict.extend((work_id, part.strip()) for part in value.split(","))
            else:
                if re.match(r"none\b", value, re.I):
                    continue
                prefix = re.split(r"[;(]", value, maxsplit=1)[0]
                strict.extend(
                    (work_id, token.strip("`"))
                    for token in re.findall(r"release:[^\s,;()]+", prefix)
                )
                for dependency in re.findall(rf"\b{_ID}\b", prefix):
                    if dependency in sections:
                        graph[work_id].add(dependency)
    if manifest is not None:
        declared = manifest.get("releases")
        if not isinstance(declared, Mapping):
            raise RoadmapDependencyError(
                "roadmap.dependency_manifest_invalid", "release manifest needs releases"
            )
        for version, entries in declared.items():
            if not isinstance(version, str) or re.fullmatch(_VERSION, version) is None:
                raise RoadmapDependencyError(
                    "roadmap.dependency_manifest_invalid", "invalid release version"
                )
            releases.add(version)
            if not isinstance(entries, list):
                raise RoadmapDependencyError(
                    "roadmap.dependency_manifest_invalid",
                    f"{version}: goals need a list",
                )
            for entry in entries:
                if not isinstance(entry, Mapping) or not isinstance(
                    entry.get("id"), str
                ):
                    raise RoadmapDependencyError(
                        "roadmap.dependency_manifest_invalid",
                        f"{version}: invalid goal",
                    )
                if entry.get("disposition", "include") == "include":
                    graph.setdefault(f"release:{version}:ready", set()).add(entry["id"])
    for work_id, dependency in strict:
        release = _RELEASE.fullmatch(dependency)
        if dependency not in sections and (
            release is None or release[1] not in releases
        ):
            raise RoadmapDependencyError(
                "roadmap.dependency_unknown",
                f"{work_id}: unknown completion prerequisite {dependency!r}; "
                "use an existing work ID or release:VERSION:PHASE",
            )
        graph[work_id].add(dependency)
    for version in sorted(releases):
        for phase in _PHASES:
            node = f"release:{version}:{phase}"
            graph.setdefault(node, set())
            if phase in _PREDECESSOR:
                graph[node].add(f"release:{version}:{_PREDECESSOR[phase]}")

    # Iterative DFS: deep valid plans must not depend on Python's recursion limit.
    done: set[str] = set()
    for start in sorted(graph):
        if start in done:
            continue
        path = [start]
        positions = {start: 0}
        stack = [iter(sorted(graph[start]))]
        while stack:
            child = next(stack[-1], None)
            if child is None:
                finished = path.pop()
                done.add(finished)
                positions.pop(finished)
                stack.pop()
            elif child in positions:
                cycle = path[positions[child] :] + [child]
                raise RoadmapDependencyError(
                    "roadmap.dependency_cycle",
                    "completion dependency cycle: "
                    + " -> ".join(cycle)
                    + "; separate deliverable completion from later release execution",
                )
            elif child not in done:
                positions[child] = len(path)
                path.append(child)
                stack.append(iter(sorted(graph.get(child, ()))))
