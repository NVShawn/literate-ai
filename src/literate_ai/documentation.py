"""Bounded Markdown links, anchors, and documentation-graph validation.

This module intentionally implements a small, documented Markdown navigation subset.
It is not a renderer.  The subset is sufficient for a portable documentation spine and
keeps admission checks deterministic without making the Python kernel depend on one
particular Markdown engine.
"""

from __future__ import annotations

import hashlib
import html
import posixpath
import re
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

_FENCE = re.compile(r"^( {0,3})(`{3,}|~{3,})([^\r\n]*)$")
_REFERENCE_DEFINITION = re.compile(
    r"^ {0,3}\[([^\]]+)\]:[ \t]*(?:<([^>\r\n]*)>|([^\s]+))"
    r"(?:[ \t]+(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|\([^\)\r\n]*\)))?[ \t]*$"
)
_INLINE_LINK = re.compile(
    r"(!?)\[([^\]]*)\]\([ \t]*(?:<([^>\r\n]*)>|([^\s\)]+))"
    r"(?:[ \t]+(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|\([^\)\r\n]*\)))?[ \t]*\)"
)
_REFERENCE_LINK = re.compile(r"(!?)\[([^\]]+)\]\[([^\]]*)\]")
_SHORTCUT_REFERENCE = re.compile(r"(!?)\[([^\]]+)\]")
_RAW_HTML_NAVIGATION = re.compile(
    r"<[A-Za-z][^>]*\b(?:href|src|srcset)[ \t]*=", re.IGNORECASE
)
_ATX_HEADING = re.compile(r"^ {0,3}#{1,6}(?:[ \t]+|$)(.*?)[ \t]*#*[ \t]*$")
_SETEXT_HEADING = re.compile(r"^ {0,3}(?:=+|-+)[ \t]*$")
_EXPLICIT_ANCHOR = re.compile(
    r"<[A-Za-z][^>]*\b(?:id|name)[ \t]*=[ \t]*"
    r"(?:\"([^\"]+)\"|'([^']+)')[^>]*>",
    re.IGNORECASE,
)
_HTML_TAG = re.compile(r"<[^>]+>")
_MARKDOWN_LINK_TEXT = re.compile(r"!?\[([^\]]+)\](?:\([^\)]*\)|\[[^\]]*\])")
_PUNCTUATION = re.compile(r"[^\w\-\s]", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")
_REFERENCE_SPACE = re.compile(r"\s+")
_ALLOWED_REMOTE_SCHEMES = frozenset({"http", "https", "mailto"})
_ROADMAP_ACTIVE_ROOT = "docs/roadmap"
_ROADMAP_HISTORY_ROOT = "docs/history/roadmap"
_ACTIVE_WORK_PATH = "docs/roadmap/active-work.md"
_ROADMAP_STATES = frozenset(
    {"active", "partial", "deferred", "completed", "historical"}
)
_ROADMAP_FIELD = re.compile(
    r"^ {0,3}-[ \t]+\*\*(Status|Owning queue item|Completion / archival evidence):\*\*"
    r"[ \t]+(.+?)[ \t]*$"
)


class DocumentationError(ValueError):
    """A documentation spine is unsafe, broken, or unreachable."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class MarkdownLink:
    destination: str
    line: int
    image: bool = False
    syntax: str = "inline"


@dataclass(frozen=True, slots=True)
class _ResolvedLink:
    source: str
    line: int
    target: str | None
    fragment: str | None
    image: bool
    external: bool

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "source": self.source,
            "line": self.line,
            "image": self.image,
            "external": self.external,
        }
        if self.target is not None:
            value["target"] = self.target
        if self.fragment is not None:
            value["fragment"] = self.fragment
        return value


def _normalize_reference(value: str) -> str:
    return _REFERENCE_SPACE.sub(" ", value.strip()).casefold()


def _blank_inline_code(line: str) -> str:
    """Blank CommonMark-style code spans while preserving string offsets."""

    characters = list(line)
    index = 0
    while index < len(line):
        if line[index] != "`":
            index += 1
            continue
        end = index
        while end < len(line) and line[end] == "`":
            end += 1
        marker = line[index:end]
        closing = line.find(marker, end)
        stop = len(line) if closing < 0 else closing + len(marker)
        characters[index:stop] = " " * (stop - index)
        index = stop
    return "".join(characters)


def _visible_markdown_lines(content: str) -> tuple[str, ...]:
    """Return lines with fenced code, comments, and inline code blanked."""

    visible: list[str] = []
    fence_character: str | None = None
    fence_length = 0
    in_comment = False
    for original in content.splitlines():
        match = _FENCE.match(original)
        if fence_character is not None:
            stripped = original.lstrip(" ")
            if (
                len(original) - len(stripped) <= 3
                and stripped.startswith(fence_character * fence_length)
                and not stripped.startswith(fence_character * fence_length + "x")
                and set(stripped.rstrip()) <= {fence_character}
            ):
                fence_character = None
                fence_length = 0
            visible.append(" " * len(original))
            continue
        if match is not None:
            marker = match.group(2)
            fence_character = marker[0]
            fence_length = len(marker)
            visible.append(" " * len(original))
            continue

        characters = list(original)
        index = 0
        while index < len(original):
            if in_comment:
                closing = original.find("-->", index)
                if closing < 0:
                    characters[index:] = " " * (len(original) - index)
                    index = len(original)
                    continue
                characters[index : closing + 3] = " " * (closing + 3 - index)
                index = closing + 3
                in_comment = False
                continue
            opening = original.find("<!--", index)
            if opening < 0:
                break
            closing = original.find("-->", opening + 4)
            if closing < 0:
                characters[opening:] = " " * (len(original) - opening)
                in_comment = True
                break
            characters[opening : closing + 3] = " " * (closing + 3 - opening)
            index = closing + 3
        visible.append(_blank_inline_code("".join(characters)))
    return tuple(visible)


def extract_markdown_links(content: str) -> tuple[MarkdownLink, ...]:
    """Extract the supported Markdown navigation subset with source line numbers."""

    lines = _visible_markdown_lines(content)
    definitions: dict[str, tuple[str, int]] = {}
    definition_lines: set[int] = set()
    for number, line in enumerate(lines, 1):
        match = _REFERENCE_DEFINITION.match(line)
        if match is None:
            continue
        label = _normalize_reference(match.group(1))
        destination = match.group(2) if match.group(2) is not None else match.group(3)
        assert destination is not None
        previous = definitions.get(label)
        if previous is not None and previous[0] != destination:
            raise DocumentationError(
                "project.documentation_reference_ambiguous",
                f"Markdown reference {match.group(1)!r} has multiple targets",
            )
        definitions[label] = (destination, number)
        definition_lines.add(number)

    links: list[MarkdownLink] = []
    for number, line in enumerate(lines, 1):
        if number in definition_lines:
            continue
        if _RAW_HTML_NAVIGATION.search(line):
            raise DocumentationError(
                "project.documentation_html_navigation_unsupported",
                "raw HTML href/src/srcset navigation is not admitted; use Markdown",
            )
        consumed: list[tuple[int, int]] = []
        for match in _INLINE_LINK.finditer(line):
            destination = (
                match.group(3) if match.group(3) is not None else match.group(4)
            )
            assert destination is not None
            links.append(
                MarkdownLink(destination, number, match.group(1) == "!", "inline")
            )
            consumed.append(match.span())
        for match in _REFERENCE_LINK.finditer(line):
            if any(start <= match.start() < end for start, end in consumed):
                continue
            label = match.group(3) or match.group(2)
            definition = definitions.get(_normalize_reference(label))
            if definition is None:
                raise DocumentationError(
                    "project.documentation_reference_missing",
                    f"Markdown reference {label!r} has no definition",
                )
            links.append(
                MarkdownLink(definition[0], number, match.group(1) == "!", "reference")
            )
            consumed.append(match.span())
        for match in _SHORTCUT_REFERENCE.finditer(line):
            if any(start <= match.start() < end for start, end in consumed):
                continue
            definition = definitions.get(_normalize_reference(match.group(2)))
            if definition is None:
                continue
            links.append(
                MarkdownLink(definition[0], number, match.group(1) == "!", "shortcut")
            )
    return tuple(links)


def _heading_text(value: str) -> str:
    value = _MARKDOWN_LINK_TEXT.sub(lambda match: match.group(1), value)
    value = _HTML_TAG.sub("", value)
    return html.unescape(value).replace("`", "").replace("*", "")


def _slug(value: str) -> str:
    cleaned = _PUNCTUATION.sub("", _heading_text(value).casefold()).strip()
    return _WHITESPACE.sub("-", cleaned)


def markdown_anchors(content: str) -> frozenset[str]:
    """Return deterministic GitHub-style heading and explicit anchor identifiers."""

    lines = _visible_markdown_lines(content)
    anchors: set[str] = set()
    counts: dict[str, int] = {}

    def add_heading(value: str) -> None:
        base = _slug(value)
        if not base:
            return
        count = counts.get(base, 0)
        counts[base] = count + 1
        anchors.add(base if count == 0 else f"{base}-{count}")

    for index, line in enumerate(lines):
        for match in _EXPLICIT_ANCHOR.finditer(line):
            anchors.add(html.unescape(match.group(1) or match.group(2)))
        atx = _ATX_HEADING.match(line)
        if atx is not None:
            add_heading(atx.group(1))
            continue
        if index > 0 and _SETEXT_HEADING.match(line) and lines[index - 1].strip():
            add_heading(lines[index - 1].strip())
    return frozenset(anchors)


def _has_symlink_component(root: Path, target: Path) -> bool:
    try:
        relative = target.relative_to(root)
    except ValueError:
        return True
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _content_identity(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _roadmap_area(path: str) -> str | None:
    if path == _ACTIVE_WORK_PATH or not path.endswith(".md"):
        return None
    if path.startswith(_ROADMAP_ACTIVE_ROOT + "/"):
        return "active"
    if path.startswith(_ROADMAP_HISTORY_ROOT + "/"):
        return "history"
    return None


def _roadmap_header_fields(path: str, content: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in _visible_markdown_lines(content):
        if re.match(r"^ {0,3}##(?:[ \t]+|$)", line):
            break
        match = _ROADMAP_FIELD.match(line)
        if match is None:
            continue
        name, value = match.groups()
        if name in fields:
            raise DocumentationError(
                "project.roadmap_lifecycle_header_duplicate",
                f"detailed roadmap repeats lifecycle field {name!r}: {path}",
            )
        fields[name] = value.strip()
    missing = tuple(
        name
        for name in ("Status", "Owning queue item", "Completion / archival evidence")
        if name not in fields
    )
    if missing:
        raise DocumentationError(
            "project.roadmap_lifecycle_header_missing",
            "detailed roadmap lifecycle header is missing "
            + ", ".join(missing)
            + f": {path}",
        )
    return fields


def _roadmap_owner_fragment(
    *, path: str, value: str, active_work_anchors: frozenset[str]
) -> str:
    links = tuple(link for link in extract_markdown_links(value) if not link.image)
    if len(links) != 1:
        raise DocumentationError(
            "project.roadmap_lifecycle_owner_invalid",
            "detailed roadmap Owning queue item must contain exactly one Markdown "
            f"link to active-work.md: {path}",
        )
    split = urlsplit(html.unescape(links[0].destination.strip()))
    destination = unquote(split.path)
    target = posixpath.normpath(posixpath.join(posixpath.dirname(path), destination))
    fragment = unquote(split.fragment) if split.fragment else ""
    if (
        split.scheme
        or split.netloc
        or "\\" in destination
        or target != _ACTIVE_WORK_PATH
        or not fragment
    ):
        raise DocumentationError(
            "project.roadmap_lifecycle_owner_invalid",
            "detailed roadmap Owning queue item must target one heading in "
            f"docs/roadmap/active-work.md: {path}",
        )
    if fragment not in active_work_anchors:
        raise DocumentationError(
            "project.roadmap_lifecycle_owner_dangling",
            f"detailed roadmap Owning queue item is unavailable: {path}#{fragment}",
        )
    return fragment


def validate_roadmap_lifecycle(documents: Mapping[str, str]) -> None:
    """Require explicit ownership and terminal evidence for detailed roadmaps.

    ``active-work.md`` remains the sole resumable queue. Supporting plans beneath
    ``docs/roadmap`` must point back to one durable item or program heading there.
    Archived plans move beneath ``docs/history/roadmap`` and retain the same ownership
    link so their origin remains inspectable.
    """

    roadmap_paths = tuple(
        path for path in sorted(documents) if _roadmap_area(path) is not None
    )
    if not roadmap_paths:
        return
    active_work = documents.get(_ACTIVE_WORK_PATH)
    if active_work is None:
        raise DocumentationError(
            "project.roadmap_lifecycle_queue_missing",
            "detailed roadmaps require docs/roadmap/active-work.md",
        )
    active_work_anchors = markdown_anchors(active_work)
    for path in roadmap_paths:
        area = _roadmap_area(path)
        assert area is not None
        fields = _roadmap_header_fields(path, documents[path])
        status = fields["Status"]
        if status not in _ROADMAP_STATES:
            raise DocumentationError(
                "project.roadmap_lifecycle_status_invalid",
                f"detailed roadmap has unknown Status {status!r}: {path}",
            )
        if (area == "active" and status == "historical") or (
            area == "history" and status != "historical"
        ):
            raise DocumentationError(
                "project.roadmap_lifecycle_location_invalid",
                "detailed roadmap Status "
                f"{status!r} conflicts with its location: {path}",
            )
        _roadmap_owner_fragment(
            path=path,
            value=fields["Owning queue item"],
            active_work_anchors=active_work_anchors,
        )
        evidence = fields["Completion / archival evidence"]
        if status in {"completed", "historical"}:
            evidence_links = tuple(
                link for link in extract_markdown_links(evidence) if not link.image
            )
            if not evidence_links:
                raise DocumentationError(
                    "project.roadmap_lifecycle_evidence_missing",
                    "completed or historical roadmap requires linked completion or "
                    f"archival evidence: {path}",
                )


def validate_documentation_graph(
    *,
    project_root: Path,
    agent_skill: Path,
    agent_skill_content: bytes | None = None,
    documents: Mapping[str, bytes],
    assets: Mapping[str, bytes],
) -> dict[str, object]:
    """Validate every local link and require a fully reachable documentation spine."""

    root = project_root.resolve(strict=True)
    document_paths = set(documents)
    asset_paths = set(assets)
    if not document_paths:
        raise DocumentationError(
            "project.documentation_missing",
            "declared documentation roots contain no Markdown documents",
        )

    contents: dict[str, str] = {}
    for path, value in documents.items():
        try:
            contents[path] = value.decode("utf-8")
        except UnicodeError as exc:
            raise DocumentationError(
                "project.documentation_invalid",
                f"declared Markdown document is not UTF-8: {path}",
            ) from exc
    validate_roadmap_lifecycle(contents)
    skill_path = agent_skill.resolve(strict=True)
    skill_relative = skill_path.relative_to(root).as_posix()
    try:
        skill_content = (
            skill_path.read_text(encoding="utf-8")
            if agent_skill_content is None
            else agent_skill_content.decode("utf-8")
        )
    except (OSError, UnicodeError) as exc:
        raise DocumentationError(
            "project.agent_skill_invalid", "agent onboarding skill is unreadable"
        ) from exc
    all_contents = {skill_relative: skill_content, **contents}
    anchors = {path: markdown_anchors(content) for path, content in contents.items()}
    edges: dict[str, set[str]] = {path: set() for path in all_contents}
    referenced_assets: set[str] = set()
    resolved_links: list[_ResolvedLink] = []

    for source, content in all_contents.items():
        source_path = root.joinpath(*PurePosixPath(source).parts)
        for link in extract_markdown_links(content):
            raw = html.unescape(link.destination.strip())
            split = urlsplit(raw)
            if split.scheme:
                if split.scheme.casefold() not in _ALLOWED_REMOTE_SCHEMES:
                    raise DocumentationError(
                        "project.documentation_link_scheme_invalid",
                        "unsupported documentation link scheme in "
                        f"{source}:{link.line}",
                    )
                if link.image:
                    raise DocumentationError(
                        "project.documentation_remote_asset",
                        "remote documentation assets are not content-identified: "
                        f"{source}:{link.line}",
                    )
                resolved_links.append(
                    _ResolvedLink(source, link.line, None, None, False, True)
                )
                continue
            if split.netloc or raw.startswith("//"):
                raise DocumentationError(
                    "project.documentation_link_scheme_invalid",
                    "protocol-relative documentation link is unsupported: "
                    f"{source}:{link.line}",
                )
            destination = unquote(split.path)
            fragment = unquote(split.fragment) if split.fragment else None
            if "\\" in destination or destination.startswith("/"):
                raise DocumentationError(
                    "project.documentation_link_escape",
                    "documentation link is not a relative POSIX path: "
                    f"{source}:{link.line}",
                )
            configured = (
                source_path if not destination else source_path.parent / destination
            )
            if _has_symlink_component(root, configured):
                raise DocumentationError(
                    "project.documentation_link_symlink",
                    "documentation link traverses a symbolic link: "
                    f"{source}:{link.line}",
                )
            try:
                target = configured.resolve(strict=True)
            except OSError as exc:
                raise DocumentationError(
                    "project.documentation_link_missing",
                    f"documentation link target is unavailable: {source}:{link.line}",
                ) from exc
            if not target.is_relative_to(root):
                raise DocumentationError(
                    "project.documentation_link_escape",
                    f"documentation link escapes the project: {source}:{link.line}",
                )
            target_relative = target.relative_to(root).as_posix()
            if source == skill_relative and target_relative not in document_paths:
                raise DocumentationError(
                    "project.agent_skill_documentation_undeclared",
                    "agent onboarding links must not target undeclared documentation: "
                    + destination,
                )
            if target.is_file() and target.suffix.casefold() == ".md":
                if target_relative not in document_paths:
                    raise DocumentationError(
                        "project.documentation_markdown_undeclared",
                        "documentation links to undeclared Markdown: "
                        f"{source}:{link.line}",
                    )
                edges[source].add(target_relative)
                if fragment is not None and fragment not in anchors[target_relative]:
                    raise DocumentationError(
                        "project.documentation_fragment_missing",
                        "documentation fragment is unavailable: "
                        f"{source}:{link.line}#{fragment}",
                    )
            elif fragment is not None:
                raise DocumentationError(
                    "project.documentation_fragment_invalid",
                    "documentation fragment targets non-Markdown content: "
                    f"{source}:{link.line}",
                )
            if target_relative in asset_paths:
                referenced_assets.add(target_relative)
            resolved_links.append(
                _ResolvedLink(
                    source,
                    link.line,
                    target_relative,
                    fragment,
                    link.image,
                    False,
                )
            )

    roots = edges[skill_relative]
    if not roots:
        raise DocumentationError(
            "project.agent_skill_documentation_missing",
            "agent onboarding skill must link to declared documentation",
        )
    reachable: set[str] = set()
    pending = deque(sorted(roots))
    while pending:
        current = pending.popleft()
        if current in reachable:
            continue
        reachable.add(current)
        pending.extend(sorted(edges.get(current, set()) - reachable))
    unreachable = sorted(document_paths - reachable)
    if unreachable:
        raise DocumentationError(
            "project.documentation_unreachable",
            "declared documentation is unreachable from SKILL.md: "
            + ", ".join(unreachable[:5]),
        )
    unreferenced_assets = sorted(asset_paths - referenced_assets)
    if unreferenced_assets:
        raise DocumentationError(
            "project.documentation_asset_unreferenced",
            "declared documentation assets are not referenced: "
            + ", ".join(unreferenced_assets[:5]),
        )

    return {
        "entrypoint": skill_relative,
        "reachable_documents": sorted(reachable),
        "links": [item.to_dict() for item in resolved_links],
        "assets": [
            {"path": path, "identity": _content_identity(assets[path])}
            for path in sorted(assets)
        ],
    }


__all__ = [
    "DocumentationError",
    "MarkdownLink",
    "extract_markdown_links",
    "markdown_anchors",
    "validate_roadmap_lifecycle",
    "validate_documentation_graph",
]
