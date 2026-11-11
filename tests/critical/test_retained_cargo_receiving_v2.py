"""Real v2 archive publication in an adopted source owner; no model invocation."""

import copy
import os
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters import retained_cargo_materialization_v2 as receiver
from literate_ai.adapters.retained_cargo_files_v2 import read_retained_cargo_files_v2
from literate_ai.adapters.retained_cargo_import_v2 import RetainedCargoCurrentInputsV2
from literate_ai.adapters.retained_cargo_scratch_v2 import (
    reserve_retained_cargo_scratch_v2,
)
from literate_ai.adapters.retained_cargo_wrapper_v2 import (
    retained_cargo_wrapper_context_v2,
)
from tests.support.fixtures_retained_cargo_scratch import scratch_review_fixture


@unittest.skipUnless(
    os.name != "nt" and shutil.which("git") and shutil.which("make"),
    "requires native POSIX adoption and Git",
)
class RetainedCargoReceivingV2Tests(unittest.TestCase):
    def setUp(self):
        self.base = Path(
            self.enterContext(tempfile.TemporaryDirectory(prefix="rv2-"))
        ).resolve()
        self.reviewed, self.provider = self.enterContext(
            scratch_review_fixture(self.base, with_provider=True)
        )
        common = self.reviewed.plan.common
        workspace = self.reviewed.project.root / common.workspace_root
        # Exact fixture after-manifests are authored before the live source owner.
        (workspace / "Cargo.toml").write_bytes(b"workspace")
        (workspace / "Cargo.lock").write_bytes(b"lock")
        self.current = RetainedCargoCurrentInputsV2(
            self.reviewed,
            replace(
                self.provider.inputs,
                importer_project_id=self.reviewed.project.definition.project_id,
                reviewed_binding_identity=self.reviewed.binding.identity,
                configured_store_id=self.reviewed.binding.common.source_store_id,
            ),
        )
        self.context = self.enterContext(
            retained_cargo_wrapper_context_v2(self.reviewed)
        )
        self.scratch_manager = reserve_retained_cargo_scratch_v2(self.context)
        self.scratch = self.enterContext(self.scratch_manager)
        self.files = read_retained_cargo_files_v2(
            self.reviewed, manifest_state="after", package_state="absent"
        )

    def receive(self, **changes):
        args = {**self.provider.calls, "read_current": lambda: self.current}
        args.update(changes)
        return receiver.materialize_retained_cargo_archive_v2(
            self.files, scratch=self.scratch, **args
        )

    def test_real_archive_is_published_under_live_original_source_and_expires(self):
        materialized = self.receive()
        # Independent importing-maintainer destinations name ALL graph exports,
        # not only library products. Never derive this expectation from the
        # materializer's observed package count.
        expected = {
            self.reviewed.project.root / destination
            for _, destination in self.reviewed.binding.common.destinations
        }
        self.assertEqual({tree.root for tree in materialized.packages}, expected)
        self.assertEqual(len(materialized.packages), len(expected))
        self.assertEqual(
            len(materialized.capture.exports.libraries),
            len(self.reviewed.binding.common.exports.libraries),
        )
        self.assertEqual(
            materialized.capture.exports, self.reviewed.binding.common.exports
        )
        self.assertTrue(
            all(
                tree.root.is_relative_to(self.context.workspace / "pkgs")
                for tree in materialized.packages
            )
        )
        materialized.require_unchanged()
        with self.assertRaises(TypeError):
            copy.copy(materialized)
        self.scratch_manager.__exit__(None, None, None)
        with self.assertRaisesRegex(ValueError, "scratch-operation-ended"):
            materialized.require_unchanged()

    def test_review_cannot_add_foreign_or_omit_graph_export_destinations(self):
        from literate_ai.contracts.identity import canonical_identity

        selected = self.reviewed.binding.common
        for values in (
            selected.destinations[:-1],
            tuple(
                sorted(
                    (
                        *selected.destinations,
                        (canonical_identity("foreign-export"), "foreign/destination"),
                    ),
                    key=lambda row: row[0].uri,
                )
            ),
        ):
            with self.subTest(destinations=len(values)), self.assertRaises(ValueError):
                replace(selected, destinations=values)
        self.assertFalse(
            any(
                (self.reviewed.project.root / value).exists()
                for _, value in selected.destinations
            )
        )

    def test_source_addition_is_never_hidden_by_owned_package_output(self):
        original = receiver.publish_directory_exclusive

        def changed_source(*args, **kwargs):
            original(*args, **kwargs)
            (self.context.workspace / "foreign-source.rs").write_bytes(b"preserve")

        with patch.object(
            receiver, "publish_directory_exclusive", side_effect=changed_source
        ):
            with self.assertRaises(ValueError):
                self.receive()
        self.assertEqual(
            (self.context.workspace / "foreign-source.rs").read_bytes(), b"preserve"
        )
        # End the refused transaction without pretending the changed source is current.
        self.context.workspace.joinpath("foreign-source.rs").unlink()

    def test_later_collision_rolls_back_only_unchanged_owned_package(self):
        original = receiver.publish_directory_exclusive
        count = 0
        destinations = sorted(
            self.reviewed.project.root / value
            for _, value in self.reviewed.binding.common.destinations
        )

        def collision(source, destination, **kwargs):
            nonlocal count
            if destination in destinations:
                count += 1
                if count == 2:
                    destination.mkdir()
                    (destination / "foreign").write_bytes(b"keep")
                    raise FileExistsError("foreign fixture destination")
            return original(source, destination, **kwargs)

        with patch.object(
            receiver, "publish_directory_exclusive", side_effect=collision
        ):
            with self.assertRaises(FileExistsError):
                self.receive()
        self.assertFalse(destinations[0].exists())
        self.assertEqual((destinations[1] / "foreign").read_bytes(), b"keep")
        self.assertTrue(list((self.context.workspace / "pkgs").glob(".rp-*")))
