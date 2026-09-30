"""Strict, human-readable hierarchical Markdown specification provider."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from literate_ai.adapters.component_markdown import (
    ComponentMarkdownError,
    parse_component_markdown,
)
from literate_ai.contracts import (
    SpecificationRequirement,
    SpecificationScenario,
    SpecificationSet,
)

from .openspec import (
    LoadedOpenSpec,
    LoadedSpecificationArtifacts,
    OpenSpecError,
    OpenSpecProvider,
)

PROVIDER_KIND = "literate-markdown"
PROVIDER_VERSION = "1"
CONTEXT_PATH = ".literate/specification-context.json"
_FRONTMATTER_FIELDS = frozenset(
    {"id", "name", "summary", "kind", "parent", "references", "status"}
)
_REQUIRED_FIELDS = frozenset({"name", "summary", "kind"})
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_-]*(?:\.[a-z0-9][a-z0-9_-]*)*$")
_SEGMENT = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_KEY_VALUE = re.compile(r"^([a-z][a-z0-9_]*):(.*)$")
_STATUS_VALUES = frozenset({"draft", "review", "approved", "deprecated"})
_CANONICAL_FRONTMATTER_FIELDS = (
    "name",
    "summary",
    "kind",
    "status",
    "id",
    "parent",
    "references",
)


@dataclass(frozen=True, slots=True)
class LiterateSpecificationNode:
    node_id: str
    name: str
    summary: str
    kind: str
    path: str
    parent: str | None
    references: tuple[str, ...]
    status: str | None
    content_identity: str

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "urn:literate-ai:schema:v2:specification-node",
            "id": self.node_id,
            "name": self.name,
            "summary": self.summary,
            "kind": self.kind,
            "path": self.path,
            "parent": self.parent,
            "references": list(self.references),
            "status": self.status,
            "content_identity": self.content_identity,
        }


@dataclass(frozen=True, slots=True)
class LiterateSpecificationContext:
    root_id: str
    nodes: tuple[LiterateSpecificationNode, ...]
    effective_documents: tuple[tuple[str, tuple[str, ...]], ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "urn:literate-ai:schema:v2:effective-specification-context",
            "root_id": self.root_id,
            "nodes": [node.to_dict() for node in self.nodes],
            "effective_documents": [
                {"node_id": node_id, "document_ids": list(document_ids)}
                for node_id, document_ids in self.effective_documents
            ],
        }

    @property
    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")


class LiterateMarkdownProvider:
    """Load narrow Markdown nodes and derive their deterministic context graph.

    The frontmatter is intentionally a strict YAML subset: top-level scalar fields and
    one block or inline string list for ``references``. General YAML features would add
    aliases, implicit typing, and parser-specific behavior without improving ordinary
    spec authoring.
    """

    provider_id = "specification-provider:literate-markdown@1"

    def __init__(
        self,
        *,
        maximum_artifacts: int = 256,
        maximum_artifact_bytes: int = 2 * 1024 * 1024,
        maximum_total_bytes: int = 8 * 1024 * 1024,
        maximum_path_bytes: int = 512,
    ) -> None:
        self._loader = OpenSpecProvider(
            maximum_artifacts=maximum_artifacts,
            maximum_artifact_bytes=maximum_artifact_bytes,
            maximum_total_bytes=maximum_total_bytes,
            maximum_path_bytes=maximum_path_bytes,
        )

    def load(
        self,
        root: Path,
        paths: Iterable[str],
        *,
        id_prefix: str,
        baseline_id: str | None = None,
        active_change_id: str | None = None,
    ) -> LoadedOpenSpec:
        declared_paths = tuple(paths)
        if not declared_paths:
            raise OpenSpecError(
                "literate_markdown.root_missing",
                "A literate Markdown corpus requires component.md or one root spec.md",
            )
        snapshot = self._loader.snapshot(root, declared_paths)
        context = _assemble_context(snapshot, root=root, id_prefix=id_prefix)
        specification_set = SpecificationSet(
            provider_kind=PROVIDER_KIND,
            provider_version=PROVIDER_VERSION,
            artifacts=snapshot.artifacts,
            requirements=(*snapshot.requirements, *_node_context_requirements(context)),
            baseline_id=baseline_id,
            active_change_id=active_change_id,
        )
        return LoadedOpenSpec(
            specification_set,
            snapshot.contents,
            (CONTEXT_PATH, context.canonical_bytes),
        )


def format_literate_markdown_document(path: str, content: bytes) -> bytes:
    """Return the canonical, semantically equivalent form of one Markdown node.

    This deliberately has no filesystem API.  Application services can therefore
    offer check/preview behavior without granting an apparently read-only operation
    an accidental write path.
    """

    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OpenSpecError(
            "literate_markdown.not_utf8", f"Specification is not UTF-8: {path}"
        ) from exc
    metadata = _parse_frontmatter(path, text)
    lines = text.splitlines()
    # _parse_frontmatter has already proved that this delimiter exists.
    end = lines.index("---", 1)
    body = "\n".join(lines[end + 1 :]).rstrip("\n")

    frontmatter = ["---"]
    for key in _CANONICAL_FRONTMATTER_FIELDS:
        if key not in metadata:
            continue
        value = metadata[key]
        if key == "references":
            references = tuple(value)
            if references:
                frontmatter.append("references:")
                frontmatter.extend(f"  - {item}" for item in references)
            else:
                frontmatter.append("references: []")
        elif key in {"name", "summary"}:
            frontmatter.append(
                f"{key}: {json.dumps(value, ensure_ascii=False, separators=(',', ':'))}"
            )
        else:
            frontmatter.append(f"{key}: {value}")
    frontmatter.append("---")
    canonical = "\n".join(frontmatter)
    if body:
        canonical = f"{canonical}\n{body}"
    return f"{canonical}\n".encode()


def _assemble_context(
    loaded: LoadedSpecificationArtifacts, *, root: Path, id_prefix: str
) -> LiterateSpecificationContext:
    if not _IDENTIFIER.fullmatch(id_prefix):
        raise OpenSpecError(
            "literate_markdown.id_prefix_invalid",
            "The Component coordinate cannot form a dotted specification ID",
        )
    paths = tuple(path for path, _ in loaded.contents)
    root_path = PurePosixPath(paths[0])
    component_root = root_path == PurePosixPath("component.md")
    if not component_root and root_path.name != "spec.md":
        raise OpenSpecError(
            "literate_markdown.root_invalid",
            "The first specification root must be component.md or a spec.md document",
        )
    corpus_root = root_path.parent
    if any(not PurePosixPath(path).is_relative_to(corpus_root) for path in paths):
        raise OpenSpecError(
            "literate_markdown.corpus_escape",
            "Every specification node must be below the root spec.md directory",
        )

    node_contents = tuple(
        (path, content)
        for path, content in loaded.contents
        if PurePosixPath(path).suffix == ".md"
    )
    unsupported = tuple(
        path for path in paths if PurePosixPath(path).suffix not in {".md", ".json"}
    )
    if unsupported:
        raise OpenSpecError(
            "literate_markdown.supporting_artifact_invalid",
            "Literate Markdown supporting artifacts must be JSON",
        )
    metadata: dict[str, dict[str, Any]] = {}
    derived_ids: dict[str, str] = {}
    for path, content in node_contents:
        relative = PurePosixPath(path).relative_to(corpus_root)
        if component_root and relative == PurePosixPath("component.md"):
            derived_ids[path] = id_prefix
        else:
            _validate_node_path(relative)
            derived_ids[path] = _derived_id(relative, id_prefix)
        try:
            text = content.decode("utf-8")
        except (
            UnicodeDecodeError
        ) as exc:  # OpenSpec already checks; keep local invariant.
            raise OpenSpecError(
                "literate_markdown.not_utf8", f"Specification is not UTF-8: {path}"
            ) from exc
        if component_root and relative == PurePosixPath("component.md"):
            try:
                authoring = parse_component_markdown(
                    root / "component.md", text, project_root=root
                )
            except (ComponentMarkdownError, OSError) as exc:
                raise OpenSpecError(
                    "literate_markdown.component_invalid",
                    "component.md is not valid Component authoring authority",
                ) from exc
            metadata[path] = {
                "name": authoring.display_name,
                "summary": "Complete Component intent and behavior in component.md",
                "kind": "component",
                "references": [],
            }
        else:
            metadata[path] = _parse_frontmatter(path, text)

    if len(set(derived_ids.values())) != len(derived_ids):
        raise OpenSpecError(
            "literate_markdown.id_duplicate",
            "Specification paths derive duplicate dotted IDs",
        )
    by_path = dict(derived_ids)
    artifacts_by_path = {artifact.uri: artifact for artifact in loaded.artifacts}
    nodes: list[LiterateSpecificationNode] = []
    for path, _ in node_contents:
        artifact = artifacts_by_path[path]
        values = metadata[path]
        node_id = derived_ids[path]
        explicit_id = values.get("id")
        if explicit_id is not None and explicit_id != node_id:
            raise OpenSpecError(
                "literate_markdown.id_mismatch",
                f"Specification id {explicit_id!r} does not match derived id "
                f"{node_id!r}",
            )
        parent = _derived_parent(
            PurePosixPath(path),
            corpus_root,
            by_path,
            component_root=component_root,
        )
        explicit_parent = values.get("parent")
        if explicit_parent is not None and explicit_parent != parent:
            raise OpenSpecError(
                "literate_markdown.parent_mismatch",
                f"Specification parent {explicit_parent!r} does not match {parent!r}",
            )
        nodes.append(
            LiterateSpecificationNode(
                node_id=node_id,
                name=values["name"],
                summary=values["summary"],
                kind=values["kind"],
                path=path,
                parent=parent,
                references=tuple(values.get("references", ())),
                status=values.get("status"),
                content_identity=artifact.identity.uri,
            )
        )

    by_id = {node.node_id: node for node in nodes}
    for node in nodes:
        missing = tuple(item for item in node.references if item not in by_id)
        if missing:
            raise OpenSpecError(
                "literate_markdown.reference_missing",
                f"Specification {node.node_id!r} references unknown node "
                f"{missing[0]!r}",
            )
        if node.node_id in node.references:
            raise OpenSpecError(
                "literate_markdown.reference_cycle",
                f"Specification {node.node_id!r} references itself",
            )
    _require_acyclic_references(by_id)
    effective_documents = tuple(
        (node.node_id, _effective_documents(node.node_id, by_id)) for node in nodes
    )
    return LiterateSpecificationContext(
        nodes[0].node_id, tuple(nodes), effective_documents
    )


def _node_context_requirements(
    context: LiterateSpecificationContext,
) -> tuple[SpecificationRequirement, ...]:
    """Project every free-form node into the typed requirement contract.

    The complete normative text remains in the content-addressed Markdown artifact.
    This projection gives otherwise free-form VFI-style nodes a stable machine handle
    without forcing Requirement/Scenario boilerplate into every authored document.
    """

    return tuple(
        SpecificationRequirement(
            requirement_id=f"{node.node_id}.document",
            title=node.name,
            statement=node.summary,
            scenarios=(
                SpecificationScenario(
                    scenario_id=f"{node.node_id}.document.context",
                    title="Effective context assembly",
                    given=(),
                    when=("the specification node participates in generation",),
                    then=(
                        "its complete Markdown artifact is supplied under its exact "
                        "content identity",
                    ),
                ),
            ),
        )
        for node in context.nodes
    )


def _parse_frontmatter(path: str, text: str) -> dict[str, Any]:
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise OpenSpecError(
            "literate_markdown.frontmatter_missing",
            f"Specification must begin with YAML frontmatter: {path}",
        )
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise OpenSpecError(
            "literate_markdown.frontmatter_unclosed",
            f"Specification frontmatter is not closed: {path}",
        ) from exc
    result: dict[str, Any] = {}
    active_list: str | None = None
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if "\t" in line:
            raise OpenSpecError(
                "literate_markdown.frontmatter_invalid",
                f"Specification frontmatter cannot contain tabs: {path}",
            )
        if line.startswith("  - "):
            if active_list != "references":
                raise OpenSpecError(
                    "literate_markdown.frontmatter_invalid",
                    f"Only references may use a block list: {path}",
                )
            result[active_list].append(_scalar(line[4:], path=path))
            continue
        if line.startswith(" "):
            raise OpenSpecError(
                "literate_markdown.frontmatter_invalid",
                f"Nested frontmatter mappings are unsupported: {path}",
            )
        match = _KEY_VALUE.fullmatch(line)
        if match is None:
            raise OpenSpecError(
                "literate_markdown.frontmatter_invalid",
                f"Invalid frontmatter line in {path}: {line!r}",
            )
        key, raw = match.groups()
        if key not in _FRONTMATTER_FIELDS:
            raise OpenSpecError(
                "literate_markdown.frontmatter_key_unknown",
                f"Unknown frontmatter key {key!r} in {path}",
            )
        if key in result:
            raise OpenSpecError(
                "literate_markdown.frontmatter_key_duplicate",
                f"Duplicate frontmatter key {key!r} in {path}",
            )
        active_list = None
        raw = raw.strip()
        if key == "references":
            if not raw:
                result[key] = []
                active_list = key
            elif raw == "[]":
                result[key] = []
            else:
                result[key] = _inline_list(raw, path=path)
        else:
            if not raw:
                raise OpenSpecError(
                    "literate_markdown.frontmatter_value_missing",
                    f"Frontmatter value {key!r} is empty in {path}",
                )
            result[key] = _scalar(raw, path=path)
    missing = _REQUIRED_FIELDS - result.keys()
    if missing:
        raise OpenSpecError(
            "literate_markdown.frontmatter_required",
            f"Specification {path} is missing {', '.join(sorted(missing))}",
        )
    for key in ("id", "parent"):
        value = result.get(key)
        if value is not None and not _IDENTIFIER.fullmatch(value):
            raise OpenSpecError(
                "literate_markdown.identifier_invalid",
                f"Frontmatter {key!r} is not a dotted identifier in {path}",
            )
    for reference in result.get("references", ()):
        if not _IDENTIFIER.fullmatch(reference):
            raise OpenSpecError(
                "literate_markdown.identifier_invalid",
                f"Reference {reference!r} is not a dotted identifier in {path}",
            )
    if len(set(result.get("references", ()))) != len(result.get("references", ())):
        raise OpenSpecError(
            "literate_markdown.reference_duplicate",
            f"Specification {path} repeats a reference",
        )
    if len(result.get("references", ())) > 64:
        raise OpenSpecError(
            "literate_markdown.reference_limit",
            f"Specification {path} declares too many references",
        )
    if len(result["name"]) > 256 or len(result["summary"]) > 1024:
        raise OpenSpecError(
            "literate_markdown.frontmatter_value_limit",
            f"Specification title or summary is too long in {path}",
        )
    if len(result["kind"]) > 64 or not _SEGMENT.fullmatch(result["kind"]):
        raise OpenSpecError(
            "literate_markdown.kind_invalid",
            f"Specification kind is not a portable identifier in {path}",
        )
    status = result.get("status")
    if status is not None and status not in _STATUS_VALUES:
        raise OpenSpecError(
            "literate_markdown.status_invalid",
            f"Specification status is unsupported in {path}",
        )
    return result


def _scalar(raw: str, *, path: str) -> str:
    value = raw.strip()
    if value.startswith('"'):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise OpenSpecError(
                "literate_markdown.frontmatter_invalid",
                f"Invalid quoted frontmatter scalar in {path}",
            ) from exc
        if not isinstance(decoded, str) or not decoded:
            raise OpenSpecError(
                "literate_markdown.frontmatter_invalid",
                f"Frontmatter scalars must be non-empty strings in {path}",
            )
        return decoded
    if value.startswith("'"):
        if not value.endswith("'") or len(value) < 2:
            raise OpenSpecError(
                "literate_markdown.frontmatter_invalid",
                f"Invalid quoted frontmatter scalar in {path}",
            )
        value = value[1:-1].replace("''", "'")
    if not value or value[0] in "[{&*!|>" or value in {"null", "true", "false", "~"}:
        raise OpenSpecError(
            "literate_markdown.frontmatter_invalid",
            f"Unsupported YAML scalar in {path}",
        )
    return value


def _inline_list(raw: str, *, path: str) -> list[str]:
    if not raw.startswith("[") or not raw.endswith("]"):
        raise OpenSpecError(
            "literate_markdown.frontmatter_invalid",
            f"References must be [] or a string list in {path}",
        )
    content = raw[1:-1].strip()
    if not content:
        return []
    return [_scalar(item, path=path) for item in content.split(",")]


def _validate_node_path(path: PurePosixPath) -> None:
    if path.suffix != ".md" or any(
        not _SEGMENT.fullmatch(part) for part in path.parts[:-1]
    ):
        raise OpenSpecError(
            "literate_markdown.path_invalid",
            f"Specification path cannot derive a portable ID: {path}",
        )
    stem = path.stem
    if stem != "spec" and not _SEGMENT.fullmatch(stem):
        raise OpenSpecError(
            "literate_markdown.path_invalid",
            f"Specification filename cannot derive a portable ID: {path}",
        )


def _derived_id(path: PurePosixPath, prefix: str) -> str:
    parts = list(path.parts)
    if parts[-1] == "spec.md":
        parts.pop()
    else:
        parts[-1] = path.stem
    return ".".join((prefix, *parts)) if parts else prefix


def _derived_parent(
    path: PurePosixPath,
    corpus_root: PurePosixPath,
    by_path: dict[str, str],
    *,
    component_root: bool = False,
) -> str | None:
    relative = path.relative_to(corpus_root)
    if relative == PurePosixPath("spec.md") or (
        component_root and relative == PurePosixPath("component.md")
    ):
        return None
    parent_relative = (
        relative.parent.parent / "spec.md"
        if relative.name == "spec.md"
        else relative.parent / "spec.md"
    )
    parent_path = (corpus_root / parent_relative).as_posix()
    parent = by_path.get(parent_path)
    if (
        component_root
        and parent is None
        and parent_relative == PurePosixPath("spec.md")
    ):
        parent = by_path.get((corpus_root / "component.md").as_posix())
    if parent is None:
        raise OpenSpecError(
            "literate_markdown.parent_missing",
            f"Specification {path} has no containing spec.md node",
        )
    return parent


def _require_acyclic_references(
    by_id: dict[str, LiterateSpecificationNode],
) -> None:
    visited: set[str] = set()
    visiting: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in visited:
            return
        if node_id in visiting:
            raise OpenSpecError(
                "literate_markdown.reference_cycle",
                f"Specification reference cycle includes {node_id!r}",
            )
        visiting.add(node_id)
        for reference in by_id[node_id].references:
            visit(reference)
        visiting.remove(node_id)
        visited.add(node_id)

    for node_id in by_id:
        visit(node_id)


def _effective_documents(
    node_id: str, by_id: dict[str, LiterateSpecificationNode]
) -> tuple[str, ...]:
    ordered: list[str] = []
    seen: set[str] = set()
    expanded_references: set[str] = set()

    def append(value: str) -> None:
        if value not in seen:
            seen.add(value)
            ordered.append(value)

    ancestors: list[str] = []
    parent = by_id[node_id].parent
    while parent is not None:
        ancestors.append(parent)
        parent = by_id[parent].parent
    for ancestor in reversed(ancestors):
        append(ancestor)

    def reference_context(reference_id: str) -> None:
        if reference_id in expanded_references:
            return
        expanded_references.add(reference_id)
        reference = by_id[reference_id]
        reference_ancestors: list[str] = []
        parent_id = reference.parent
        while parent_id is not None:
            reference_ancestors.append(parent_id)
            parent_id = by_id[parent_id].parent
        for ancestor_id in reversed(reference_ancestors):
            append(ancestor_id)
        for transitive in reference.references:
            reference_context(transitive)
        append(reference_id)

    for reference_id in by_id[node_id].references:
        reference_context(reference_id)
    append(node_id)
    return tuple(ordered)


__all__ = [
    "CONTEXT_PATH",
    "LiterateMarkdownProvider",
    "LiterateSpecificationContext",
    "LiterateSpecificationNode",
    "PROVIDER_KIND",
    "PROVIDER_VERSION",
    "format_literate_markdown_document",
]
