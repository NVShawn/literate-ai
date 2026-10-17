"""FINALIZE result descriptors reject substituted request/package and missing proof."""

import unittest
from dataclasses import replace

import tests.support.fixtures_test_action_finalize_record as fixture_module
from literate_ai.adapters.action_dispatch_wire import record_identity
from literate_ai.adapters.action_finalize_result_record import FinalizeWorkerResult
from literate_ai.adapters.action_package_execution import PackageWorkerResult
from literate_ai.contracts import canonical_identity
from literate_ai.contracts.blobs import BlobRef
from literate_ai.contracts.standard_root_integration import (
    StandardRootIntegrationEvidence,
)


class FinalizeResultTests(unittest.TestCase):
    def setUp(self):
        f = self.fixture = fixture_module.FinalizeWorkerInputTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        package = f.value.package_input
        self.value = replace(
            f.value,
            package_result_identity=record_identity(
                PackageWorkerResult(
                    record_identity(package.to_bytes()), f.package
                ).to_bytes()
            ),
        )
        self.raw = self.value.to_bytes()
        self.identity = record_identity(self.raw)
        evidence = StandardRootIntegrationEvidence(
            self.value.component_lock.identity,
            package.execution_plan.identity,
            self.value.project_plan.identity,
            package.artifact_graph,
            next(
                link
                for link in package.artifact_graph.link_plans
                if link.identity == package.plan.link_plan_identity
            ),
            package.plan,
            f.package,
            canonical_identity("test"),
            canonical_identity("execute"),
            canonical_identity("accept"),
        )
        refs = tuple(
            sorted(
                (
                    BlobRef(identity.digest, 1)
                    for identity in (
                        evidence.identity,
                        evidence.root_generated_integration_test_identity,
                        evidence.packaged_execution_identity,
                        evidence.independent_acceptance_identity,
                    )
                ),
                key=lambda ref: ref.identity,
            )
        )
        self.result = FinalizeWorkerResult(self.identity, evidence, refs)

    def admit(self, result=None, raw=None, input_record=None):
        content = raw if raw is not None else (result or self.result).to_bytes()
        request = input_record if input_record is not None else self.raw
        return FinalizeWorkerResult.admit(
            content,
            record_identity(content),
            input_record=request,
            input_identity=record_identity(request),
            deadline=self.fixture.deadline,
        )
