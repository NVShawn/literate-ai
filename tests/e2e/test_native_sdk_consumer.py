"""Actual source-build products become exact, temporary consumer SDK inputs."""

from __future__ import annotations

import subprocess
import sys
import unittest

from literate_ai.adapters.native_sdk_consumer import NativeSdkConsumerInputs
from literate_ai.contracts.identity import canonical_identity
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
