from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from literate_ai.contracts.identity import canonical_identity
from literate_ai.target_matrices import (
    TargetMatrixCell,
    TargetMatrixCellReceipt,
    TargetMatrixDeclaration,
    TargetMatrixError,
    TargetMatrixStageEvidence,
    run_target_matrix,
)


def identity(label: str):
    return canonical_identity({"label": label})


def passing_result(cell: TargetMatrixCell) -> dict[str, object]:
    return {
        "specification": cell.component,
        "target": cell.target,
        "explicit_flavor_selectors": list(cell.flavors),
        "validated_project_authority_identity": identity("authority").uri,
        "lifecycle_request_identity": identity(f"plan:{cell.identity.uri}").uri,
        "component_lock_identities": [identity(f"lock:{cell.identity.uri}").uri],
        "source_cache_lifecycle_identity": identity(f"source:{cell.identity.uri}").uri,
        "receipt_identity": identity(f"lifecycle:{cell.identity.uri}").uri,
    }


class TargetMatrixContractTests(unittest.TestCase):
    def cells(self) -> tuple[TargetMatrixCell, TargetMatrixCell]:
        return (
            TargetMatrixCell(
                "components/renderer", "linux-x86_64", ("+lang-cpp", "+build-bazel")
            ),
            TargetMatrixCell(
                "components/renderer", "windows-x86_64", ("+lang-cpp", "+build-make")
            ),
        )

    def test_linux_windows_mixed_result_is_unaccepted_and_persisted(self) -> None:
        declaration = TargetMatrixDeclaration.create(self.cells())

        def execute(cell: TargetMatrixCell, _: Path) -> dict[str, object]:
            if cell.target == "linux-x86_64":
                raise RuntimeError("interrupted")
            return passing_result(cell)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            receipt = run_target_matrix(
                declaration, evidence_root=root, executor=execute, jobs=2
            )
            persisted = json.loads(
                (root / "aggregate-receipt.json").read_text(encoding="utf-8")
            )
        self.assertEqual(receipt.status, "partial")
        self.assertFalse(receipt.accepted)
        self.assertFalse(persisted["accepted"])
        self.assertEqual(
            {item.status for item in receipt.receipts}, {"passed", "failed"}
        )

    def test_cross_cell_and_predecessor_tampering_are_rejected(self) -> None:
        first, second = self.cells()
        foreign = TargetMatrixStageEvidence(
            second.identity, "plan", identity("plan"), None, True
        )
        with self.assertRaisesRegex(TargetMatrixError, "another cell"):
            TargetMatrixCellReceipt(
                first,
                "passed",
                identity("authority"),
                identity("plan"),
                identity("lock"),
                identity("source"),
                (foreign,),
            )

        stages = []
        predecessor = None
        for stage in ("plan", "lock", "source-admission", "lifecycle"):
            item = TargetMatrixStageEvidence(
                first.identity,
                stage,
                identity(stage),
                identity("wrong") if stage == "lock" else predecessor,
                True,
            )
            stages.append(item)
            predecessor = item.identity
        with self.assertRaisesRegex(TargetMatrixError, "complete accepted"):
            TargetMatrixCellReceipt(
                first,
                "passed",
                identity("authority"),
                identity("plan"),
                identity("lock"),
                identity("source"),
                tuple(stages),
            )


if __name__ == "__main__":
    unittest.main()
