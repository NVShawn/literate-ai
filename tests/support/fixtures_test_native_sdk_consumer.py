from __future__ import annotations

"""Shared fixtures extracted from ``tests.unit.test_native_sdk_consumer``."""

import subprocess
import sys
import unittest
from unittest.mock import patch

from literate_ai.adapters.native_sdk_consumer import NativeSdkConsumerInputs
from literate_ai.contracts.identity import canonical_identity
from literate_ai.storage.cas import BlobIntegrityError
from tests.support import (
    fixtures_test_native_sdk_source_build as test_native_sdk_source_build,
)


class NativeSdkConsumerTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_native_sdk_source_build.NativeSdkSourceBuildTests(
            "test_real_policy_build_cache_and_relocated_native_execution"
        )
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.service = self.fixture.service()
        self.parent = self.fixture.fixture.root / "consumer"
        self.parent.mkdir()

    def inputs(self, services=None):
        return NativeSdkConsumerInputs(
            snapshot=self.fixture.snapshot,
            services=(self.service,) if services is None else services,
        )

    def test_unbuilt_or_missing_source_builds_never_execute_as_a_side_effect(self):
        with patch("literate_ai.adapters.native_sdk_build.run_bounded_process") as run:
            with self.assertRaisesRegex(ValueError, "not completed"):
                self.inputs()
            with self.assertRaisesRegex(ValueError, "every selected"):
                self.inputs(())
            with self.assertRaisesRegex(TypeError, "source-build services"):
                self.inputs(({"sdk": "self-asserted input"},))
            run.assert_not_called()
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_bound_sdk_inputs_relocate_run_and_clean_up_without_source(self):
        built = self.service.build()
        inputs = self.inputs()
        revision = built.selection.component_revision
        target = built.selection.target_identity
        (binding,) = inputs.for_consumer(revision, target_identity=target)
        self.assertEqual(binding.build, built)
        self.assertEqual(
            binding.to_dict()["source_admission"], built.resolution.admission.to_dict()
        )
        self.assertEqual(
            self.fixture.fixture.manifest(binding.identity), binding.to_dict()
        )
        self.assertNotIn(str(self.fixture.fixture.root), str(binding.to_dict()))
        self.fixture.fixture.remove_source()
        unrelated = self.parent / "keep.txt"
        unrelated.write_text("consumer-owned")
        with inputs.materialize(
            revision, target_identity=target, parent=self.parent
        ) as values:
            (sdk,) = values
            self.assertEqual(sdk.binding.identity, binding.identity)
            observed = self.service.store.get_manifest(sdk.runtime_observation)
            self.assertEqual(
                observed["snapshot_identity"], built.product.snapshot.identity.to_dict()
            )
            self.assertTrue(observed["native_libraries"])
            result = subprocess.run(
                (
                    sys.executable,
                    "-I",
                    "-B",
                    "-c",
                    "import sys;sys.path.insert(0,sys.argv[1]);import vendor_math;"
                    "print(vendor_math.scale(1.5,3))",
                    str(sdk.import_root),
                ),
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "4.5")
        self.assertFalse(sdk.root.exists())
        self.assertEqual(unrelated.read_text(), "consumer-owned")
        self.assertEqual(list(self.parent.iterdir()), [unrelated])
        with self.assertRaisesRegex(ValueError, "exact selected"):
            self.inputs((self.service, self.service))
        with self.assertRaisesRegex(ValueError, "target differs"):
            inputs.for_consumer(
                revision, target_identity=canonical_identity("wrong target")
            )
        with self.assertRaisesRegex(ValueError, "absent"):
            inputs.for_consumer(
                canonical_identity("wrong consumer"), target_identity=target
            )

    def test_changed_consumer_sdk_refuses_and_removes_only_owned_directory(self):
        built = self.service.build()
        inputs = self.inputs()
        unrelated = self.parent / "keep.txt"
        unrelated.write_text("keep")
        with self.assertRaisesRegex(ValueError, "changed during use"):
            with inputs.materialize(
                built.selection.component_revision,
                target_identity=built.selection.target_identity,
                parent=self.parent,
            ) as values:
                sdk = values[0]
                library = sdk.root / built.product.snapshot.native_libraries[0]
                library.write_bytes(library.read_bytes() + b"changed")
        self.assertFalse(sdk.root.exists())
        self.assertEqual(list(self.parent.iterdir()), [unrelated])

    def test_consumer_exception_still_cleans_up_its_sdk_inputs(self):
        built = self.service.build()
        inputs = self.inputs()
        with self.assertRaisesRegex(RuntimeError, "consumer failed"):
            with inputs.materialize(
                built.selection.component_revision,
                target_identity=built.selection.target_identity,
                parent=self.parent,
            ) as values:
                root = values[0].root
                raise RuntimeError("consumer failed")
        self.assertFalse(root.exists())
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_corrupt_cas_refuses_before_creating_consumer_directories(self):
        built = self.service.build()
        inputs = self.inputs()
        native = next(
            item
            for item in built.product.snapshot.files
            if item.path == built.product.snapshot.native_libraries[0]
        )
        blob = self.service.store.path_for(native.blob)
        content = blob.read_bytes()
        blob.chmod(0o600)
        blob.write_bytes(content + b"corrupt")
        with self.assertRaises(BlobIntegrityError):
            with inputs.materialize(
                built.selection.component_revision,
                target_identity=built.selection.target_identity,
                parent=self.parent,
            ):
                self.fail("corrupt SDK input was exposed to a consumer")
        self.assertEqual(list(self.parent.iterdir()), [])
