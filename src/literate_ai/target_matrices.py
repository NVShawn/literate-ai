"""Canonical concurrent target-matrix contracts and execution."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from literate_ai.adapters._processes import run_with_tree_kill
from literate_ai.contracts.identity import (
    ContentIdentity,
    canonical_json_bytes,
    contract_identity,
)
from literate_ai.diagnostics import redact_secrets

_PORTABLE_TARGET = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")
_COMPONENT = re.compile(
    r"^(?:components|samples)/[a-z0-9](?:[a-z0-9._/-]{0,253}[a-z0-9])?$"
)
_STAGES = ("plan", "lock", "source-admission", "lifecycle")


class TargetMatrixError(RuntimeError):
    """A declaration, evidence chain, or matrix execution is invalid."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _identity(value: object, path: str) -> ContentIdentity:
    if isinstance(value, ContentIdentity):
        return value
    if isinstance(value, str):
        try:
            return ContentIdentity.parse_uri(value)
        except ValueError as exc:
            raise TargetMatrixError(
                "matrix.identity_invalid", f"{path} is invalid"
            ) from exc
    raise TargetMatrixError("matrix.identity_invalid", f"{path} must be an identity")


@dataclass(frozen=True, slots=True)
class TargetMatrixCell:
    """One Component, target, and precedence-preserving Flavor selection."""

    component: str
    target: str
    flavors: tuple[str, ...] = ()

    SCHEMA: ClassVar[str] = "literate-ai/target-matrix-cell@1"

    def __post_init__(self) -> None:
        if (
            not _COMPONENT.fullmatch(self.component)
            or ".." in Path(self.component).parts
        ):
            raise TargetMatrixError(
                "matrix.component_unsafe",
                "matrix cell component must be a safe project-relative "
                "components/ or samples/ path",
            )
        if not _PORTABLE_TARGET.fullmatch(self.target):
            raise TargetMatrixError(
                "matrix.target_unsafe", "matrix cell target must be a portable selector"
            )
        if len(self.flavors) > 64 or any(
            not isinstance(value, str)
            or not value
            or len(value) > 512
            or "\x00" in value
            for value in self.flavors
        ):
            raise TargetMatrixError(
                "matrix.flavors_invalid",
                "matrix cell Flavors must be an ordered list of bounded selectors",
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "component": self.component,
            "target": self.target,
            "flavors": list(self.flavors),
        }

    @classmethod
    def from_dict(cls, value: object) -> TargetMatrixCell:
        if not isinstance(value, dict) or set(value) != {
            "schema",
            "component",
            "target",
            "flavors",
        }:
            raise TargetMatrixError(
                "matrix.cell_invalid", "matrix cell fields are invalid"
            )
        if value["schema"] != cls.SCHEMA or not isinstance(value["flavors"], list):
            raise TargetMatrixError(
                "matrix.cell_invalid", "matrix cell schema is invalid"
            )
        if not isinstance(value["component"], str) or not isinstance(
            value["target"], str
        ):
            raise TargetMatrixError(
                "matrix.cell_invalid", "matrix cell selectors must be strings"
            )
        return cls(
            value["component"],
            value["target"],
            tuple(value["flavors"]),
        )


@dataclass(frozen=True, slots=True)
class TargetMatrixDeclaration:
    """An exact set of target cells, canonically ordered by cell identity."""

    cells: tuple[TargetMatrixCell, ...]

    SCHEMA: ClassVar[str] = "literate-ai/target-matrix@1"

    def __post_init__(self) -> None:
        if not self.cells or len(self.cells) > 256:
            raise TargetMatrixError(
                "matrix.cells_invalid", "matrix must contain between 1 and 256 cells"
            )
        identities = tuple(cell.identity.uri for cell in self.cells)
        if identities != tuple(sorted(identities)) or len(set(identities)) != len(
            identities
        ):
            raise TargetMatrixError(
                "matrix.cells_noncanonical",
                "matrix cells must be unique and ordered by deterministic identity",
            )

    @classmethod
    def create(cls, cells: tuple[TargetMatrixCell, ...]) -> TargetMatrixDeclaration:
        return cls(tuple(sorted(cells, key=lambda cell: cell.identity.uri)))

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {"schema": self.SCHEMA, "cells": [cell.to_dict() for cell in self.cells]}

    @classmethod
    def from_dict(cls, value: object) -> TargetMatrixDeclaration:
        if (
            not isinstance(value, dict)
            or set(value) != {"schema", "cells"}
            or value["schema"] != cls.SCHEMA
            or not isinstance(value["cells"], list)
        ):
            raise TargetMatrixError(
                "matrix.declaration_invalid", "matrix declaration is invalid"
            )
        return cls.create(
            tuple(TargetMatrixCell.from_dict(item) for item in value["cells"])
        )


