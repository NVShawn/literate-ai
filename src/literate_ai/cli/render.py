"""The render CLI is a thin wrapper over typed requests and the rendering service."""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path
from typing import Any

from literate_ai.adapters.html_dashboard import render_dashboard
from literate_ai.adapters.html_render import render_html
from literate_ai.adapters.html_surfaces import html_surfaces
from literate_ai.contracts.html_observability import (
    HtmlRenderRefusal,
    HtmlRenderRequest,
    HtmlRenderResult,
    HtmlView,
)
from literate_ai.contracts.paths import canonical_relative_posix_path
from literate_ai.projects import ProjectError, discover_project


def render_dashboard_from_args(args: Namespace) -> tuple[dict[str, Any], int]:
    try:
        projects = tuple(Path(item) for item in args.project)
        return render_dashboard(Path(args.root), projects, args.output), 0
    except (OSError, ValueError, TypeError, ProjectError):
        return {
            "status": "refused",
            "refusal": {
                "code": "render.dashboard_unavailable",
                "message": (
                    "Dashboard publication requires contained projects with exact "
                    "declared, rendered HTML artifacts and a safe derived output."
                ),
            },
        }, 1


def render_html_from_args(args: Namespace) -> tuple[dict[str, Any], int]:
    def refused(code: str, message: str) -> tuple[dict[str, Any], int]:
        return HtmlRenderResult(
            "refused", None, HtmlRenderRefusal(code, message)
        ).to_dict(), 1

    try:
        output = canonical_relative_posix_path(args.output, label="HTML output")
        if output.suffix != ".html":
            raise ValueError("HTML extension required")
    except (ValueError, TypeError):
        return refused(
            "render.output_outside_project",
            "HTML output must be one portable project-relative .html path.",
        )
    if args.surface not in {surface.surface_id for surface in html_surfaces()}:
        return refused(
            "render.unsupported_surface",
            "The requested HTML surface is not registered.",
        )
    scope = args.scope_identifier
    if scope is None:
        try:
            loaded = discover_project(Path(args.project))
            if loaded is None:
                raise ValueError("project not found")
            scope = loaded.definition.project_id
        except (OSError, ValueError, TypeError, ProjectError):
            return refused(
                "render.surface_unavailable",
                "The selected source project is unavailable.",
            )
    try:
        view = HtmlView(args.view, args.view_version, args.scope_kind, scope)
    except (OSError, ValueError, TypeError, ProjectError):
        return refused(
            "render.unknown_view",
            "The requested project-scoped HTML view is unavailable.",
        )
    try:
        request = HtmlRenderRequest(
            args.surface, view, args.output, args.cache_mode, args.external_asset_policy
        )
    except (ValueError, TypeError):
        return refused(
            "render.unsupported_surface", "The requested HTML surface is invalid."
        )
    result = render_html(Path(args.project), request)
    return result.to_dict(), 1 if result.status == "refused" else 0
