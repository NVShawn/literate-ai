"""Reviewed replacement of exact, still-unqualified registered promotions."""

from __future__ import annotations

import base64
import json
import os
import stat
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path

from literate_ai._cache_lock import CacheLockError, exclusive_cache_lock
from literate_ai._filesystem import ensure_safe_directory, stat_is_link_or_reparse
from literate_ai.adapters.authority import (
    AuthorityProjectionStoreError,
    FileAuthorityProjectionStore,
)
from literate_ai.adapters.conversion_authority import (
    CONVERSION_AUTHORITY_FILE,
    NATIVE_PROJECTS_FILE,
    ConversionAuthorityError,
    FilesystemConversionAuthorityStore,
    _native_project_roots,
    require_native_project_path,
)
from literate_ai.adapters.native_project_updates import (
    _capture,
    _digest,
    _read,
    _RegisteredChild,
    _replace_metadata,
)
from literate_ai.contracts import canonical_identity, canonical_json_bytes
from literate_ai.contracts.authority import (
    ComponentAuthorityProjection,
    ComponentAuthorityState,
)
from literate_ai.contracts.operator_adoption import ConversionAuthorityStage
from literate_ai.projects import ProjectError, load_project, parse_project_configuration
from literate_ai.source_to_specification.promotion_materialization import (
    SourcePromotionError,
    verify_source_promotion_evidence,
)

REPLACEMENT_PLAN_SCHEMA = "literate-ai/native-child-replacement-plan@1"
_MAXIMUM_FILES = 4096
_MAXIMUM_FILE_BYTES = 64 * 1024 * 1024
_MAXIMUM_TREE_BYTES = 256 * 1024 * 1024