@dataclass(frozen=True, slots=True)
class TargetMatrixStageEvidence:
    """Accepted evidence from one cell and its exact predecessor."""

    cell_identity: ContentIdentity
    stage: str
    evidence_identity: ContentIdentity
    predecessor_identity: ContentIdentity | None
    accepted: bool

    SCHEMA: ClassVar[str] = "literate-ai/target-matrix-stage-evidence@1"

    def __post_init__(self) -> None:
        if self.stage not in _STAGES:
            raise TargetMatrixError(
                "matrix.stage_invalid", "matrix stage is not canonical"
            )

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "cell_identity": self.cell_identity.uri,
            "stage": self.stage,
            "evidence_identity": self.evidence_identity.uri,
            "predecessor_identity": (
                None
                if self.predecessor_identity is None
                else self.predecessor_identity.uri
            ),
            "accepted": self.accepted,
        }

    @classmethod
    def from_dict(cls, value: object) -> TargetMatrixStageEvidence:
        required = {
            "schema",
            "cell_identity",
            "stage",
            "evidence_identity",
            "predecessor_identity",
            "accepted",
        }
        if (
            not isinstance(value, dict)
            or set(value) != required
            or value["schema"] != cls.SCHEMA
        ):
            raise TargetMatrixError(
                "matrix.stage_invalid", "matrix stage evidence is invalid"
            )
        if not isinstance(value["stage"], str) or not isinstance(
            value["accepted"], bool
        ):
            raise TargetMatrixError(
                "matrix.stage_invalid", "matrix stage and acceptance are invalid"
            )
        predecessor = value["predecessor_identity"]
        return cls(
            _identity(value["cell_identity"], "stage cell identity"),
            value["stage"],
            _identity(value["evidence_identity"], "stage evidence identity"),
            None
            if predecessor is None
            else _identity(predecessor, "stage predecessor"),
            value["accepted"],
        )


@dataclass(frozen=True, slots=True)
class TargetMatrixCellReceipt:
    """Canonical complete or partial result for one exact target cell."""

    cell: TargetMatrixCell
    status: str
    component_authority_identity: ContentIdentity | None
    plan_identity: ContentIdentity | None
    lock_identity: ContentIdentity | None
    source_admission_identity: ContentIdentity | None
    stages: tuple[TargetMatrixStageEvidence, ...]
    error: str | None = None

    SCHEMA: ClassVar[str] = "literate-ai/target-matrix-cell-receipt@1"

    def __post_init__(self) -> None:
        if self.status not in {"passed", "failed", "partial"}:
            raise TargetMatrixError("matrix.status_invalid", "cell status is invalid")
        for item in self.stages:
            if item.cell_identity != self.cell.identity:
                raise TargetMatrixError(
                    "matrix.cross_cell_evidence",
                    "cell receipt contains evidence from another cell",
                )
        if self.status == "passed" and not self.accepted:
            raise TargetMatrixError(
                "matrix.receipt_incomplete",
                "a passing cell receipt must contain the complete accepted "
                "evidence chain",
            )

    @property
    def accepted(self) -> bool:
        identities = (
            self.component_authority_identity,
            self.plan_identity,
            self.lock_identity,
            self.source_admission_identity,
        )
        if self.status != "passed" or any(value is None for value in identities):
            return False
        if tuple(item.stage for item in self.stages) != _STAGES:
            return False
        predecessor = None
        for item in self.stages:
            if not item.accepted or item.predecessor_identity != predecessor:
                return False
            predecessor = item.identity
        return True

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        def uri(value: ContentIdentity | None) -> str | None:
            return None if value is None else value.uri

        return {
            "schema": self.SCHEMA,
            "cell": self.cell.to_dict(),
            "status": self.status,
            "component_authority_identity": uri(self.component_authority_identity),
            "plan_identity": uri(self.plan_identity),
            "lock_identity": uri(self.lock_identity),
            "source_admission_identity": uri(self.source_admission_identity),
            "stages": [item.to_dict() for item in self.stages],
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, value: object) -> TargetMatrixCellReceipt:
        required = {
            "schema",
            "cell",
            "status",
            "component_authority_identity",
            "plan_identity",
            "lock_identity",
            "source_admission_identity",
            "stages",
            "error",
        }
        if (
            not isinstance(value, dict)
            or set(value) != required
            or value["schema"] != cls.SCHEMA
        ):
            raise TargetMatrixError("matrix.receipt_invalid", "cell receipt is invalid")

        def optional(name: str) -> ContentIdentity | None:
            raw = value[name]
            return None if raw is None else _identity(raw, name)

        if not isinstance(value["stages"], list):
            raise TargetMatrixError(
                "matrix.receipt_invalid", "cell stages must be a list"
            )
        return cls(
            TargetMatrixCell.from_dict(value["cell"]),
            str(value["status"]),
            optional("component_authority_identity"),
            optional("plan_identity"),
            optional("lock_identity"),
            optional("source_admission_identity"),
            tuple(
                TargetMatrixStageEvidence.from_dict(item) for item in value["stages"]
            ),
            None if value["error"] is None else str(value["error"]),
        )


