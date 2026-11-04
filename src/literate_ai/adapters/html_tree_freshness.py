"""Check actual published tree data and independently reobserve current inputs.

Checking creates no caches, output, temporary files or execution/publication
owners. The existing complete no-follow source capture is used in hash-only
mode; historical descriptors cannot substitute for either current source pass.
"""

from __future__ import annotations

from pathlib import Path

from literate_ai.contracts.html_trees import HtmlTreeViewDeclaration

from .html_tree_published_output import observe_published_html_tree_output
from .html_tree_render import (
    HtmlTreeRenderError,
    _framework,
    _renderer_identity,
    _source_node,
)
from .native_sdk_tree_custody import _capture_composed_native_tree
from .retained_project_inputs import _root


def check_published_html_tree(root, declaration, *, require_declaration_current):
    """Return current facts only after complete output and source closing checks.

    The checks are temporal guards, not an atomic snapshot of unrelated source,
    renderer and output trees. Neither recognition nor this result authorizes a
    later write, recovery, collection or exclusion outside the actual context.
    """
    if not isinstance(declaration, HtmlTreeViewDeclaration) or not callable(
        require_declaration_current
    ):
        raise TypeError("html.tree.typed-freshness-context-required")
    require_declaration_current()
    root = _root(Path(root))
    framework = _framework()
    renderer = _renderer_identity(framework)
    request = declaration.resolve(renderer)
    source = _root(root / request.source_root)
    source_node = _source_node(root, request.source_root)
    limits = request.limits

    def authority_current():
        require_declaration_current()
        if (
            _framework() != framework
            or _source_node(root, request.source_root) != source_node
        ):
            raise HtmlTreeRenderError(
                "html.tree.freshness-authority-changed",
                "The source root or installed renderer changed during checking.",
            )

    with observe_published_html_tree_output(root, request) as output:
        observed = closing = None
        try:

            def capture(reserved):
                authority_current()
                tree = _capture_composed_native_tree(
                    source,
                    store=None,
                    limits=limits.source,
                    case_sensitive=request.case_sensitive,
                    max_descriptor_bytes=limits.max_descriptor_bytes,
                    max_descriptor_total_bytes=limits.max_descriptor_total_bytes,
                    max_index_bytes=limits.max_index_bytes,
                    max_metadata_memory_bytes=limits.max_metadata_memory_bytes,
                    output_custody=output,
                    reserved_memory_bytes=reserved,
                    budget_capture=True,
                    retain_payloads=False,
                )
                authority_current()
                return tree

            observed = capture(output.retained_memory_bytes)
            if observed.identity != output.tree.identity:
                raise HtmlTreeRenderError(
                    "html.tree.source-stale",
                    "The current complete source tree differs from the published view.",
                )
            output.require_current(
                reserved_memory_bytes=observed.index.metadata_memory_bound
            )
            # Both source observations remain charged while the independent final
            # capture closes source membership, payload and alias currentness.
            closing = capture(
                output.retained_memory_bytes + observed.index.metadata_memory_bound
            )
            if closing.identity != observed.identity:
                raise HtmlTreeRenderError(
                    "html.tree.source-stale",
                    "The complete source tree changed while checking "
                    "its published view.",
                )
            output.require_current(
                reserved_memory_bytes=(
                    observed.index.metadata_memory_bound
                    + closing.index.metadata_memory_bound
                )
            )
            authority_current()
            result = {
                "schema": "literate-ai/retained-tree-html-freshness@1",
                "current": True,
                "request_identity": request.identity.uri,
                "renderer_identity": renderer.uri,
                "framework_distribution_identity": (
                    framework.framework_distribution_identity.uri
                ),
                "generation_manifest": output.staged.manifest.to_dict(),
                "source_identity": closing.identity.uri,
                "source_member_count": closing.index.member_count,
                "directory_count": len(closing.directories) + 1,
                "page_count": output.staged.page_count,
                "page_bytes": output.staged.page_bytes,
                "writes_authorized": False,
            }
        finally:
            # Source graphs are no longer needed by any output closing guard.
            # Clear them on successful and refused observations alike.
            observed = closing = None
    return result
