"""Reviewed identity migration for original-source converted projects.

The authored project definition supplies the new name. One atomic conversion
state publication commits the migration after its immutable lineage is durable.
Historical producer records and receipts are never changed by this operation.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import replace
from pathlib import Path
from typing import Any

from literate_ai.adapters.conversion_authority import (
    CONVERSION_AUTHORITY_FILE,
    FilesystemConversionAuthorityStore,
)
from literate_ai.adapters.harness_tree import (
    _validated_scope,
    capture_retained_source_scope,
)
from literate_ai.adapters.lifecycle_lock import project_lifecycle_lock
from literate_ai.adapters.project_validation import FilesystemProjectValidationAdapter
from literate_ai.adapters.retained_harness_receipts import (
    _implementation_root,
    _load_retained_documents,
    _require_qualified_parity,
)
from literate_ai.adapters.retained_scope_refresh import _members, _safe_path
from literate_ai.contracts import (
    ContentIdentity,
    canonical_identity,
    canonical_json_bytes,
)
from literate_ai.contracts.operator_adoption import (
    ConversionAuthorityStage,
    ConversionAuthorityState,
)
from literate_ai.projects import LoadedProject, load_project

HISTORY = ".literate/project-identity-migrations"
PLAN_SCHEMA = "literate-ai/project-identity-migration-plan@1"
RECORD_SCHEMA = "literate-ai/project-identity-migration@1"


class ProjectIdentityMigrationError(ValueError):
    def __init__(self, suffix: str, message: str) -> None:
        self.code = "project_identity." + suffix
        self.message = message
        super().__init__(message)


def _fail(suffix: str, message: str) -> None:
    raise ProjectIdentityMigrationError(suffix, message)


def _history_path(root: Path, identity: ContentIdentity) -> Path:
    return _safe_path(root, f"{HISTORY}/{identity.digest}.json")


def _read_record(root: Path, identity: ContentIdentity) -> dict[str, Any]:
    path = _history_path(root, identity)
    try:
        fd = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_BINARY", 0),
        )
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= 262144:
                raise ValueError
            content = os.read(fd, 262145)
            after = os.fstat(fd)
            named = path.lstat()

            def stamp(s):
                return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)

            if (
                len(content) != before.st_size
                or stamp(before) != stamp(after)
                or stamp(before) != stamp(named)
            ):
                raise ValueError
        finally:
            os.close(fd)
        record = json.loads(content)
        if (
            content != canonical_json_bytes(record) + b"\n"
            or canonical_identity(record) != identity
        ):
            raise ValueError
        if set(record) != {
            "schema",
            "previous_state",
            "from_project_id",
            "to_project_id",
            "project_definition_identity",
            "source_identity",
            "historical_evidence",
            "authority_identity",
        }:
            raise ValueError
        if record["schema"] != RECORD_SCHEMA:
            raise ValueError
        previous = ConversionAuthorityState.from_dict(record["previous_state"])
        if (
            previous.project_id != record["from_project_id"]
            or previous.project_id == record["to_project_id"]
        ):
            raise ValueError
        if previous.release_authority != "original-source":
            raise ValueError
        for key in (
            "project_definition_identity",
            "source_identity",
            "authority_identity",
        ):
            ContentIdentity.parse_uri(record[key])
        if (
            not isinstance(record["historical_evidence"], dict)
            or not record["historical_evidence"]
        ):
            raise ValueError
        for item in record["historical_evidence"].values():
            ContentIdentity.parse_uri(item)
        return record
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ProjectIdentityMigrationError(
            "history_invalid",
            "identity migration history is missing, unsafe or changed",
        ) from exc


def validate_identity_migrations(root: Path, state: ConversionAuthorityState) -> None:
    """Validate the finite immutable rename chain on every conversion-state read."""
    seen: set[str] = set()
    while state.identity_migration_identity is not None:
        identity = state.identity_migration_identity
        if identity.uri in seen or len(seen) >= 128:
            _fail(
                "history_invalid",
                "identity migration history is cyclic or exceeds its bound",
            )
        seen.add(identity.uri)
        record = _read_record(root, identity)
        if record["to_project_id"] != state.project_id:
            _fail("history_invalid", "identity migration history names another project")
        state = ConversionAuthorityState.from_dict(record["previous_state"])


def _capture_record(
    project: LoadedProject, state: ConversionAuthorityState
) -> dict[str, Any]:
    root = project.root
    documents = _load_retained_documents(project)
    _require_qualified_parity(documents)
    implementation = _implementation_root(project, documents)
    _safe_path(root, implementation.relative_to(root).as_posix())
    inventory = documents["inventory"]["value"]
    scope = capture_retained_source_scope(
        implementation,
        required_paths=tuple(stage["evidence"] for stage in inventory["stages"]),
    )
    paths, _ = _validated_scope(scope)
    members = _members(implementation, paths)
    review = FilesystemProjectValidationAdapter().documentation_review(root)
    authority_identity = review.get("authority_identity")
    if not isinstance(authority_identity, str):
        _fail(
            "authority_unavailable", "the current project authority must be inspectable"
        )
    return {
        "schema": RECORD_SCHEMA,
        "previous_state": state.to_dict(),
        "from_project_id": state.project_id,
        "to_project_id": project.definition.project_id,
        "project_definition_identity": "sha256:"
        + hashlib.sha256((root / "literate.project.json").read_bytes()).hexdigest(),
        "source_identity": canonical_identity({"scope": scope, "members": members}).uri,
        "historical_evidence": {
            name: data["identity"] for name, data in documents.items()
        },
        "authority_identity": authority_identity,
    }


def plan_project_identity_migration(
    selected: Path, *, from_project_id: str
) -> dict[str, Any]:
    """Plan recovery of an explicitly authored new ID; never infer the old ID."""
    project = load_project(selected)
    root = project.root
    _safe_path(root, "literate.project.json")
    _safe_path(root, CONVERSION_AUTHORITY_FILE)
    _safe_path(root, HISTORY)
    state = FilesystemConversionAuthorityStore(root).load_optional()
    if state is None or state.project_id != from_project_id:
        _fail(
            "previous_project_mismatch",
            "the explicit previous project ID must match conversion authority",
        )
    if state.project_id == project.definition.project_id:
        _fail("unchanged", "author a distinct new project ID before planning migration")
    if state.stage not in {
        ConversionAuthorityStage.WRAPPED,
        ConversionAuthorityStage.RETAINED,
        ConversionAuthorityStage.DRAFTED,
    }:
        _fail(
            "stage_unsupported",
            "identity migration requires original-source converted authority",
        )
    record = _capture_record(project, state)
    identity = canonical_identity(record)
    replacement = replace(
        state,
        project_id=project.definition.project_id,
        prior_state_identity=None
        if state.stage is ConversionAuthorityStage.WRAPPED
        else state.identity,
        identity_migration_identity=identity,
    )
    history = _history_path(root, identity)
    if history.exists():
        _read_record(root, identity)
    plan = {
        "schema": PLAN_SCHEMA,
        "project": str(root),
        "record": record,
        "record_identity": identity.uri,
        "conversion_identity": state.identity.uri,
        "new_conversion_state": replacement.to_dict(),
        "write_set": [history.relative_to(root).as_posix(), CONVERSION_AUTHORITY_FILE],
        "execution_performed": False,
        "requalification_required": True,
    }
    plan["plan_identity"] = canonical_identity(plan).uri
    return plan


def apply_project_identity_migration(
    selected: Path,
    *,
    from_project_id: str,
    expected_plan_identity: str,
    acknowledge: bool,
) -> dict[str, Any]:
    if acknowledge is not True:
        _fail("acknowledgement_required", "identity migration requires acknowledgement")
    project = load_project(selected)
    with project_lifecycle_lock(project.root, operation="project.identity.migrate"):
        plan = plan_project_identity_migration(
            project.root, from_project_id=from_project_id
        )
        if plan["plan_identity"] != expected_plan_identity:
            _fail(
                "plan_stale",
                "source or authority changed after review; inspect a new plan",
            )
        identity = ContentIdentity.parse_uri(plan["record_identity"])
        history = _history_path(project.root, identity)
        history.parent.mkdir(parents=True, exist_ok=True)
        if not history.exists():
            try:
                with history.open("xb") as stream:
                    stream.write(canonical_json_bytes(plan["record"]) + b"\n")
                    stream.flush()
                    os.fsync(stream.fileno())
            except FileExistsError:
                pass
        _read_record(project.root, identity)
        # Recheck the full input closure after history publication, before the
        # one state mutation. Interrupted history-only attempts earn no authority.
        if (
            plan_project_identity_migration(
                project.root, from_project_id=from_project_id
            )["plan_identity"]
            != expected_plan_identity
        ):
            _fail("plan_stale", "source or authority changed before publication")
        store = FilesystemConversionAuthorityStore(project.root)
        previous = ConversionAuthorityState.from_dict(plan["record"]["previous_state"])
        replacement = ConversionAuthorityState.from_dict(plan["new_conversion_state"])
        store._replace(replacement, expected=previous.identity)
        try:
            # A concurrent authored change after the commit remains a refusal,
            # never an assertion of qualification for unobserved contents.
            current = load_project(project.root)
            if _capture_record(current, previous) != plan["record"]:
                _fail(
                    "source_changed",
                    "source or authority changed during identity publication",
                )
            store.load_optional()
        except BaseException:
            store._replace(previous, expected=replacement.identity)
            raise
        return {
            **plan,
            "applied": True,
            "history": history.relative_to(project.root).as_posix(),
        }