@dataclass(frozen=True, slots=True)
class TargetMatrixAggregateReceipt:
    """Receipt binding the exact matrix and every cell result."""

    matrix_identity: ContentIdentity
    cell_identities: tuple[ContentIdentity, ...]
    receipts: tuple[TargetMatrixCellReceipt, ...]
    status: str

    SCHEMA: ClassVar[str] = "literate-ai/target-matrix-aggregate-receipt@1"

    def __post_init__(self) -> None:
        if self.status not in {"passed", "failed", "partial"}:
            raise TargetMatrixError(
                "matrix.status_invalid", "aggregate status is invalid"
            )
        receipt_cells = tuple(item.cell.identity for item in self.receipts)
        if receipt_cells != self.cell_identities:
            raise TargetMatrixError(
                "matrix.aggregate_cell_mismatch",
                "aggregate receipts do not bind the exact declared cell order",
            )
        if self.status == "passed" and not self.accepted:
            raise TargetMatrixError(
                "matrix.aggregate_incomplete",
                "passing aggregate receipt requires every exact cell to be accepted",
            )

    @property
    def accepted(self) -> bool:
        return self.status == "passed" and all(item.accepted for item in self.receipts)

    @property
    def identity(self) -> ContentIdentity:
        return contract_identity(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.SCHEMA,
            "matrix_identity": self.matrix_identity.uri,
            "cell_identities": [item.uri for item in self.cell_identities],
            "receipt_identities": [item.identity.uri for item in self.receipts],
            "receipts": [item.to_dict() for item in self.receipts],
            "status": self.status,
            "accepted": self.accepted,
        }

    @classmethod
    def from_dict(cls, value: object) -> TargetMatrixAggregateReceipt:
        required = {
            "schema",
            "matrix_identity",
            "cell_identities",
            "receipt_identities",
            "receipts",
            "status",
            "accepted",
        }
        if (
            not isinstance(value, dict)
            or set(value) != required
            or value["schema"] != cls.SCHEMA
            or not isinstance(value["cell_identities"], list)
            or not isinstance(value["receipt_identities"], list)
            or not isinstance(value["receipts"], list)
            or not isinstance(value["accepted"], bool)
        ):
            raise TargetMatrixError(
                "matrix.aggregate_invalid", "aggregate receipt is invalid"
            )
        receipts = tuple(
            TargetMatrixCellReceipt.from_dict(item) for item in value["receipts"]
        )
        receipt_identities = tuple(
            _identity(item, "aggregate receipt identity")
            for item in value["receipt_identities"]
        )
        if receipt_identities != tuple(item.identity for item in receipts):
            raise TargetMatrixError(
                "matrix.aggregate_receipt_mismatch",
                "aggregate receipt identities do not match embedded receipts",
            )
        aggregate = cls(
            _identity(value["matrix_identity"], "aggregate matrix identity"),
            tuple(
                _identity(item, "aggregate cell identity")
                for item in value["cell_identities"]
            ),
            receipts,
            str(value["status"]),
        )
        if aggregate.accepted != value["accepted"]:
            raise TargetMatrixError(
                "matrix.aggregate_acceptance_forged",
                "aggregate accepted flag does not match exact cell receipts",
            )
        return aggregate


CellExecutor = Callable[[TargetMatrixCell, Path], Mapping[str, object]]