class NativeChildReplacementError(SourcePromotionError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__("promotion.native_child_" + code, message)


def _refuse(code: str, message: str) -> None:
    raise NativeChildReplacementError(code, message)


def _tree(root: Path) -> dict[str, dict[str, object]]:
    """Bound every retained node, including generated and otherwise ignored files."""
    metadata = root.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or stat_is_link_or_reparse(metadata):
        _refuse("unsafe_tree", "replacement requires a direct child directory")
    entries: dict[str, dict[str, object]] = {}
    total = 0
    for current, directories, filenames in os.walk(root, followlinks=False):
        for name in sorted((*directories, *filenames)):
            path = Path(current) / name
            metadata = path.lstat()
            if stat_is_link_or_reparse(metadata):
                _refuse(
                    "unsafe_tree", "replacement cannot follow links or reparse points"
                )
            relative = path.relative_to(root).as_posix()
            mode = stat.S_IMODE(metadata.st_mode)
            if stat.S_ISDIR(metadata.st_mode):
                entries[relative] = {"kind": "directory", "mode": mode}
            elif stat.S_ISREG(metadata.st_mode):
                if metadata.st_size > _MAXIMUM_FILE_BYTES:
                    _refuse("tree_limit", "a retained file exceeds the supported limit")
                content = _read(path, maximum_bytes=_MAXIMUM_FILE_BYTES)
                total += len(content)
                entries[relative] = {
                    "kind": "file",
                    "mode": mode,
                    "bytes": len(content),
                    "identity": _digest(content),
                }
            else:
                _refuse(
                    "unsafe_tree",
                    "replacement requires direct regular files and directories",
                )
            if len(entries) > _MAXIMUM_FILES or total > _MAXIMUM_TREE_BYTES:
                _refuse(
                    "tree_limit", "retained child exceeds the supported recovery limit"
                )
    return dict(sorted(entries.items()))


def _copy_tree(
    source: Path, destination: Path, entries: Mapping[str, Mapping[str, object]]
) -> None:
    destination.mkdir()
    for relative, entry in entries.items():
        target = destination / relative
        if entry["kind"] == "directory":
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            content = _read(source / relative, maximum_bytes=_MAXIMUM_FILE_BYTES)
            if _digest(content) != entry["identity"]:
                _refuse("changed", "child changed while retaining recovery evidence")
            target.write_bytes(content)
        # Apply directory modes last so read-only parent directories remain copyable.
        if entry["kind"] == "file":
            target.chmod(entry["mode"])
    for relative, entry in reversed(tuple(entries.items())):
        if entry["kind"] == "directory":
            (destination / relative).chmod(entry["mode"])


def _unqualified(root: Path) -> tuple[ComponentAuthorityProjection, ...]:
    store = FileAuthorityProjectionStore(root)
    coordinates = store.coordinates()
    if not coordinates:
        _refuse("promotion_missing", "registered child has no source promotion")
    projections = []
    for coordinate in coordinates:
        history = store.history(coordinate)
        current = history[-1]
        if current.state is not ComponentAuthorityState.DERIVED_SOURCE_RETAINED or any(
            p.state is ComponentAuthorityState.REGENERATIVELY_QUALIFIED_FUNGIBLE
            for p in history
        ):
            _refuse(
                "qualified", "only an unqualified source-derived child can be replaced"
            )
        projections.append(history[-1])
    return tuple(projections)


def _historical_manifest(
    captured: _RegisteredChild, current: bytes, records: list[tuple[str, dict]]
) -> tuple[bytes, str] | None:
    """Permit only an exact recorded Standard rebind when reading original audits."""
    from dataclasses import replace

    from literate_ai.application.standard_lifecycle_rebind import _validated_plan
    from literate_ai.contracts import (
        ProjectTestReceiptPolicy,
        StandardProjectLifecycleDriver,
    )
    from literate_ai.projects import serialize_project_configuration

    for identity, record in records:
        if (
            set(record) != {"schema", "plan", "before", "after"}
            or record["schema"] != "literate-ai/registered-child-rebind-recovery@1"
        ):
            continue
        plan = record["plan"]
        if canonical_identity(
            {k: v for k, v in plan.items() if k != "identity"}
        ).uri != plan.get("identity"):
            continue
        context = plan.get("registration", {})
        if (
            context.get("parent_project_id") != captured.parent.definition.project_id
            or context.get("child_path") != captured.context["child_path"]
        ):
            continue
        before = base64.b64decode(
            record["before"]["child_manifest_base64"], validate=True
        )
        after = base64.b64decode(
            record["after"]["child_manifest_base64"], validate=True
        )
        if after != current or _digest(before) != context.get(
            "child_configuration_identity"
        ):
            continue
        binding = _validated_plan(plan["rebind"])
        expected = serialize_project_configuration(
            replace(
                parse_project_configuration(before),
                lifecycle_driver=StandardProjectLifecycleDriver.from_dict(
                    binding["proposed_binding"]
                ),
                test_receipt_policy=ProjectTestReceiptPolicy.from_dict(
                    binding["proposed_receipt_policy"]
                ),
            )
        )
        if after == expected:
            return before, identity
    return None


def _promotion_custody(captured: _RegisteredChild, tree: dict) -> dict[str, object]:
    projections = _unqualified(captured.child)
    records: list[tuple[str, dict]] = []
    history_root = captured.parent.root / ".literate/native-updates"
    require_native_project_path(captured.parent, history_root)
    for path in sorted(history_root.glob("*.json"))[:65]:
        if len(records) >= 64:
            _refuse("history_limit", "too many retained rebind records")
        raw = _read(path, maximum_bytes=8 * 1024 * 1024)
        records.append((_digest(raw), json.loads(raw)))
    transitions: list[str] = []
    # Audit verification occurs in an inert retained copy. Never rewrite the live
    # manifest or grant current qualification from a historical promotion audit.
    with tempfile.TemporaryDirectory(prefix="litai-nr-") as temporary:
        view = Path(temporary) / "old"
        _copy_tree(captured.child, view, tree)
        candidate = captured.manifest
        seen = set()
        while True:
            try:
                for projection in projections:
                    verify_source_promotion_evidence(view, projection)
                break
            except SourcePromotionError:
                if _digest(candidate) in seen:
                    raise
                seen.add(_digest(candidate))
                previous = _historical_manifest(captured, candidate, records)
                if previous is None:
                    raise
                candidate, identity = previous
                transitions.append(identity)
                (view / "literate.project.json").write_bytes(candidate)
    return {
        "projections": [p.identity.uri for p in projections],
        "historical_rebind_records": transitions,
    }


def _plan_native_child_replacement(
    parent: Path, child: Path, request: Mapping[str, object]
) -> dict[str, object]:
    captured = _capture(parent, child)
    if (
        request.get("project_id")
        != parse_project_configuration(captured.manifest).project_id
    ):
        _refuse(
            "project_id", "replacement must preserve the registered project identifier"
        )
    state = FilesystemConversionAuthorityStore(captured.parent.root).load_optional()
    if state is None or state.stage not in {
        ConversionAuthorityStage.RETAINED,
        ConversionAuthorityStage.DRAFTED,
    }:
        _refuse(
            "parent_stage", "replacement requires retained or drafted parent authority"
        )
    state_bytes = _read(captured.parent.root / CONVERSION_AUTHORITY_FILE)
    tree = _tree(captured.child)
    custody = _promotion_custody(captured, tree)
    if (
        _capture(parent, child).context != captured.context
        or _tree(captured.child) != tree
        or _read(captured.parent.root / CONVERSION_AUTHORITY_FILE) != state_bytes
    ):
        _refuse("changed", "registered authority changed while replacement was planned")
    plan: dict[str, object] = {
        "schema": REPLACEMENT_PLAN_SCHEMA,
        "registration": captured.context,
        "conversion_authority_identity": _digest(state_bytes),
        "prior_tree": tree,
        "promotion": custody,
        "request": dict(request),
    }
    plan["identity"] = canonical_identity(plan).uri
    return plan


def _apply_native_child_replacement(
    parent: Path,
    child: Path,
    plan: Mapping[str, object],
    request: Mapping[str, object],
    *,
    create: Callable[[Path], dict[str, object]],
    source_guard: Callable[[], None],
) -> dict[str, object]:
    if plan.get("schema") != REPLACEMENT_PLAN_SCHEMA or canonical_identity(
        {k: v for k, v in plan.items() if k != "identity"}
    ).uri != plan.get("identity"):
        _refuse("plan_invalid", "replacement plan schema or identity is invalid")
    captured = _capture(parent, child)
    registry_path = captured.parent.root / NATIVE_PROJECTS_FILE
    with (
        exclusive_cache_lock(registry_path.with_suffix(".lock")),
        exclusive_cache_lock(
            captured.parent.root / ".literate/conversion-authority.lock"
        ),
    ):
        current = plan_native_child_replacement(parent, child, request)
        if current != plan:
            _refuse("changed", "reviewed replacement inputs are no longer current")
        source_guard()
        captured = _capture(parent, child)
        base = captured.parent.root / ".literate/native-updates"
        require_native_project_path(captured.parent, base)
        ensure_safe_directory(base)
        recovery = Path(tempfile.mkdtemp(prefix="r-", dir=base))
        old = recovery / "old"
        new = recovery / "new"
        before_registry = captured.registry
        updated_registry: bytes | None = None
        installed_tree: dict | None = None
        moved = False
        installed = False
        try:
            result = create(new)
            if (
                load_project(new).definition.project_id
                != load_project(child).definition.project_id
            ):
                _refuse(
                    "project_id",
                    "replacement must preserve the registered project identifier",
                )
            _unqualified(new)
            installed_tree = _tree(new)
            # Recheck signed source and all old authority after materialization.
            source_guard()
            if plan_native_child_replacement(parent, child, request) != plan:
                _refuse(
                    "changed",
                    "registered authority changed during replacement creation",
                )
            record = {
                "schema": "literate-ai/native-child-replacement-recovery@1",
                "plan": dict(plan),
                "registry_base64": base64.b64encode(before_registry).decode("ascii"),
                "parent_manifest_base64": base64.b64encode(
                    captured.parent_manifest
                ).decode("ascii"),
            }
            (recovery / "record.json").write_bytes(canonical_json_bytes(record) + b"\n")
            require_native_project_path(captured.parent, child)
            require_native_project_path(captured.parent, old)
            os.rename(child, old)
            moved = True
            if _tree(old) != plan["prior_tree"]:
                _refuse("changed", "old promotion changed at the custody boundary")
            _unqualified(old)
            require_native_project_path(captured.parent, new)
            require_native_project_path(captured.parent, child)
            os.rename(new, child)
            installed = True
            registry = json.loads(before_registry)
            for entry in registry["projects"]:
                if entry["path"] == captured.context["child_path"]:
                    entry["project_definition_identity"] = canonical_identity(
                        json.loads(_read(child / "literate.project.json"))
                    ).uri
            updated_registry = canonical_json_bytes(registry) + b"\n"
            if (
                _read(captured.parent.root / "literate.project.json")
                != captured.parent_manifest
            ):
                _refuse("changed", "parent manifest changed during replacement")
            _replace_metadata(
                captured.parent, registry_path, before_registry, updated_registry
            )
            source_guard()
            _native_project_roots(captured.parent)
            if _tree(child) != installed_tree or _tree(old) != plan["prior_tree"]:
                _refuse("changed", "promotion changed before replacement committed")
            completion = {
                "schema": "literate-ai/native-child-replacement-completion@1",
                "plan_identity": plan["identity"],
                "recovery_identity": canonical_identity(record).uri,
                "registry_identity": _digest(updated_registry),
            }
            (recovery / "completed.json").write_bytes(
                canonical_json_bytes(completion) + b"\n"
            )
        except BaseException:
            try:
                for path in (child, recovery, registry_path):
                    require_native_project_path(captured.parent, path)
                if installed:
                    if _tree(child) != installed_tree:
                        _refuse(
                            "rollback_conflict",
                            "child changed; rollback recovery is retained",
                        )
                    os.rename(child, recovery / "failed")
                if moved:
                    if child.exists() or child.is_symlink():
                        _refuse(
                            "rollback_conflict",
                            "child location changed; recovery is retained",
                        )
                    os.rename(old, child)
                observed = _read(registry_path)
                if observed != before_registry:
                    if updated_registry is None or observed != updated_registry:
                        _refuse(
                            "rollback_conflict",
                            "registry changed; rollback recovery is retained",
                        )
                    _replace_metadata(
                        captured.parent,
                        registry_path,
                        updated_registry,
                        before_registry,
                    )
            except NativeChildReplacementError:
                raise
            except (ConversionAuthorityError, OSError, ValueError) as exc:
                raise NativeChildReplacementError(
                    "rollback_conflict",
                    "rollback failed; prior promotion and recovery are retained",
                ) from exc
            raise
        return {
            **result,
            "project_target": str(child),
            "integrated_project": str(captured.parent.root),
            "replacement": {
                "plan_identity": plan["identity"],
                "recovery_path": recovery.relative_to(captured.parent.root).as_posix(),
                "recovery_identity": canonical_identity(record).uri,
            },
        }


_METADATA_ERRORS = (
    AuthorityProjectionStoreError,
    CacheLockError,
    ConversionAuthorityError,
    ProjectError,
    OSError,
    ValueError,
    TypeError,
    KeyError,
)


def plan_native_child_replacement(
    parent: Path, child: Path, request: Mapping[str, object]
) -> dict[str, object]:
    try:
        return _plan_native_child_replacement(parent, child, request)
    except SourcePromotionError:
        raise
    except _METADATA_ERRORS as exc:
        raise NativeChildReplacementError(
            "metadata_invalid",
            "registered-child replacement metadata is unavailable, unsafe or changed",
        ) from exc


def apply_native_child_replacement(
    parent: Path,
    child: Path,
    plan: Mapping[str, object],
    request: Mapping[str, object],
    *,
    create: Callable[[Path], dict[str, object]],
    source_guard: Callable[[], None],
) -> dict[str, object]:
    try:
        return _apply_native_child_replacement(
            parent, child, plan, request, create=create, source_guard=source_guard
        )
    except SourcePromotionError:
        raise
    except _METADATA_ERRORS as exc:
        raise NativeChildReplacementError(
            "metadata_invalid",
            "registered-child replacement failed; recovery evidence is retained",
        ) from exc
