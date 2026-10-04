"""Real SDK producer custody binds Standard intent and composite build authority.

Per-node generation inputs are fixtures: public planning remains closed until the
consumer execution path is complete. SDK source capture, build and CAS are real.
"""

import unittest
from dataclasses import replace
from types import SimpleNamespace

from literate_ai.adapters.lifecycle import (
    LocalStandardLifecyclePorts,
)
from literate_ai.adapters.native_sdk_consumer import NativeSdkConsumerInputs
from literate_ai.contracts.identity import canonical_identity
from literate_ai.storage.cas import BlobIntegrityError
from tests.support import (
    fixtures_test_native_sdk_source_build as test_native_sdk_source_build,
)
from tests.support.fixtures_test_standard_local_command_adapter import (
    _python_copy_lifecycle,
)


class NativeSdkStandardAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_native_sdk_source_build.NativeSdkSourceBuildTests(
            "test_real_policy_build_cache_and_relocated_native_execution"
        )
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.entrypoint_kind = getattr(self, "entrypoint_kind", None)
        self.fixture.configure_recipe_fixture = getattr(
            self, "configure_recipe_fixture", None
        )
        self.fixture.setUp()
        self.service = self.fixture.service()
        self.built = self.service.build()
        self.inputs = NativeSdkConsumerInputs(
            snapshot=self.fixture.snapshot, services=(self.service,)
        )
        root = self.fixture.fixture.root / "standard"
        root.mkdir()
        baseline, _, candidate, _ = _python_copy_lifecycle(root)
        original = next(iter(baseline.contracts.values()))
        self.contract = replace(
            original,
            component_revision=self.built.selection.component_revision,
            artifact_export=replace(
                original.artifact_export,
                target_identity=self.built.selection.target_identity,
            ),
        )
        self.arguments = dict(
            source_trees=baseline.source_trees,
            object_root=root / "sdk-objects",
            contracts=(self.contract,),
            tool_bindings=tuple(baseline.tool_bindings.values()),
        )
        self.ports = LocalStandardLifecyclePorts(
            **self.arguments, native_sdk_inputs=self.inputs
        )
        self.execution = SimpleNamespace(
            component_lock_identity=self.fixture.snapshot.authority.lock.identity
        )
        self.generation = SimpleNamespace(
            identity=candidate.component_generation_plan_identity,
            component_revision=self.contract.component_revision,
            direct_generation_edges=(),
        )
        self.candidate = replace(
            candidate, component_revision=self.contract.component_revision
        )

    def intent(self, ports=None):
        return (ports or self.ports).create(
            self.execution,
            self.generation,
            self.candidate,
            getattr(self, "provider_artifacts", ()),
            (),
        )

    def test_corrupted_sdk_custody_refuses_after_intent_and_after_authorization(self):
        intent = self.intent()
        index = canonical_identity("current generated-source index")
        authorization = self.ports.authorize(intent, index)
        native = next(
            item
            for item in self.built.product.snapshot.files
            if item.path == self.built.product.snapshot.native_libraries[0]
        )
        path = self.service.store.path_for(native.blob)
        original = path.read_bytes()
        path.chmod(0o600)
        path.write_bytes(original + b"corruption")
        with self.assertRaises(BlobIntegrityError):
            self.intent()
        with self.assertRaises(BlobIntegrityError):
            self.ports.authorize(intent, index)
        with self.assertRaises(BlobIntegrityError):
            self.ports.finalize(intent, authorization)
