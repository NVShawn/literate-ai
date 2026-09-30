"""Exact, non-mutating orchestration plans over independent child repositories."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from literate_ai._filesystem import UnsafeFilesystemPathError
from literate_ai.contracts.identity import canonical_identity
from literate_ai.contracts.repository_orchestration import (
    RepositoryOrchestration,
    RepositoryPin,
    RepositoryRelationship,
)

from .repository_orchestration import (
    OrchestrationInventoryError,
    _read_document,
    inspect_gitlink_inventory,
)

DECLARATION_SCHEMA = "literate-ai/orchestration@1"
PLAN_SCHEMA = "literate-ai/orchestration-plan@1"
_IDENTITY = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _fail(suffix: str, message: str) -> None:
    raise OrchestrationInventoryError(suffix, message)


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in pairs:
        if name in result:
            _fail("declaration_invalid", "duplicate orchestration declaration field")
        result[name] = value
    return result


def _declaration(path: Path) -> tuple[bytes, list[dict[str, str]]]:
    try:
        raw = _read_document(
            path, label="orchestration declaration", error_suffix="declaration_invalid"
        )
        value = json.loads(raw, object_pairs_hook=_unique)
    except OrchestrationInventoryError:
        raise
    except (OSError, ValueError, RecursionError, UnsafeFilesystemPathError) as exc:
        raise OrchestrationInventoryError(
            "declaration_invalid", "orchestration declaration is unavailable or invalid"
        ) from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "relationships"}
        or value["schema"] != DECLARATION_SCHEMA
        or not isinstance(value["relationships"], list)
        or len(value["relationships"]) > 1024
    ):
        _fail(
            "declaration_invalid",
            "declaration requires its schema and up to 1024 relationships",
        )
    relationships: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for edge in value["relationships"]:
        if (
            not isinstance(edge, dict)
            or set(edge) != {"consumer", "provider"}
            or any(not isinstance(item, str) or not item for item in edge.values())
        ):
            _fail(
                "declaration_invalid",
                "relationships require consumer and provider paths",
            )
        pair = (edge["consumer"], edge["provider"])
        if pair[0] == pair[1] or pair in seen:
            _fail(
                "declaration_invalid",
                "duplicate and self relationships are not admitted",
            )
        seen.add(pair)
        relationships.append(edge)
    return raw, sorted(
        relationships, key=lambda edge: (edge["consumer"], edge["provider"])
    )


def plan_orchestration(root: Path, declaration: Path) -> dict[str, Any]:
    """Bind all Gitlinks and explicit dependency declarations; never initialize."""
    root = root.absolute()
    declaration = declaration.absolute()
    raw, relationships = _declaration(declaration)
    inventory = inspect_gitlink_inventory(root)
    paths = {child.path for child in inventory.children}
    if not paths:
        _fail(
            "gitlinks_required",
            "orchestration requires at least one indexed child Gitlink",
        )
    if any(
        edge[role] not in paths
        for edge in relationships
        for role in ("consumer", "provider")
    ):
        _fail(
            "relationship_unknown",
            "every relationship must name exact inventoried Gitlink paths",
        )
    try:
        authority = RepositoryOrchestration(
            inventory.gitmodules_identity,
            tuple(
                RepositoryPin(
                    child.name, child.path, child.url, child.commit, child.branch
                )
                for child in inventory.children
            ),
            tuple(RepositoryRelationship.from_dict(edge) for edge in relationships),
        )
    except (TypeError, ValueError) as exc:
        raise OrchestrationInventoryError(
            "authority_invalid", "inventory cannot form canonical repository authority"
        ) from exc
    plan = {
        "schema": PLAN_SCHEMA,
        "profile": "git-submodule-orchestration",
        "declaration_identity": "sha256:" + hashlib.sha256(raw).hexdigest(),
        "inventory": inventory.to_dict(),
        "inventory_identity": inventory.identity,
        "relationships": relationships,
        "repository_authority": authority.to_dict(),
        "repository_authority_identity": authority.identity,
        "writes": False,
        "execution": False,
        "apply_supported": False,
        "initialization_performed": False,
        "child_authority": "independent",
        "publication": "not-checked",
    }
    # Include declaration custody in the same reobservation window as the Git pins.
    if (
        _declaration(declaration)[0] != raw
        or inspect_gitlink_inventory(root) != inventory
        or _declaration(declaration)[0] != raw
    ):
        _fail("inputs_changed", "orchestration inputs changed during planning")
    return {**plan, "plan_identity": canonical_identity(plan).uri}


def check_orchestration(
    root: Path, declaration: Path, *, expected_plan_identity: str
) -> dict[str, Any]:
    """Recompute an exact reviewed plan, not merely check a saved JSON envelope."""
    if (
        not isinstance(expected_plan_identity, str)
        or _IDENTITY.fullmatch(expected_plan_identity) is None
    ):
        _fail("plan_identity_invalid", "check requires an exact sha256 plan identity")
    plan = plan_orchestration(root, declaration)
    if plan["plan_identity"] != expected_plan_identity:
        _fail(
            "plan_stale", "reviewed orchestration plan no longer matches current inputs"
        )
    return {
        "schema": "literate-ai/orchestration-check@1",
        "state": "current",
        "plan_identity": expected_plan_identity,
        "writes": False,
        "execution": False,
        "apply_supported": False,
        "publication": "not-checked",
    }
