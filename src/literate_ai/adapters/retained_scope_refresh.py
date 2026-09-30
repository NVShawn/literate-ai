"""Reviewed source-scope maintenance; conversion evidence stays historical."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any

from literate_ai._filesystem import stat_is_link_or_reparse
from literate_ai.adapters.conversion_authority import (
    CONVERSION_AUTHORITY_FILE,
    FilesystemConversionAuthorityStore,
)
from literate_ai.adapters.harness_tree import (
    _validated_scope,
    capture_retained_source_scope,
)
from literate_ai.adapters.lifecycle_lock import project_lifecycle_lock
from literate_ai.adapters.project_initialization import record_project_authority_review
from literate_ai.adapters.project_validation import FilesystemProjectValidationAdapter
from literate_ai.adapters.retained_harness_receipts import (
    RETAINED_HARNESS_SUITE_ID,
    _implementation_root,
    _load_retained_documents,
    _require_qualified_parity,
    retained_harness_receipt_policy,
)
from literate_ai.application.project_authority import AUTHORITY_REVIEW_MARKER
from literate_ai.contracts import canonical_identity, canonical_json_bytes
from literate_ai.contracts.operator_adoption import ConversionAuthorityStage
from literate_ai.projects import ProjectConfigurationStore, load_project

PLAN_SCHEMA = "literate-ai/retained-scope-refresh-plan@1"
INVENTORY = ".literate/harness-inventory.json"
HISTORY = ".literate/retained-scope-refresh"
_PRESERVED_METADATA = (
    CONVERSION_AUTHORITY_FILE,
    ".literate/legacy-harness-baseline.json",
    ".literate/legacy-wrapper-parity.json",
    ".literate/legacy-lift-shift.json",
    "litai.harness.mk",
)


class RetainedScopeRefreshError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code, self.message = code, message
        super().__init__(message)


def _fail(suffix: str, message: str) -> None:
    raise RetainedScopeRefreshError(f"retained_scope.{suffix}", message)


def _safe_path(root: Path, relative: str) -> Path:
    path = PurePosixPath(relative)
    if (
        path.is_absolute()
        or path.as_posix() != relative
        or ".." in path.parts
        or "\\" in relative
    ):
        _fail("unsafe_path", "refresh paths must be canonical project-relative paths")
    current = root
    for part in path.parts:
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        if stat_is_link_or_reparse(metadata):
            _fail(
                "unsafe_path",
                f"refresh refuses indirect filesystem custody: {relative}",
            )
    if not current.resolve().is_relative_to(root):
        _fail("unsafe_path", "refresh path escapes project custody")
    return current


def _identity(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _members(root: Path, paths: tuple[str, ...]) -> list[dict[str, Any]]:
    result = []
    for relative in paths:
        path = _safe_path(root, relative)
        try:
            before = path.stat()
        except FileNotFoundError:
            result.append({"path": relative, "kind": "missing"})
            continue
        if not stat.S_ISREG(before.st_mode):
            _fail("unsafe_path", "retained source members must be regular files")
        content = path.read_bytes()
        after = path.stat()
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_mode,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_mode,
        ):
            _fail("source_changed", "source changed while planning; retry the plan")
        result.append(
            {
                "path": relative,
                "kind": "file",
                "identity": _identity(content),
                "executable": bool(before.st_mode & 0o111),
            }
        )
    return result


def plan_retained_scope_refresh(selected: Path) -> dict[str, Any]:
    """Inspect exact old/new scope without running commands or writing files."""

    project = load_project(selected)
    root = project.root
    monorepo_refresh_required = False
    monorepo_path = root / ".literate" / "monorepo-components" / "installation.json"
    if monorepo_path.exists() or monorepo_path.is_symlink():
        from literate_ai.adapters.monorepo_adoption import MonorepoAdoptionError
        from literate_ai.adapters.monorepo_components import (
            check_installed_monorepo_components,
        )

        try:
            check_installed_monorepo_components(root)
        except MonorepoAdoptionError:
            monorepo_refresh_required = True
    for relative in (
        INVENTORY,
        CONVERSION_AUTHORITY_FILE,
        HISTORY,
        ".literate/legacy-harness-baseline.json",
        ".literate/legacy-wrapper-parity.json",
        ".literate/legacy-lift-shift.json",
        "litai.harness.mk",
    ):
        _safe_path(root, relative)
    state = FilesystemConversionAuthorityStore(root).load_optional()
    if state is None or state.project_id != project.definition.project_id:
        _fail(
            "not_adopted", "refresh requires the adopted project's conversion authority"
        )
    if state.stage is ConversionAuthorityStage.QUALIFIED:
        _fail(
            "source_not_authoritative",
            "refresh cannot change specification-authoritative source",
        )
    documents = _load_retained_documents(project)
    _require_qualified_parity(documents)
    implementation = _implementation_root(project, documents)
    _safe_path(root, implementation.relative_to(root).as_posix())
    inventory = documents["inventory"]["value"]
    old_paths, _ = _validated_scope(inventory.get("source_scope"))
    new_scope = capture_retained_source_scope(
        implementation,
        required_paths=tuple(stage["evidence"] for stage in inventory["stages"]),
    )
    new_paths, _ = _validated_scope(new_scope)
    members = _members(implementation, tuple(sorted(set(old_paths) | set(new_paths))))
    new_inventory = {**inventory, "source_scope": new_scope}
    policy = project.definition.test_receipt_policy
    if policy is None or policy.suite_id != RETAINED_HARNESS_SUITE_ID:
        _fail(
            "policy_unsupported",
            "refresh requires an existing retained-harness receipt policy",
        )
    expected_policy = retained_harness_receipt_policy(new_inventory)
    review = FilesystemProjectValidationAdapter().documentation_review(root)
    document = review.get("document")
    if review.get("state") not in {"current", "stale"} or not isinstance(document, str):
        _fail(
            "review_unavailable",
            "refresh requires one existing authority review document",
        )
    review_path = _safe_path(root, document)
    value = {
        "schema": PLAN_SCHEMA,
        "project": str(root),
        "project_id": project.definition.project_id,
        "project_identity": _identity((root / "literate.project.json").read_bytes()),
        "authority_review": review,
        "review_document_identity": _identity(review_path.read_bytes()),
        "conversion_identity": state.identity.uri,
        "implementation": implementation.relative_to(root).as_posix(),
        "old_inventory": inventory,
        "new_inventory": new_inventory,
        "historical_evidence": {
            name: data["identity"] for name, data in documents.items()
        },
        "preserved_metadata": {
            relative: _identity(_safe_path(root, relative).read_bytes())
            for relative in _PRESERVED_METADATA
        },
        "source_members": members,
        "added": sorted(set(new_paths) - set(old_paths)),
        "removed": sorted(set(old_paths) - set(new_paths)),
        "new_receipt_policy": expected_policy.to_dict(),
        "changes_required": inventory != new_inventory
        or policy != expected_policy
        or review["state"] == "stale"
        or monorepo_refresh_required,
        "monorepo_refresh_required": monorepo_refresh_required,
        "commands_changed": False,
        "execution_performed": False,
        "requalification_required": True,
    }
    value["plan_identity"] = canonical_identity(value).uri
    return value


def apply_retained_scope_refresh(
    selected: Path,
    *,
    expected_plan_identity: str,
    acknowledge: bool,
    run_component_baselines: bool = False,
) -> dict[str, Any]:
    """Revalidate under the lifecycle lock; retain receipts but make them stale."""

    if acknowledge is not True:
        _fail(
            "acknowledgement_required",
            "scope and policy mutation requires acknowledgement",
        )
    project = load_project(selected)
    with project_lifecycle_lock(project.root, operation="retained-scope.refresh"):
        plan = plan_retained_scope_refresh(project.root)
        if plan["plan_identity"] != expected_plan_identity:
            _fail(
                "plan_stale",
                "source or metadata changed after review; inspect a fresh plan",
            )
        if plan["monorepo_refresh_required"] and not run_component_baselines:
            _fail(
                "component_execution_required",
                "refined monorepo refresh requires --run-component-baselines",
            )
        if not plan["changes_required"]:
            return {**plan, "applied": False}
        root = project.root
        inventory_path = _safe_path(root, INVENTORY)
        review_path = _safe_path(root, plan["authority_review"]["document"])
        manifest_path = root / "literate.project.json"
        before = {
            path: path.read_bytes()
            for path in (inventory_path, manifest_path, review_path)
        }
        store = ProjectConfigurationStore(root)
        snapshot = store.read()
        if (
            _identity(snapshot.content) != plan["project_identity"]
            or _identity(before[review_path]) != plan["review_document_identity"]
        ):
            _fail(
                "plan_stale", "project metadata changed before publication; plan again"
            )
        history = _safe_path(
            root, f"{HISTORY}/{expected_plan_identity.removeprefix('sha256:')}.json"
        )
        history.parent.mkdir(parents=True, exist_ok=True)
        history_bytes = canonical_json_bytes(plan) + b"\n"
        created_history = False
        if history.exists():
            if history.read_bytes() != history_bytes:
                _fail(
                    "history_collision", "retained refresh history has different bytes"
                )
        else:
            with history.open("xb") as stream:
                stream.write(history_bytes)
                stream.flush()
                os.fsync(stream.fileno())
            created_history = True
        written: dict[Path, bytes] = {}
        try:
            content = canonical_json_bytes(plan["new_inventory"]) + b"\n"
            FilesystemConversionAuthorityStore._atomic_write(inventory_path, content)
            written[inventory_path] = content
            updated = store.update(
                snapshot,
                replace(
                    snapshot.definition,
                    test_receipt_policy=retained_harness_receipt_policy(
                        plan["new_inventory"]
                    ),
                ),
            )
            written[manifest_path] = updated.content
            monorepo_refresh = None
            if plan["monorepo_refresh_required"]:
                from literate_ai.adapters.monorepo_components import (
                    refresh_installed_monorepo_components,
                )

                monorepo_refresh = refresh_installed_monorepo_components(
                    root,
                    acknowledged=True,
                )
            review = FilesystemProjectValidationAdapter().documentation_review(root)
            written[review_path] = AUTHORITY_REVIEW_MARKER.sub(
                review["expected_marker"].encode("utf-8"), before[review_path], count=1
            )
            record_project_authority_review(root)
            if any(
                _identity(_safe_path(root, relative).read_bytes()) != identity
                for relative, identity in plan["preserved_metadata"].items()
            ):
                _fail("metadata_changed", "historical metadata changed during refresh")
            implementation = root / plan["implementation"]
            current_scope = capture_retained_source_scope(
                implementation,
                required_paths=tuple(
                    stage["evidence"] for stage in plan["new_inventory"]["stages"]
                ),
            )
            if (
                current_scope != plan["new_inventory"]["source_scope"]
                or _members(
                    implementation,
                    tuple(item["path"] for item in plan["source_members"]),
                )
                != plan["source_members"]
            ):
                _fail(
                    "source_changed",
                    "source changed during refresh; no new evidence was earned",
                )
        except BaseException:
            # Ordinary failures restore only bytes still owned by this operation.
            # An abrupt process exit leaves policy/review mismatches fail-closed;
            # a fresh acknowledged plan can reconcile that partial state.
            for path, content in reversed(tuple(written.items())):
                if path.read_bytes() == content:
                    FilesystemConversionAuthorityStore._atomic_write(path, before[path])
            if created_history and history.read_bytes() == history_bytes:
                history.unlink()
            raise
        return {
            **plan,
            "applied": True,
            "history": history.relative_to(root).as_posix(),
            "monorepo_refresh": monorepo_refresh,
        }
