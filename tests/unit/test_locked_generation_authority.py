"""Canonical Component-lock to generation-authority admission tests."""

from __future__ import annotations

import hashlib
import json
import os
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
from tests.unit.test_component_lock_planning import _fixture, _flavor_variant

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
    def test_scoped_lock_bytes_replace_host_lock_in_closure_and_revalidation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            component, flavors = _fixture(root)
            host_lock = _write_lock(component, flavors, target="committed-host-target")
            host_path = component / "component.lock.json"
            host_bytes = host_path.read_bytes()
            self.assertEqual(
                host_bytes,
                canonical_json_bytes(host_lock.to_dict()) + b"\n",
            )

            closure_identities: set[str] = set()
            scoped_bytes: list[bytes] = []
            for target in ("linux-target", "windows-target", "macos-target"):
                cell_root = root / f"cell-{target}"
                with mock.patch.dict(
                    os.environ,
                    {"LITAI_MATRIX_CELL_ROOT": str(cell_root)},
                    clear=False,
                ):
                    _write_lock(component, flavors, target=target)
                    store = ComponentLockStore(component)
                    content = store.path.read_bytes()
                    scoped_bytes.append(content)
                    snapshot = FilesystemLockedGenerationAuthorityReader().read(
                        component,
                        target_name=target,
                        flavor_selectors=_SELECTORS,
                        flavor_roots=(flavors,),
                    )
                    closure_identities.add(snapshot.input_closure_identity)
                    component_lock_input = next(
                        item
                        for item in snapshot.input_closure.to_dict()["inputs"]
                        if item["labels"] == ["component-lock"]
                    )
                    self.assertEqual(
                        component_lock_input["content_identity"],
                        f"sha256:{hashlib.sha256(content).hexdigest()}",
                    )

                    host_path.write_bytes(host_bytes + b" ")
                    snapshot.require_unchanged()
                    host_path.write_bytes(host_bytes)

                    store.path.write_bytes(content + b" ")
                    with self.assertRaises(
                        LockedGenerationAuthorityReaderError
                    ) as changed:
                        snapshot.require_unchanged()
                    self.assertEqual(
                        changed.exception.code,
                        "component_lock.changed_during_lifecycle",
                    )

            self.assertEqual(len(set(scoped_bytes)), 3)
            self.assertEqual(len(closure_identities), 3)
            self.assertEqual(host_path.read_bytes(), host_bytes)

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

    def test_reader_rejects_wrong_target_and_selector_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            component, flavors = _fixture(Path(temporary))
            _write_lock(component, flavors)
            reader = FilesystemLockedGenerationAuthorityReader()

            with self.assertRaises(LockedGenerationAuthorityReaderError) as target:
                reader.read(
                    component,
                    target_name="linux-host",
                    flavor_selectors=_SELECTORS,
                    flavor_roots=(flavors,),
                )
            self.assertEqual(target.exception.code, "locked_generation.target_mismatch")

            with self.assertRaises(LockedGenerationAuthorityReaderError) as selector:
                reader.read(
                    component,
                    target_name=_TARGET,
                    flavor_selectors=("+macos", "-python"),
                    flavor_roots=(flavors,),
                )
            self.assertEqual(
                selector.exception.code, "locked_generation.selector_mismatch"
            )

    def test_reader_requires_the_exact_current_resolution_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            component, flavors = _fixture(Path(temporary))
            _write_lock(component, flavors)
            audit = ComponentResolutionAuditStore(component, _TARGET)
            audit.path.unlink()

            with self.assertRaises(LockedGenerationAuthorityReaderError) as missing:
                FilesystemLockedGenerationAuthorityReader().read(
                    component,
                    target_name=_TARGET,
                    flavor_selectors=_SELECTORS,
                    flavor_roots=(flavors,),
                )

            self.assertEqual(
                missing.exception.code, "component_resolution_audit.missing"
            )

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

    def test_reader_rejects_authoring_and_selected_flavor_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            component, flavors = _fixture(Path(temporary))
            _write_lock(component, flavors)
            reader = FilesystemLockedGenerationAuthorityReader()

            authoring = component / "component.md"
            original = authoring.read_text(encoding="utf-8")
            authoring.write_text(
                original.replace("A small portable", "A revised portable"),
                encoding="utf-8",
                newline="\n",
            )
            with self.assertRaises(LockedGenerationAuthorityReaderError) as stale:
                reader.read(
                    component,
                    target_name=_TARGET,
                    flavor_selectors=_SELECTORS,
                    flavor_roots=(flavors,),
                )
            self.assertEqual(stale.exception.code, "component_lock.stale")

            authoring.write_text(original, encoding="utf-8", newline="\n")
            flavor_manifest = flavors / "lang-python" / "flavor.md"
            flavor_manifest.write_text(
                flavor_manifest.read_text(encoding="utf-8").replace(
                    "Portable Python 3.11+", "Drifted Python"
                ),
                encoding="utf-8",
                newline="\n",
            )
            with self.assertRaises(LockedGenerationAuthorityReaderError) as flavor:
                reader.read(
                    component,
                    target_name=_TARGET,
                    flavor_selectors=_SELECTORS,
                    flavor_roots=(flavors,),
                )
            self.assertEqual(flavor.exception.code, "locked_generation.flavor_stale")

    def test_snapshot_revalidates_selected_exact_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            component, flavors = _fixture(Path(temporary))
            _write_lock(component, flavors)
            snapshot = FilesystemLockedGenerationAuthorityReader().read(
                component,
                target_name=_TARGET,
                flavor_selectors=_SELECTORS,
                flavor_roots=(flavors,),
            )
            specification = component / "specs" / "spec.md"
            specification.write_text(
                specification.read_text(encoding="utf-8") + "\nDrift.\n",
                encoding="utf-8",
            )

            with self.assertRaises(LockedGenerationAuthorityReaderError) as stale:
                snapshot.require_unchanged()
            self.assertEqual(stale.exception.code, "component_lock.stale")

    def test_snapshot_revalidates_the_exact_resolution_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            component, flavors = _fixture(Path(temporary))
            _write_lock(component, flavors)
            snapshot = FilesystemLockedGenerationAuthorityReader().read(
                component,
                target_name=_TARGET,
                flavor_selectors=_SELECTORS,
                flavor_roots=(flavors,),
            )
            ComponentResolutionAuditStore(component, _TARGET).path.unlink()

            with self.assertRaises(LockedGenerationAuthorityReaderError) as stale:
                snapshot.require_unchanged()

            self.assertEqual(stale.exception.code, "component_lock.stale")

    def test_reader_rejects_stale_locked_selector_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            component, flavors = _fixture(root)
            _write_lock(component, flavors)
            selected_skill = root / "skills" / "implement.json"
            selected_skill.write_text(
                selected_skill.read_text(encoding="utf-8") + " ",
                encoding="utf-8",
            )

            with self.assertRaises(LockedGenerationAuthorityReaderError) as stale:
                FilesystemLockedGenerationAuthorityReader().read(
                    component,
                    target_name=_TARGET,
                    flavor_selectors=_SELECTORS,
                    flavor_roots=(flavors,),
                )
            self.assertEqual(stale.exception.code, "component_lock.stale")

    def test_unselected_catalog_addition_requires_fresh_graph_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            component, flavors = _fixture(Path(temporary))
            _write_lock(component, flavors)
            reader = FilesystemLockedGenerationAuthorityReader()
            before = reader.read(
                component,
                target_name=_TARGET,
                flavor_selectors=_SELECTORS,
                flavor_roots=(flavors,),
            )

            _flavor_variant(
                flavors / "lang-python",
                flavors / "ruby-unselected",
                name="implementation-ruby-unselected",
                value="ruby-unselected",
            )
            with self.assertRaises(LockedGenerationAuthorityReaderError) as stale:
                reader.read(
                    component,
                    target_name=_TARGET,
                    flavor_selectors=_SELECTORS,
                    flavor_roots=(flavors,),
                )
            self.assertEqual(stale.exception.code, "component_lock.stale")

            _write_lock(component, flavors)
            after = reader.read(
                component,
                target_name=_TARGET,
                flavor_selectors=_SELECTORS,
                flavor_roots=(flavors,),
            )

            self.assertEqual(after.authority.identity, before.authority.identity)
            self.assertEqual(
                after.input_closure_identity, before.input_closure_identity
            )


if __name__ == "__main__":
    unittest.main()
