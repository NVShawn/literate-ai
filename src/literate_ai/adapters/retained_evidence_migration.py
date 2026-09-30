"""Migrate 0.9.0-shaped retained-harness evidence to the 0.11.0 schema.

Projects converted and Phase 1-qualified under Literate AI 0.9.0 recorded two
evidence documents in a shape the 0.11.0 retained-harness receipt front door no
longer accepts:

- ``.literate/legacy-wrapper-parity.json`` stayed at schema
  ``literate-ai/legacy-wrapper-parity@1`` forever, because nothing re-ran the
  legacy harness after it once passed. 0.11.0's ``_require_qualified_parity``
  requires ``literate-ai/legacy-wrapper-parity@2``.
- ``.literate/harness-inventory.json`` never recorded a
  ``literate-ai/retained-source-scope@1`` ``source_scope``, because that field
  did not exist until 0.9.0's parity pipeline started threading it through.
  0.11.0's retained-authority observation and ``run-retained`` both require it.

Neither file is a template file ``litai update``'s three-way classifier
understands (see ``project_updates.py``), so this migration is a distinct,
narrow step: it only ever rewrites a recorded ``schema`` string or backfills a
missing derived field. It never re-executes the legacy harness and never
invents evidence it cannot derive.

The source scope is reconstructed from the retained implementation directory
recorded by lift-and-shift evidence (``.literate/legacy-lift-shift.json``),
using the same ``capture_retained_source_scope`` the 0.9.0+ parity pipeline
already uses to self-heal a missing scope (see
``execute_harness_wrapper_parity`` in ``harness_inventory.py``). If lift-shift
evidence is absent or unsafe, the inventory is left untouched rather than
guessing: a blind capture of the current framework-shaped project is not a
safe substitute for the exact pre-lift-shift legacy tree (issue #342).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any

from literate_ai.contracts import canonical_json_bytes
from literate_ai.projects import ProjectConfigurationStore, ProjectError

from .harness_inventory import HARNESS_PARITY_SCHEMA
from .harness_tree import SOURCE_SCOPE_SCHEMA, capture_retained_source_scope

LEGACY_WRAPPER_PARITY_SCHEMA_V1 = "literate-ai/legacy-wrapper-parity@1"

MIGRATION_RESULT_SCHEMA = "literate-ai/retained-evidence-migration@1"

_PARITY_PATH = ".literate/legacy-wrapper-parity.json"
_INVENTORY_PATH = ".literate/harness-inventory.json"
_LIFT_SHIFT_PATH = ".literate/legacy-lift-shift.json"


@dataclass(frozen=True, slots=True)
class AppliedRetainedEvidenceMigration:
    """Which retained-harness evidence documents this migration rewrote."""

    migrated: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": MIGRATION_RESULT_SCHEMA,
            "migrated": list(self.migrated),
        }


def migrate_legacy_wrapper_parity_document(
    document: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Upgrade a qualified v1 parity document's schema to ``@2``.

    This never pretends to re-execute the legacy harness: a document that
    hasn't already reached ``state == "passed"`` is left alone, because there
    is nothing for a schema-only migration to safely say about it.
    """

    if not isinstance(document, dict):
        return document, False
    if document.get("schema") != LEGACY_WRAPPER_PARITY_SCHEMA_V1:
        return document, False
    if document.get("state") != "passed":
        return document, False
    migrated = dict(document)
    migrated["schema"] = HARNESS_PARITY_SCHEMA
    return migrated, True


def backfill_harness_inventory_source_scope(
    inventory: dict[str, Any], implementation_root: Path
) -> tuple[dict[str, Any], bool]:
    """Populate a missing ``source_scope`` from the retained implementation."""

    if not isinstance(inventory, dict):
        return inventory, False
    existing = inventory.get("source_scope")
    if isinstance(existing, dict) and existing.get("schema") == SOURCE_SCOPE_SCHEMA:
        return inventory, False
    migrated = dict(inventory)
    migrated["source_scope"] = capture_retained_source_scope(implementation_root)
    return migrated, True


