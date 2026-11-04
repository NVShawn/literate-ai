"""Installed-renderer binding and read-only output intent for tree HTML.

A plan observes paths and the actual non-editable framework. It neither captures
source payloads nor initializes a cache/output namespace. Its identity will be an
explicit acknowledgement input to publication, never reconstructed live custody.
"""

from __future__ import annotations

from pathlib import Path

from literate_ai.contracts.html_observability import HtmlRenderRefusal
from literate_ai.contracts.html_trees import HtmlTreeViewDeclaration
from literate_ai.contracts.identity import canonical_identity

from .html_framework import observe_html_framework
from .html_tree_output_initialization import plan_initial_html_tree_output
from .repository_refresh import _local_identity_wire
from .retained_project_inputs import _root

_RENDERER_URI = "renderer://literate-ai/information-cards/1.0.0"


class HtmlTreeRenderError(ValueError):
    """A typed refusal with no implication that source/output was admitted."""

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _framework():
    observed = observe_html_framework()
    if isinstance(observed, HtmlRenderRefusal):
        raise HtmlTreeRenderError(
            "render.tree.framework_unavailable",
            "Tree rendering requires the current non-editable framework installation.",
        )
    return observed


def _renderer_identity(framework):
    return canonical_identity(
        {
            "schema": "literate-ai/retained-tree-html-renderer-binding@1",
            "renderer": _RENDERER_URI,
            "framework_distribution_identity": (
                framework.framework_distribution_identity.to_dict()
            ),
            "schema_catalog_release": framework.schema_catalog_release,
            "generation_schema": "literate-ai/retained-tree-html-generation@1",
        }
    )


def _source_node(root, relative):
    source = _root(root / relative)
    if not source.is_relative_to(root):
        raise HtmlTreeRenderError(
            "render.tree.source_outside_root",
            "The selected source must be a direct directory inside the chosen root.",
        )
    node = source.lstat()
    return _local_identity_wire(
        (node.st_dev, node.st_ino, node.st_mode, node.st_uid, node.st_gid)
    )


def plan_html_tree_render(root, declaration):
    """Observe initial output intent without creating files or publication rights.

    Source contents are observed only during the separately authorized render.
    This plan binds the selected physical source root, declared finite policy,
    actual renderer and absent output coordinates; it does not claim a captured
    source identity, existing-output ownership or resource qualification.
    """
    if not isinstance(declaration, HtmlTreeViewDeclaration):
        raise TypeError("render.tree.typed-declaration-required")
    root = _root(Path(root))
    framework = _framework()
    renderer = _renderer_identity(framework)
    request = declaration.resolve(renderer)
    source_node = _source_node(root, request.source_root)
    output_plan = plan_initial_html_tree_output(root, request)
    result = {
        "schema": "literate-ai/retained-tree-html-plan@1",
        "action": "initialize",
        "root": str(root),
        "declaration": declaration.to_dict(),
        "resolved_request": request.to_dict(),
        "renderer_identity": renderer.to_dict(),
        "framework_distribution_identity": (
            framework.framework_distribution_identity.to_dict()
        ),
        "schema_catalog_release": framework.schema_catalog_release,
        "source_node": source_node,
        "output_plan": output_plan,
    }
    if (
        _framework() != framework
        or _source_node(root, request.source_root) != source_node
        or plan_initial_html_tree_output(root, request) != output_plan
    ):
        raise HtmlTreeRenderError(
            "render.tree.plan_changed",
            "The source root, renderer or output changed while planning.",
        )
    return result
