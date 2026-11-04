"""Thin planning and initial-apply CLI over actual installed tree owners."""

from __future__ import annotations

import json
import re
from pathlib import Path

from literate_ai._cache_lock import CacheLockError
from literate_ai._filesystem import UnsafeFilesystemPathError
from literate_ai.adapters.html_tree_apply import apply_initial_html_tree
from literate_ai.adapters.html_tree_render import (
    HtmlTreeRenderError,
    plan_html_tree_render,
)
from literate_ai.adapters.user_paths import resolve_user_paths
from literate_ai.cache_directories import CacheDirectoryError
from literate_ai.contracts.html_trees import HtmlTreeViewDeclaration
from literate_ai.contracts.identity import ContentIdentity, canonical_identity
from literate_ai.projects import PinnedInputClosure, ProjectError
from literate_ai.storage import StorageError

_MAXIMUM_DECLARATION_BYTES = 65536


def _pairs(values):
    result = {}
    for name, value in values:
        if name in result:
            raise ValueError("tree declaration contains duplicate JSON fields")
        result[name] = value
    return result


def _constant(_value):
    raise ValueError("tree declaration contains a non-JSON numeric constant")


def plan_tree_from_args(args):
    """Default to a read-only plan; apply only an exact fresh acknowledgement."""
    apply = bool(getattr(args, "apply", False))
    schema = (
        "literate-ai/retained-tree-html-apply-result@1"
        if apply
        else "literate-ai/retained-tree-html-plan-result@1"
    )
    try:
        acknowledgement = getattr(args, "ack_plan", None)
        if apply and acknowledgement is None:
            raise ValueError("html.tree.explicit-plan-acknowledgement-required")
        if not apply and acknowledgement is not None:
            raise ValueError("html.tree.acknowledgement-requires-apply")
        acknowledged_identity = (
            ContentIdentity.parse_uri(acknowledgement) if apply else None
        )
        source = Path(args.declaration).absolute()
        inputs = PinnedInputClosure(
            maximum_files=1,
            maximum_file_bytes=_MAXIMUM_DECLARATION_BYTES,
            maximum_total_bytes=_MAXIMUM_DECLARATION_BYTES,
        )
        raw = inputs.pin(source, boundary=source.parent, label="tree-html-declaration")
        declaration = HtmlTreeViewDeclaration.from_dict(
            json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)
        )
        if apply:
            publication = apply_initial_html_tree(
                Path(args.root),
                declaration,
                acknowledged_plan_identity=acknowledged_identity,
                state_root=Path(resolve_user_paths().state_root),
                require_declaration_current=inputs.require_unchanged,
            )
            inputs.require_unchanged()
            return {
                "schema": schema,
                "status": "published",
                "publication": publication,
            }, 0
        plan = plan_html_tree_render(Path(args.root), declaration)
        inputs.require_unchanged()
        return {
            "schema": schema,
            "status": "planned",
            "plan": plan,
            "plan_identity": canonical_identity(plan).uri,
        }, 0
    except HtmlTreeRenderError as exc:
        code, message = exc.code, str(exc)
    except CacheLockError:
        code = "html.tree.publication-lock-unavailable"
        message = "The selected initial publication target could not be locked safely."
    except CacheDirectoryError:
        code = "html.tree.private-state-unavailable"
        message = "The private managed publication state could not be verified."
    except StorageError:
        code = "html.tree.retained-store-unavailable"
        message = "The retained tree generation store could not be verified."
    except (
        OSError,
        ValueError,
        TypeError,
        ProjectError,
        UnsafeFilesystemPathError,
        RecursionError,
    ) as exc:
        code = (
            "render.tree.apply_unavailable" if apply else "render.tree.plan_unavailable"
        )
        detail = str(exc)
        if re.fullmatch(r"(?:html\.tree|tree\.shards)\.[A-Za-z0-9_.-]{1,160}", detail):
            # Stable typed policy failures name the actual budget/selection;
            # arbitrary exception text, host paths and input values stay private.
            code = detail
        message = (
            "The declaration, source root, installed renderer or initial output "
            "coordinates could not be verified."
        )
    return {
        "schema": schema,
        "status": "refused",
        "refusal": {"code": code, "message": message},
    }, 1
