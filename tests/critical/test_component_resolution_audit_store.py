"""Target-specific resolution-audit filesystem adapter tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from literate_ai.adapters.component_resolution_audits import (
    ComponentResolutionAuditStore,
    ComponentResolutionAuditStoreError,
)
from literate_ai.contracts.component_locking import ComponentResolutionAudit
from literate_ai.contracts.identity import canonical_identity
from tests.support.fixtures_test_component_lock_contracts import component_lock


def audit(label: str = "catalog") -> ComponentResolutionAudit:
    lock = component_lock()
    return ComponentResolutionAudit(
        component_lock_identity=lock.identity,
        catalog_identity=canonical_identity({"catalog": label}),
        resolver_identity=lock.resolver_identity,
        candidates=(),
    )


class ComponentResolutionAuditStoreTests(unittest.TestCase):
    def test_symlink_destination_fails_without_mutating_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            store = ComponentResolutionAuditStore(root, "host")
            outside = root / "outside.json"
            outside.write_bytes(b"outside-sentinel\n")
            try:
                store.path.symlink_to(outside)
            except OSError as error:
                self.skipTest(f"host cannot create file symlinks: {error}")

            with self.assertRaisesRegex(
                ComponentResolutionAuditStoreError, "regular file"
            ):
                store.update(audit())
            self.assertEqual(outside.read_bytes(), b"outside-sentinel\n")


if __name__ == "__main__":
    unittest.main()