def _resolve_implementation_root(
    project_root: Path, lift_shift_document: dict[str, Any]
) -> Path | None:
    """Resolve lift-shift's recorded implementation directory, fail closed."""

    raw = lift_shift_document.get("implementation_directory")
    if not isinstance(raw, str):
        return None
    relative = PurePosixPath(raw)
    if (
        relative.is_absolute()
        or relative.as_posix() != raw
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        return None
    candidate = project_root.joinpath(*relative.parts)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(project_root.resolve())
    except (OSError, ValueError):
        return None
    if candidate.is_symlink() or not resolved.is_dir():
        return None
    return resolved


def _read_json_document(path: Path) -> dict[str, Any] | None:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _rebind_receipt_policy_and_review(
    project_root: Path, migrated_inventory: dict[str, Any]
) -> None:
    """Refresh the receipt-policy identity and review after inventory changes.

    Populating ``source_scope`` changes the inventory's content identity, which
    ``retained_harness_receipt_policy`` folds into the bound runner identity
    (issue #342's expected behavior item 3). A policy bound against the
    pre-migration inventory would otherwise immediately refuse the next
    ``run-retained`` with ``retained_receipt.policy_mismatch``. Reviewed
    projects with no bound receipt policy yet, or whose policy was never a
    retained-harness policy to begin with, are left untouched — this is not
    the place to originate one.
    """

    # Imported lazily: project_initialization and retained_harness_receipts
    # are large modules whose own import graphs are unrelated to this narrow
    # rebind step, and neither needs to load eagerly for callers that only
    # migrate documents.
    from .project_initialization import record_project_authority_review
    from .retained_harness_receipts import (
        RETAINED_HARNESS_SUITE_ID,
        retained_harness_receipt_policy,
    )

    try:
        store = ProjectConfigurationStore(project_root)
        snapshot = store.read()
    except ProjectError:
        return
    current_policy = snapshot.definition.test_receipt_policy
    if current_policy is None:
        return
    if current_policy.suite_id != RETAINED_HARNESS_SUITE_ID:
        return
    expected_policy = retained_harness_receipt_policy(migrated_inventory)
    if current_policy == expected_policy:
        return
    updated_definition = replace(
        snapshot.definition, test_receipt_policy=expected_policy
    )
    try:
        store.update(snapshot, updated_definition)
        record_project_authority_review(project_root)
    except ProjectError:
        return


def migrate_retained_evidence(project_root: Path) -> AppliedRetainedEvidenceMigration:
    """Migrate any 0.9.0-shaped retained-harness evidence found in a project.

    A no-op for projects that never converted a legacy harness: both evidence
    files are simply absent. Each document is migrated independently, so a
    project missing lift-shift evidence still gets its parity schema bumped
    even though its source scope cannot be safely reconstructed.
    """

    migrated: list[str] = []

    parity_path = project_root / _PARITY_PATH
    if parity_path.is_file():
        document = _read_json_document(parity_path)
        if document is not None:
            updated, changed = migrate_legacy_wrapper_parity_document(document)
            if changed:
                parity_path.write_bytes(canonical_json_bytes(updated) + b"\n")
                migrated.append(_PARITY_PATH)

    inventory_path = project_root / _INVENTORY_PATH
    lift_shift_path = project_root / _LIFT_SHIFT_PATH
    if inventory_path.is_file() and lift_shift_path.is_file():
        inventory_document = _read_json_document(inventory_path)
        lift_shift_document = _read_json_document(lift_shift_path)
        if inventory_document is not None and lift_shift_document is not None:
            implementation_root = _resolve_implementation_root(
                project_root, lift_shift_document
            )
            if implementation_root is not None:
                updated, changed = backfill_harness_inventory_source_scope(
                    inventory_document, implementation_root
                )
                if changed:
                    inventory_path.write_bytes(canonical_json_bytes(updated) + b"\n")
                    migrated.append(_INVENTORY_PATH)
                    _rebind_receipt_policy_and_review(project_root, updated)

    return AppliedRetainedEvidenceMigration(migrated=tuple(migrated))
