"""Read only explicitly declared project tree views under pinned input custody."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path

from literate_ai.contracts.html_tree_projects import (
    MAX_PROJECT_TREE_VIEWS,
    PROJECT_TREE_HTML_FILENAME,
    ProjectHtmlTreeViews,
)
from literate_ai.contracts.html_trees import HtmlTreeViewDeclaration
from literate_ai.projects import PinnedInputClosure

from .html_tree_output_initialization import _node
from .retained_project_inputs import _root

_MAX_DECLARATION_BYTES = 65536


def project_tree_views_present(root):
    """A dangling or foreign registry is present and must refuse, never skip."""
    return os.path.lexists(Path(root) / PROJECT_TREE_HTML_FILENAME)


def _pairs(values):
    result = {}
    for key, value in values:
        if key in result:
            raise ValueError("html.tree.project-duplicate-json-field")
        result[key] = value
    return result


def _constant(_value):
    raise ValueError("html.tree.project-non-json-number")


def _decode(raw):
    return json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)


@contextmanager
def observe_project_tree_views(project):
    """Hold actual bounded registry/declaration bytes through all tree checks.

    The registry does not change ProjectDefinition or the frozen single-file
    protocol. Declaration inputs remain subject to project pinned-input limits;
    each view separately owns its source/output rendering limits. No filesystem
    discovery supplies an implicit list of requests or source exclusions.
    """
    root = _root(project.root)
    node = _node(root.lstat())

    def root_current():
        if _node(_root(root).lstat()) != node:
            raise ValueError("html.tree.project-root-changed")

    if not project_tree_views_present(root):

        def absent_current():
            root_current()
            if project_tree_views_present(root):
                raise ValueError("html.tree.project-declarations-changed")

        yield (), absent_current
        absent_current()
        return
    policy = PinnedInputClosure.for_project(project)
    maximum_total = min(
        (MAX_PROJECT_TREE_VIEWS + 1) * _MAX_DECLARATION_BYTES,
        policy.maximum_total_bytes,
    )
    inputs = PinnedInputClosure(
        maximum_files=min(MAX_PROJECT_TREE_VIEWS + 1, policy.maximum_files),
        maximum_file_bytes=min(
            _MAX_DECLARATION_BYTES, policy.maximum_file_bytes, maximum_total
        ),
        maximum_total_bytes=maximum_total,
    )
    registry = ProjectHtmlTreeViews.from_dict(
        _decode(
            inputs.pin(
                root / PROJECT_TREE_HTML_FILENAME,
                boundary=root,
                label="project-tree-html-registry",
            )
        )
    )
    views = []
    outputs = set()
    for ordinal, name in enumerate(registry.declarations):
        declaration = HtmlTreeViewDeclaration.from_dict(
            _decode(
                inputs.pin(
                    root / name,
                    boundary=root,
                    label="project-tree-html-declaration-" + str(ordinal),
                )
            )
        )
        output = declaration.output_root.casefold()
        if output in outputs:
            raise ValueError("html.tree.project-output-index-collision")
        outputs.add(output)
        views.append(declaration)

    def current():
        root_current()
        inputs.require_unchanged()
        root_current()

    current()
    yield tuple(views), current
    current()
