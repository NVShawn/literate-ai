"""CLI adapter for canonical concurrent target matrices."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from literate_ai.adapters.project_validation import (
    validated_project_authority_identity,
)
from literate_ai.target_matrices import (
    TargetMatrixDeclaration,
    TargetMatrixError,
    run_target_matrix,
    subprocess_rebuild_executor,
)

from .errors import CliFailure


def target_matrix_from_args(args: Any) -> tuple[dict[str, object], int]:
    declaration_path = Path(args.declaration).resolve(strict=True)
    if declaration_path.is_symlink() or not declaration_path.is_file():
        raise CliFailure(
            "matrix.declaration_unsafe", "matrix declaration must be a direct file"
        )
    try:
        declaration = TargetMatrixDeclaration.from_dict(
            json.loads(declaration_path.read_text(encoding="utf-8"))
        )
        current_authority_identity = (
            validated_project_authority_identity(
                Path(args.project), synchronize_source_intelligence=False
            )
            if args.reuse
            else None
        )
        aggregate = run_target_matrix(
            declaration,
            evidence_root=Path(args.evidence_root),
            executor=subprocess_rebuild_executor(
                project=Path(args.project),
                allow_host_execution=bool(args.allow_host_execution),
                timeout_seconds=float(args.cell_timeout),
            ),
            jobs=args.jobs,
            reuse=bool(args.reuse),
            current_authority_identity=current_authority_identity,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, TargetMatrixError) as exc:
        if isinstance(exc, TargetMatrixError):
            raise CliFailure(exc.code, exc.message) from exc
        raise CliFailure("matrix.declaration_invalid", str(exc)) from exc
    return (
        {
            **aggregate.to_dict(),
            "aggregate_receipt_identity": aggregate.identity.uri,
            "evidence_root": str(Path(args.evidence_root).resolve()),
        },
        0 if aggregate.accepted else 1,
    )
