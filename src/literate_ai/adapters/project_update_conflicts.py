"""Display-only ours/base/theirs evidence for ``litai update`` conflicts.

Classification already names conflicts. Operators and coding agents still need the
three blobs. This module never writes markers into the project tree.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from literate_ai.adapters.project_updates import (
    _identity,
    _local_bytes,
    _upstream_template,
)
from literate_ai.adapters.repository_updates import (
    PlannedRepositoryLineageUpdate,
    _prospective_content,
)
from literate_ai.contracts import (
    ProjectUpdateClassification,
    ProjectUpdatePlan,
)

CONFLICT_DIFF_SCHEMA = "literate-ai/project-update-conflict-diff@1"
_BINARY_PLACEHOLDER = "(binary content omitted)\n"


def _decode_side(content: bytes | None) -> str | None:
    if content is None:
        return None
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return _BINARY_PLACEHOLDER
    if "\0" in text:
        return _BINARY_PLACEHOLDER
    return text


def _identity_dict(content: bytes | None) -> dict[str, str] | None:
    if content is None:
        return None
    identity = _identity(content)
    return identity.to_dict()


def _conflict_markers(
    path: str,
    *,
    ours: str | None,
    base: str | None,
    theirs: str | None,
) -> str:
    """Render a display-only diff3 conflict. Nothing is written to disk."""

    lines = [f"--- {path}", f"+++ {path}"]
    if ours is None and theirs is None and base is None:
        lines.append("(no recoverable sides)")
        return "\n".join(lines) + "\n"
    lines.append("<<<<<<< ours")
    if ours is not None:
        lines.extend(ours.splitlines())
    if base is not None:
        lines.append("||||||| base")
        lines.extend(base.splitlines())
    lines.append("=======")
    if theirs is not None:
        lines.extend(theirs.splitlines())
    lines.append(">>>>>>> theirs")
    return "\n".join(lines) + "\n"


def conflict_diff_document(
    path: str,
    *,
    ours: bytes | None,
    base: bytes | None,
    theirs: bytes | None,
) -> dict[str, Any]:
    ours_text = _decode_side(ours)
    base_text = _decode_side(base)
    theirs_text = _decode_side(theirs)
    unified = _conflict_markers(
        path, ours=ours_text, base=base_text, theirs=theirs_text
    )
    return {
        "schema": CONFLICT_DIFF_SCHEMA,
        "path": path,
        "ours": ours_text,
        "base": base_text,
        "theirs": theirs_text,
        "ours_identity": _identity_dict(ours),
        "base_identity": _identity_dict(base),
        "theirs_identity": _identity_dict(theirs),
        "unified_diff": unified,
    }


def framework_conflict_diffs(
    root: Path, plan: ProjectUpdatePlan
) -> list[dict[str, Any]]:
    """Attach local and template bytes to each framework-template conflict."""

    baseline_paths = {
        item.path for item in plan.files if item.baseline_identity is not None
    }
    upstream = _upstream_template(baseline_paths)
    diffs: list[dict[str, Any]] = []
    for item in plan.files:
        if item.classification is not ProjectUpdateClassification.CONFLICT:
            continue
        diffs.append(
            conflict_diff_document(
                item.path,
                ours=_local_bytes(root, item.path),
                base=None,
                theirs=upstream.get(item.path),
            )
        )
    return diffs


def lineage_conflict_diffs(
    root: Path, planned: PlannedRepositoryLineageUpdate
) -> list[dict[str, Any]]:
    """Attach local and prospective catalog bytes to each inherited conflict."""

    upstream = _prospective_content(planned.catalogs)
    diffs: list[dict[str, Any]] = []
    for item in planned.contract.files:
        if item.classification is not ProjectUpdateClassification.CONFLICT:
            continue
        inherited = upstream.get(item.path)
        diffs.append(
            conflict_diff_document(
                item.path,
                ours=_local_bytes(root, item.path),
                base=None,
                theirs=None if inherited is None else inherited.content,
            )
        )
    return diffs


def enrich_conflict_files(
    files: list[dict[str, Any]], diffs: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Copy display-only sides onto conflict file records in the JSON envelope."""

    by_path = {item["path"]: item for item in diffs}
    enriched: list[dict[str, Any]] = []
    for item in files:
        extra = by_path.get(item.get("path"))
        if item.get("classification") != "conflict" or extra is None:
            enriched.append(item)
            continue
        enriched.append(
            {
                **item,
                "ours": extra["ours"],
                "base": extra["base"],
                "theirs": extra["theirs"],
                "unified_diff": extra["unified_diff"],
            }
        )
    return enriched


__all__ = [
    "CONFLICT_DIFF_SCHEMA",
    "conflict_diff_document",
    "enrich_conflict_files",
    "framework_conflict_diffs",
    "lineage_conflict_diffs",
]