def _receipt_from_result(
    cell: TargetMatrixCell, result: Mapping[str, object]
) -> TargetMatrixCellReceipt:
    try:
        if (
            result["specification"] != cell.component
            or result["target"] != cell.target
            or result["explicit_flavor_selectors"] != list(cell.flavors)
        ):
            raise TargetMatrixError(
                "matrix.selection_mismatch",
                "cell lifecycle result does not bind the exact Component, target, "
                "and ordered Flavor selection",
            )
        component_authority = _identity(
            result["validated_project_authority_identity"], "component authority"
        )
        plan = _identity(result["lifecycle_request_identity"], "plan")
        raw_locks = result["component_lock_identities"]
        if not isinstance(raw_locks, list) or len(raw_locks) != 1:
            raise TargetMatrixError(
                "matrix.lock_scope_invalid",
                "a matrix cell must produce exactly one lock",
            )
        lock = _identity(raw_locks[0], "lock")
        source = _identity(
            result["source_cache_lifecycle_identity"], "source admission"
        )
        lifecycle = _identity(result["receipt_identity"], "lifecycle receipt")
        evidence_identities = (plan, lock, source, lifecycle)
        stages: list[TargetMatrixStageEvidence] = []
        predecessor = None
        for stage, evidence_identity in zip(_STAGES, evidence_identities, strict=True):
            evidence = TargetMatrixStageEvidence(
                cell.identity, stage, evidence_identity, predecessor, True
            )
            stages.append(evidence)
            predecessor = evidence.identity
        return TargetMatrixCellReceipt(
            cell,
            "passed",
            component_authority,
            plan,
            lock,
            source,
            tuple(stages),
        )
    except (KeyError, TypeError, ValueError, TargetMatrixError) as exc:
        if isinstance(exc, TargetMatrixError):
            raise
        raise TargetMatrixError(
            "matrix.lifecycle_result_invalid",
            "cell lifecycle result lacks canonical authority or evidence identities",
        ) from exc


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    content = canonical_json_bytes(value) + b"\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def run_target_matrix(
    declaration: TargetMatrixDeclaration,
    *,
    evidence_root: Path,
    executor: CellExecutor,
    jobs: int,
    reuse: bool = False,
    current_authority_identity: ContentIdentity | None = None,
) -> TargetMatrixAggregateReceipt:
    """Run cells concurrently and persist fail-closed cell and aggregate receipts."""

    if jobs < 1 or jobs > 256:
        raise TargetMatrixError(
            "matrix.jobs_invalid", "matrix jobs must be between 1 and 256"
        )
    root = evidence_root.resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    receipts: dict[str, TargetMatrixCellReceipt] = {}

    def execute(cell: TargetMatrixCell) -> TargetMatrixCellReceipt:
        cell_root = root / "cells" / cell.identity.digest
        cell_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if cell_root.is_symlink() or not cell_root.is_dir():
            raise TargetMatrixError(
                "matrix.cell_root_unsafe",
                "matrix cell root must be a direct directory",
            )
        receipt_path = cell_root / "receipt.json"
        if reuse and receipt_path.is_file() and not receipt_path.is_symlink():
            try:
                existing = TargetMatrixCellReceipt.from_dict(
                    json.loads(receipt_path.read_text(encoding="utf-8"))
                )
            except (OSError, UnicodeError, json.JSONDecodeError, TargetMatrixError):
                existing = None
            if (
                existing is not None
                and existing.cell == cell
                and existing.accepted
                and existing.component_authority_identity == current_authority_identity
            ):
                return existing
        try:
            result = executor(cell, cell_root)
            receipt = _receipt_from_result(cell, result)
        except Exception as exc:
            receipt = TargetMatrixCellReceipt(
                cell, "failed", None, None, None, None, (), str(exc)[:512]
            )
        _atomic_json(receipt_path, receipt.to_dict())
        return receipt

    with ThreadPoolExecutor(max_workers=min(jobs, len(declaration.cells))) as pool:
        futures = {pool.submit(execute, cell): cell for cell in declaration.cells}
        for future in as_completed(futures):
            receipt = future.result()
            receipts[receipt.cell.identity.uri] = receipt
    ordered = tuple(receipts[cell.identity.uri] for cell in declaration.cells)
    passed = sum(receipt.accepted for receipt in ordered)
    status = (
        "passed" if passed == len(ordered) else ("failed" if passed == 0 else "partial")
    )
    aggregate = TargetMatrixAggregateReceipt(
        declaration.identity,
        tuple(cell.identity for cell in declaration.cells),
        ordered,
        status,
    )
    _atomic_json(root / "aggregate-receipt.json", aggregate.to_dict())
    return aggregate


