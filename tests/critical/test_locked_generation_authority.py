"""Canonical Component-lock to generation-authority admission tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import literate_ai.adapters.component_lock_planning as lock_planning
from literate_ai.adapters.component_lock_planning import (
    FilesystemComponentLockPlanner,
)
from literate_ai.adapters.component_locks import ComponentLockStore
from literate_ai.adapters.component_resolution_audits import (
    ComponentResolutionAuditStore,
)
from literate_ai.adapters.locked_generation_authority import (
    FilesystemLockedGenerationAuthorityReader,
    LockedGenerationAuthorityReaderError,
)
from literate_ai.application.component_lock_resolution import ComponentLockResolver
from literate_ai.contracts.identity import canonical_json_bytes
from tests.support.fixtures_test_component_lock_planning import (
    _fixture,
)

_SELECTORS = ("+macos", "+python")
_TARGET = "macos-host"


def _write_lock(
    component: Path,
    flavors: Path,
    *,
    target: str = _TARGET,
    selectors: tuple[str, ...] = _SELECTORS,
):
    plan = FilesystemComponentLockPlanner().plan(
        component,
        target_name=target,
        flavor_selectors=selectors,
        flavor_roots=(flavors,),
    )
    result = ComponentLockResolver().resolve(
        plan, expected_input_evidence_identity=plan.identity
    )
    ComponentResolutionAuditStore(component, target).update(result.catalog_audit)
    ComponentLockStore(component).update(result.lock)
    return result.lock


class LockedGenerationAuthorityTests(unittest.TestCase):
    def test_reader_requires_a_canonical_unforged_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            component, flavors = _fixture(Path(temporary))
            reader = FilesystemLockedGenerationAuthorityReader()
            with self.assertRaises(LockedGenerationAuthorityReaderError) as missing:
                reader.read(
                    component,
                    target_name=_TARGET,
                    flavor_selectors=_SELECTORS,
                    flavor_roots=(flavors,),
                )
            self.assertEqual(missing.exception.code, "component_lock.missing")

            lock = _write_lock(component, flavors)
            path = component / "component.lock.json"
            path.write_text(json.dumps(lock.to_dict(), indent=2), encoding="utf-8")
            with self.assertRaises(LockedGenerationAuthorityReaderError) as malformed:
                reader.read(
                    component,
                    target_name=_TARGET,
                    flavor_selectors=_SELECTORS,
                    flavor_roots=(flavors,),
                )
            self.assertEqual(malformed.exception.code, "component_lock.noncanonical")

            forged = lock.to_dict()
            forged["root_revision"]["digest"] = "0" * 64
            path.write_bytes(canonical_json_bytes(forged) + b"\n")
            with self.assertRaises(LockedGenerationAuthorityReaderError) as rejected:
                reader.read(
                    component,
                    target_name=_TARGET,
                    flavor_selectors=_SELECTORS,
                    flavor_roots=(flavors,),
                )
            self.assertEqual(rejected.exception.code, "component_lock.invalid")

    def test_reader_and_snapshot_reject_effective_graph_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            component, flavors = _fixture(Path(temporary))
            graph_a = "sha256:" + "a" * 64
            graph_b = "sha256:" + "b" * 64
            with mock.patch.object(
                lock_planning,
                "_effective_authority_graph_identity",
                return_value=graph_a,
            ):
                _write_lock(component, flavors)
                snapshot = FilesystemLockedGenerationAuthorityReader().read(
                    component,
                    target_name=_TARGET,
                    flavor_selectors=_SELECTORS,
                    flavor_roots=(flavors,),
                )

            with mock.patch.object(
                lock_planning,
                "_effective_authority_graph_identity",
                return_value=graph_b,
            ):
                with self.assertRaises(LockedGenerationAuthorityReaderError) as stale:
                    FilesystemLockedGenerationAuthorityReader().read(
                        component,
                        target_name=_TARGET,
                        flavor_selectors=_SELECTORS,
                        flavor_roots=(flavors,),
                    )
                with self.assertRaises(LockedGenerationAuthorityReaderError) as drift:
                    snapshot.require_unchanged()

            self.assertEqual(stale.exception.code, "component_lock.stale")
            self.assertEqual(drift.exception.code, "component_lock.stale")


if __name__ == "__main__":
    unittest.main()
