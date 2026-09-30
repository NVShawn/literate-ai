"""Read-only HTML surface adapters over existing canonical JSON producers.

This registry does not render HTML, fetch assets, or publish files. The graph
adapter retains the existing graph identity and JSON bytes, including the legacy
wire schema value. Its URN identifies a schema resource, not a rewritten payload.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, ValidationError

from literate_ai.adapters.project_lock_health import observe_project_locks
from literate_ai.adapters.project_verification import observe_project_verification
from literate_ai.adapters.repository_lineage import (
    GitRepositoryLineageError,
    RepositoryLineageStoreError,
)
from literate_ai.application.planning import (
    GenerationPlanningError,
    normalize_generation_workflow_document,
)
from literate_ai.authority_graph import AuthorityGraph, AuthorityGraphError
from literate_ai.cache_directories import CacheDirectoryError, resolve_cache_directories
from literate_ai.contracts.html_observability import (
    HtmlRenderRefusal,
    HtmlRenderRequest,
    HtmlSourceBinding,
    HtmlSurface,
    HtmlView,
)
from literate_ai.contracts.identity import (
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.perf import read_performance_spans
from literate_ai.project_authority_graph import project_authority_graph
from literate_ai.projects import ProjectError, discover_project
from literate_ai.schema_catalog import SchemaCatalogError, schema_path
from literate_ai.version_check import VersionCheckError, check_versions

AUTHORITY_GRAPH_SOURCE_SCHEMA = "urn:literate-ai:schema:v2:authority-graph"
_AUTHORITY_GRAPH = HtmlSurface(
    "authority-graph", AUTHORITY_GRAPH_SOURCE_SCHEMA, ("dag",)
)

VERSION_CHECK_SOURCE_SCHEMA = "urn:literate-ai:schema:v2:version-check"
_VERSION_CHECK = HtmlSurface("version-check", VERSION_CHECK_SOURCE_SCHEMA, ("health",))
LOCK_HEALTH_SOURCE_SCHEMA = "urn:literate-ai:schema:v2:project-lock-health"
_LOCK_HEALTH = HtmlSurface("lock-health", LOCK_HEALTH_SOURCE_SCHEMA, ("health",))
VERIFICATION_HEALTH_GATES = ("authority", "locks", "source-intelligence", "receipt")
_VERIFICATION_HEALTH = HtmlSurface(
    "verification-health", "urn:literate-ai:schema:v2:project-verify", ("health",)
)
PERFORMANCE_HISTORY_SOURCE_SCHEMA = "urn:literate-ai:schema:v2:html-performance-history"
_PERFORMANCE_HISTORY = HtmlSurface(
    "performance-history", PERFORMANCE_HISTORY_SOURCE_SCHEMA, ("history",)
)
WORKFLOW_ROUTING_SOURCE_SCHEMA = (
    "urn:literate-ai:schema:v2:html-workflow-routing-catalog"
)
_WORKFLOW_ROUTING = HtmlSurface(
    "workflow-routing", WORKFLOW_ROUTING_SOURCE_SCHEMA, ("catalog",)
)

_MAXIMUM_OBSERVABILITY_RECORDS = 4096


@dataclass(frozen=True, slots=True)
class HtmlSurfaceSource:
    """Adapter-owned immutable observation; not an externally admitted wire record."""

    surface: HtmlSurface
    view: HtmlView
    binding: HtmlSourceBinding
    json_bytes: bytes

    def document(self) -> dict[str, Any]:
        """Return a fresh value so callers cannot mutate the observed source."""
        return json.loads(self.json_bytes)


def html_surfaces() -> tuple[HtmlSurface, ...]:
    return (
        _AUTHORITY_GRAPH,
        _VERSION_CHECK,
        _LOCK_HEALTH,
        _VERIFICATION_HEALTH,
        _PERFORMANCE_HISTORY,
        _WORKFLOW_ROUTING,
    )


def _view_matches(request: HtmlRenderRequest, view_id: str, project_id: str) -> bool:
    view = request.view
    return (
        view.view_id,
        view.view_version,
        view.scope_kind,
        view.scope_identifier,
    ) == (view_id, "1.0.0", "project", project_id)


def _source(
    surface: HtmlSurface, request: HtmlRenderRequest, document: dict[str, Any]
) -> HtmlSurfaceSource:
    rendered = canonical_json_bytes(document)
    return HtmlSurfaceSource(
        surface,
        request.view,
        HtmlSourceBinding(
            surface.surface_id, surface.source_schema, canonical_identity(document)
        ),
        rendered,
    )


def _load_performance_history(
    project: Path, request: HtmlRenderRequest
) -> HtmlSurfaceSource | HtmlRenderRefusal:
    try:
        selected = discover_project(project)
        if selected is None or not _view_matches(
            request, "history", selected.definition.project_id
        ):
            return HtmlRenderRefusal(
                "render.unknown_view",
                "Performance history supports history 1.0.0 for the selected "
                "project only.",
            )
        build_root = resolve_cache_directories(selected.root).obj_dir
        spans = read_performance_spans(build_root)
        total = len(spans)
        if len(spans) > _MAXIMUM_OBSERVABILITY_RECORDS:
            spans = spans[-_MAXIMUM_OBSERVABILITY_RECORDS:]
        rows = [span.to_dict() for span in spans]
        document = {
            "schema": PERFORMANCE_HISTORY_SOURCE_SCHEMA,
            "project_id": selected.definition.project_id,
            "spans": rows,
            "truncated": total > len(rows),
        }
        schema = json.loads(
            schema_path(
                "html-observability-sources.schema.json", catalog_version="v2"
            ).read_text(encoding="utf-8")
        )
        Draft202012Validator(schema["$defs"]["performanceHistory"]).validate(document)
        return _source(_PERFORMANCE_HISTORY, request, document)
    except (
        CacheDirectoryError,
        SchemaCatalogError,
        ValidationError,
        OSError,
        ValueError,
        TypeError,
        ProjectError,
    ):
        return HtmlRenderRefusal(
            "render.surface_unavailable",
            "The performance history is unavailable or failed source validation.",
        )


def _catalog_files(
    root: Path, declared: tuple[str, ...], name: str
) -> tuple[Path, ...]:
    found: list[Path] = []
    for relative in declared:
        catalog = (root / relative).resolve(strict=True)
        if not catalog.is_relative_to(root) or not catalog.is_dir():
            raise ValueError("catalog escapes the selected project")
        for path in sorted(catalog.rglob(name)):
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(catalog) or not resolved.is_file():
                raise ValueError("catalog entry escapes its declared root")
            found.append(resolved)
            if len(found) > _MAXIMUM_OBSERVABILITY_RECORDS:
                raise ValueError("catalog exceeds the observability bound")
    return tuple(found)


def _load_workflow_routing(
    project: Path, request: HtmlRenderRequest
) -> HtmlSurfaceSource | HtmlRenderRefusal:
    try:
        selected = discover_project(project)
        if selected is None or not _view_matches(
            request, "catalog", selected.definition.project_id
        ):
            return HtmlRenderRefusal(
                "render.unknown_view",
                "Workflow routing supports catalog 1.0.0 for the selected "
                "project only.",
            )
        root = selected.root.resolve(strict=True)
        workflows = []
        for path in _catalog_files(
            root, selected.definition.workflow_roots, "workflow.md"
        ):
            content = path.read_bytes()
            normalized = normalize_generation_workflow_document(
                content, project_root=root, source=path
            )
            workflows.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "content_identity": "sha256:" + hashlib.sha256(content).hexdigest(),
                    "workflow": normalized,
                }
            )
        routes = []
        for path in _catalog_files(
            root, selected.definition.routing_roots, "routing.json"
        ):
            content = path.read_bytes()
            value = json.loads(content)
            if not isinstance(value, dict):
                raise ValueError("routing document is not an object")
            routes.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "content_identity": "sha256:" + hashlib.sha256(content).hexdigest(),
                    "routing": value,
                }
            )
        document = {
            "schema": WORKFLOW_ROUTING_SOURCE_SCHEMA,
            "project_id": selected.definition.project_id,
            "workflows": workflows,
            "routes": routes,
        }
        schema = json.loads(
            schema_path(
                "html-observability-sources.schema.json", catalog_version="v2"
            ).read_text(encoding="utf-8")
        )
        Draft202012Validator(schema["$defs"]["workflowRouting"]).validate(document)
        return _source(_WORKFLOW_ROUTING, request, document)
    except (
        GenerationPlanningError,
        SchemaCatalogError,
        ValidationError,
        json.JSONDecodeError,
        OSError,
        ValueError,
        TypeError,
        ProjectError,
    ):
        return HtmlRenderRefusal(
            "render.surface_unavailable",
            "The workflow and routing catalog is unavailable or failed source "
            "validation.",
        )


def _supported_view(view: HtmlView) -> bool:
    return (
        view.view_id in _AUTHORITY_GRAPH.view_ids
        and view.view_version == "1.0.0"
        and view.scope_kind == "project"
    )


def _unknown_view() -> HtmlRenderRefusal:
    return HtmlRenderRefusal(
        "render.unknown_view",
        "The authority-graph surface supports dag 1.0.0 for the selected project only.",
    )


def _unavailable() -> HtmlRenderRefusal:
    return HtmlRenderRefusal(
        "render.surface_unavailable",
        "The selected authority graph is unavailable or failed source validation.",
    )


def bind_authority_graph(
    graph: AuthorityGraph, view: HtmlView
) -> HtmlSurfaceSource | HtmlRenderRefusal:
    """Observe an already-solved graph; do not introduce a second derivation path."""
    if not _supported_view(view) or view.scope_identifier != graph.project_id:
        return _unknown_view()
    try:
        rendered = graph.render("json").encode("utf-8")
        document = json.loads(rendered)
        schema = json.loads(
            schema_path("authority-graph.schema.json", catalog_version="v2").read_text(
                encoding="utf-8"
            )
        )
        Draft202012Validator(schema).validate(document)
        if document != graph.to_dict():
            return _unavailable()
        binding = HtmlSourceBinding(
            _AUTHORITY_GRAPH.surface_id,
            _AUTHORITY_GRAPH.source_schema,
            ContentIdentity.parse_uri(graph.identity),
        )
        return HtmlSurfaceSource(_AUTHORITY_GRAPH, view, binding, rendered)
    except (
        AuthorityGraphError,
        SchemaCatalogError,
        ValidationError,
        OSError,
        ValueError,
        TypeError,
    ):
        return _unavailable()


def _load_version_check(
    project: Path, request: HtmlRenderRequest
) -> HtmlSurfaceSource | HtmlRenderRefusal:
    view = request.view
    if (view.view_id, view.view_version, view.scope_kind) != (
        "health",
        "1.0.0",
        "project",
    ):
        return HtmlRenderRefusal(
            "render.unknown_view",
            "Version check supports health 1.0.0 for the selected project only.",
        )
    try:
        document = check_versions(project_path=project, require_project=True)
        if document["project"]["project_id"] != view.scope_identifier:
            return HtmlRenderRefusal(
                "render.unknown_view",
                "The version-check report belongs to a different "
                "or unavailable project.",
            )
        schema = json.loads(
            schema_path("version-check.schema.json", catalog_version="v2").read_text(
                encoding="utf-8"
            )
        )
        Draft202012Validator(schema["$defs"]["report"]).validate(document)
        binding = HtmlSourceBinding(
            _VERSION_CHECK.surface_id,
            _VERSION_CHECK.source_schema,
            canonical_identity(document),
        )
        return HtmlSurfaceSource(
            _VERSION_CHECK, view, binding, canonical_json_bytes(document)
        )
    except (
        VersionCheckError,
        SchemaCatalogError,
        ValidationError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
    ):
        return HtmlRenderRefusal(
            "render.surface_unavailable",
            "The version-check report is unavailable or failed source validation.",
        )


def _load_lock_health(
    project: Path, request: HtmlRenderRequest
) -> HtmlSurfaceSource | HtmlRenderRefusal:
    view = request.view
    if (view.view_id, view.view_version, view.scope_kind) != (
        "health",
        "1.0.0",
        "project",
    ):
        return HtmlRenderRefusal(
            "render.unknown_view",
            "Lock health supports health 1.0.0 for the selected project only.",
        )
    try:
        document = observe_project_locks(project).to_dict()
        if document["project_id"] != view.scope_identifier:
            return HtmlRenderRefusal(
                "render.unknown_view",
                "The lock report belongs to a different or unavailable project.",
            )
        schema = json.loads(
            schema_path(
                "project-lock-health.schema.json", catalog_version="v2"
            ).read_text(encoding="utf-8")
        )
        Draft202012Validator(schema).validate(document)
        binding = HtmlSourceBinding(
            _LOCK_HEALTH.surface_id,
            _LOCK_HEALTH.source_schema,
            canonical_identity(document),
        )
        return HtmlSurfaceSource(
            _LOCK_HEALTH, view, binding, canonical_json_bytes(document)
        )
    except (
        SchemaCatalogError,
        ValidationError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
    ):
        return HtmlRenderRefusal(
            "render.surface_unavailable",
            "The lock-health report is unavailable or failed source validation.",
        )


def _load_verification_health(
    project: Path, request: HtmlRenderRequest
) -> HtmlSurfaceSource | HtmlRenderRefusal:
    view = request.view
    if (view.view_id, view.view_version, view.scope_kind) != (
        "health",
        "1.0.0",
        "project",
    ):
        return HtmlRenderRefusal(
            "render.unknown_view",
            "Verification health supports health 1.0.0 for the selected project only.",
        )
    try:
        selected = discover_project(project)
        if selected is None or selected.definition.project_id != view.scope_identifier:
            return HtmlRenderRefusal(
                "render.unknown_view", "The selected project does not match the view."
            )
        # Full verification checks this artifact separately. Including that gate
        # here would make the source identity depend on its own rendered output.
        document, _ = observe_project_verification(
            project, gates=VERIFICATION_HEALTH_GATES
        )
        schema = json.loads(
            schema_path("project-verify.schema.json", catalog_version="v2").read_text(
                encoding="utf-8"
            )
        )
        Draft202012Validator(schema["$defs"]["report"]).validate(document)
        if (
            document["project"] != str(project)
            or tuple(row["gate"] for row in document["gates"])
            != VERIFICATION_HEALTH_GATES
        ):
            raise ValueError("Verification source has different project or coverage")
        if document["counts"] != {
            state: sum(row["state"] == state for row in document["gates"])
            for state in ("pass", "fail", "skipped")
        } or document["ok"] != (document["counts"]["fail"] == 0):
            raise ValueError("Verification source has inconsistent verdicts")
        binding = HtmlSourceBinding(
            _VERIFICATION_HEALTH.surface_id,
            _VERIFICATION_HEALTH.source_schema,
            canonical_identity(document),
        )
        return HtmlSurfaceSource(
            _VERIFICATION_HEALTH, view, binding, canonical_json_bytes(document)
        )
    except (
        ProjectError,
        SchemaCatalogError,
        ValidationError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
    ):
        return HtmlRenderRefusal(
            "render.surface_unavailable",
            "The verification report is unavailable or failed source validation.",
        )


def load_html_surface(
    project: Path, request: HtmlRenderRequest
) -> HtmlSurfaceSource | HtmlRenderRefusal:
    """Resolve the registered producer without writing, fetching, or rendering HTML."""
    if request.surface_id == _WORKFLOW_ROUTING.surface_id:
        return _load_workflow_routing(project, request)
    if request.surface_id == _PERFORMANCE_HISTORY.surface_id:
        return _load_performance_history(project, request)
    if request.surface_id == _VERIFICATION_HEALTH.surface_id:
        return _load_verification_health(project, request)
    if request.surface_id == _LOCK_HEALTH.surface_id:
        return _load_lock_health(project, request)
    if request.surface_id == _VERSION_CHECK.surface_id:
        return _load_version_check(project, request)
    if request.surface_id != _AUTHORITY_GRAPH.surface_id:
        return HtmlRenderRefusal(
            "render.unsupported_surface",
            "The requested HTML surface is not registered.",
        )
    if not _supported_view(request.view):
        return _unknown_view()
    try:
        graph = project_authority_graph(project)
    except (
        AuthorityGraphError,
        ProjectError,
        GitRepositoryLineageError,
        RepositoryLineageStoreError,
        OSError,
        ValueError,
    ):
        return _unavailable()
    return bind_authority_graph(graph, request.view)