def subprocess_rebuild_executor(
    *,
    project: Path,
    allow_host_execution: bool,
    timeout_seconds: float = 3600.0,
) -> CellExecutor:
    """Build an executor that invokes the canonical rebuild lifecycle in-place."""

    project_root = project.resolve(strict=True)
    if timeout_seconds <= 0:
        raise TargetMatrixError(
            "matrix.timeout_invalid", "matrix cell timeout must be positive"
        )

    def execute(cell: TargetMatrixCell, cell_root: Path) -> Mapping[str, object]:
        with tempfile.TemporaryDirectory(prefix="litai-matrix-cell-") as temporary:
            temporary_root = Path(temporary)
            runtime_root = temporary_root / "runtime"
            candidate = temporary_root / "candidate-receipt.json"
            environment = dict(os.environ)
            environment["LITAI_MATRIX_CELL_ROOT"] = str(
                (cell_root / "scoped-state").resolve()
            )
            lock_command = [
                sys.executable,
                "-m",
                "literate_ai.cli",
                "--json",
                "lock",
                cell.component,
                "--target",
                cell.target,
            ]
            for flavor in cell.flavors:
                lock_command.extend(("--flavor", flavor))
            try:
                locked = run_with_tree_kill(
                    lock_command,
                    cwd=project_root,
                    env=environment,
                    text=True,
                    timeout=timeout_seconds,
                )
            except subprocess.TimeoutExpired as exc:
                raise TargetMatrixError(
                    "matrix.cell_timeout",
                    f"cell lock resolution exceeded {timeout_seconds:g} seconds",
                ) from exc
            if locked.returncode != 0:
                detail = locked.stderr or locked.stdout or "cell lock resolution failed"
                raise TargetMatrixError(
                    "matrix.cell_lock_failed",
                    redact_secrets(detail, environment)[:512],
                )
            try:
                lock_envelope = json.loads(locked.stdout)
            except json.JSONDecodeError as exc:
                raise TargetMatrixError(
                    "matrix.cell_lock_invalid",
                    "cell lock resolution returned invalid JSON",
                ) from exc
            if (
                not isinstance(lock_envelope, dict)
                or lock_envelope.get("ok") is not True
            ):
                raise TargetMatrixError(
                    "matrix.cell_lock_invalid",
                    "cell lock resolution did not accept the exact target selection",
                )
            lock_result = lock_envelope.get("result")
            if (
                not isinstance(lock_result, dict)
                or lock_result.get("target") != cell.target
            ):
                raise TargetMatrixError(
                    "matrix.cell_lock_invalid",
                    "cell lock resolution did not accept the exact target selection",
                )
            lock_identity = _identity(
                lock_result.get("component_lock_identity"),
                "cell lock identity",
            )

            rebuild_command = [
                sys.executable,
                "-m",
                "literate_ai.cli",
                "--json",
                "rebuild",
                cell.component,
                "--project",
                str(project_root),
                "--target",
                cell.target,
                "--runtime-root",
                str(runtime_root),
                "--candidate-receipt",
                str(candidate),
            ]
            for flavor in cell.flavors:
                rebuild_command.extend(("--flavor", flavor))
            if allow_host_execution:
                rebuild_command.append("--allow-host-execution")
            try:
                completed = run_with_tree_kill(
                    rebuild_command,
                    cwd=project_root,
                    env=environment,
                    text=True,
                    timeout=timeout_seconds,
                )
            except subprocess.TimeoutExpired as exc:
                raise TargetMatrixError(
                    "matrix.cell_timeout",
                    f"cell lifecycle exceeded {timeout_seconds:g} seconds",
                ) from exc
        if completed.returncode != 0:
            detail = completed.stderr or completed.stdout or "cell lifecycle failed"
            raise TargetMatrixError(
                "matrix.cell_failed",
                redact_secrets(detail, environment)[:512],
            )
        envelope = json.loads(completed.stdout)
        if not isinstance(envelope, dict) or envelope.get("ok") is not True:
            raise TargetMatrixError(
                "matrix.cell_result_invalid",
                "cell lifecycle returned an invalid envelope",
            )
        result = envelope.get("result")
        if not isinstance(result, dict):
            raise TargetMatrixError(
                "matrix.cell_result_invalid", "cell lifecycle result must be an object"
            )
        if result.get("component_lock_identities") != [lock_identity.uri]:
            raise TargetMatrixError(
                "matrix.cell_lock_mismatch",
                "cell lifecycle did not consume the exact scoped lock selected before "
                "rebuild",
            )
        return result

    return execute
