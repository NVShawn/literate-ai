"""Controller source publication verifies full proof and exact workspace custody."""

import shutil
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import tests.support.fixtures_test_action_generate_result as fixture_module
from literate_ai.adapters.action_dispatch_wire import ActionWireError, record_identity
from literate_ai.adapters.action_generate_source import receive_generated_source
from literate_ai.adapters.lifecycle.standard_local import LocalSourceTreeRegistry
from literate_ai.adapters.source_generation import CachedCodingCliSourceGenerationError
from literate_ai.contracts import canonical_identity
from literate_ai.storage import FileSystemCAS


class GenerateSourceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = f = fixture_module.GenerateWorkerResultTests()
        self.addCleanup(f.doCleanups)
        f.setUp()
        self.prepared = f.fixture.node
        self.root = Path(self.prepared.workspace.locator)
        shutil.rmtree(self.root)
        self.root.mkdir()
        self.cas = FileSystemCAS(f.fixture.root / "controller")
        self.registry = LocalSourceTreeRegistry()
        self.healthy = True

    def guard(self):
        if not self.healthy:
            raise ActionWireError("fixture.revoked", "authority revoked")

    def receive(self, **changes):
        f = self.fixture
        content = f.result.to_bytes()
        return receive_generated_source(
            **(
                dict(
                    content=content,
                    result_identity=record_identity(content),
                    input_record=f.input,
                    input_identity=f.identity,
                    prepared=self.prepared,
                    deadline=f.deadline,
                    cas=self.cas,
                    registry=self.registry,
                    admission_guard=self.guard,
                    blob_source=f.fixture.cas.get_bytes,
                )
                | changes
            )
        )

    def test_received_files_register_exact_source_and_recipe_validation(self):
        output = self.receive()
        self.assertEqual(output, self.fixture.output)
        self.assertEqual(
            self.registry.resolve(output.candidate.tree_identity), self.root
        )
        custody = self.registry.registered_evidence(output.candidate.tree_identity)
        self.assertEqual(custody.candidate, output.candidate)
        self.assertEqual(custody.source_generation_identity, output.identity)
        self.assertEqual(list(self.root.parent.glob("generated-*")), [])

    def test_nonempty_allocation_is_preserved(self):
        marker = self.root / "user.txt"
        marker.write_text("preserve")
        with self.assertRaises(CachedCodingCliSourceGenerationError):
            self.receive()
        self.assertEqual(marker.read_text(), "preserve")
        self.assertEqual(tuple(self.cas.iter_refs()), ())

    def test_foreign_preparation_refuses_before_transfer(self):
        foreign = replace(
            self.prepared,
            workspace=replace(
                self.prepared.workspace,
                allocation_identity=canonical_identity("foreign"),
            ),
        )
        with self.assertRaises(ActionWireError):
            self.receive(prepared=foreign)
        self.assertEqual(tuple(self.cas.iter_refs()), ())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_revocation_during_staging_leaves_original_allocation_empty(self):
        copy = self.cas.copy_to

        def revoked(*args, **kwargs):
            copy(*args, **kwargs)
            self.healthy = False

        with patch.object(self.cas, "copy_to", side_effect=revoked):
            with self.assertRaises(ActionWireError):
                self.receive()
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual(list(self.root.parent.glob("generated-*")), [])
        self.assertEqual(self.registry._paths, {})

    def test_concurrent_destination_is_not_overwritten_or_removed(self):
        from literate_ai.adapters.exclusive_directory import publish_directory_exclusive

        def concurrent(stage, destination, **kwargs):
            destination.mkdir()
            (destination / "peer.txt").write_text("preserve")
            publish_directory_exclusive(stage, destination, **kwargs)

        with patch(
            "literate_ai.adapters.action_generate_source.publish_directory_exclusive",
            side_effect=concurrent,
        ):
            with self.assertRaises(FileExistsError):
                self.receive()
        self.assertEqual((self.root / "peer.txt").read_text(), "preserve")
        self.assertEqual(list(self.root.parent.glob("generated-*")), [])
        self.assertEqual(self.registry._paths, {})

    def test_invalid_source_proof_never_materializes(self):
        with self.assertRaises(ActionWireError):
            self.receive(blob_source=lambda _: b"bad")
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual(self.registry._paths, {})

    def test_publication_error_after_rename_removes_only_owned_source(self):
        from literate_ai.adapters.exclusive_directory import publish_directory_exclusive

        def interrupted(stage, destination, **kwargs):
            publish_directory_exclusive(stage, destination, **kwargs)
            raise OSError("publication interrupted")

        with patch(
            "literate_ai.adapters.action_generate_source.publish_directory_exclusive",
            side_effect=interrupted,
        ):
            with self.assertRaises(OSError):
                self.receive()
        self.assertFalse(self.root.exists())
        self.assertEqual(list(self.root.parent.glob("generated-*")), [])
        self.assertEqual(self.registry._paths, {})
