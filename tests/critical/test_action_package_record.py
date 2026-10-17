"""PACKAGE descriptors preserve exact plan intent without asserting LINK proof."""

import json
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import tests.support.fixtures_test_standard_artifact_assembly as fixture_module
from literate_ai.adapters.action_dispatch_wire import (
    ActionDispatchDeadline,
    ActionWireError,
    record_identity,
)
from literate_ai.adapters.action_package_record import PackageWorkerInput
from literate_ai.application.action_dag_planning import plan_lifecycle_action_dag
from literate_ai.application.action_dag_scheduler import LifecycleActionKind
from literate_ai.contracts import canonical_identity, canonical_json_bytes


class PackageWorkerInputTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture = fixture_module.StandardArtifactAssemblyTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        execution = fixture.fixture.execution
        root = fixture.result.root_integration
        package = next(
            node
            for node in plan_lifecycle_action_dag(execution, worker_ids=("package",))
            if node.kind is LifecycleActionKind.PACKAGE
        )
        self.value = PackageWorkerInput(
            execution,
            root.artifact_graph,
            root.package_plan,
            tuple(
                (action, canonical_identity(action))
                for action in package.predecessor_ids
            ),
        )
        self.deadline = ActionDispatchDeadline(datetime.now(UTC) + timedelta(minutes=1))

    def admit(self, value=None, raw=None):
        raw = (value or self.value).to_bytes() if raw is None else raw
        return PackageWorkerInput.admit(raw, record_identity(raw), self.deadline)

    def test_exact_plan_roundtrips_with_all_link_references(self):
        self.assertEqual(self.admit(), self.value)
        self.assertGreater(len(self.value.link_results), 1)

    def test_missing_duplicate_foreign_and_reordered_links_refuse(self):
        links = self.value.link_results
        for changed in (
            links[:-1],
            (*links, links[0]),
            tuple(reversed(links)),
            (("foreign", links[0][1]), *links[1:]),
            ((links[0][0], links[1][1]), *links[1:]),
        ):
            with self.subTest(links=changed), self.assertRaises(ActionWireError):
                self.admit(replace(self.value, link_results=changed))

    def test_substituted_package_authority_refuses_even_when_rehashed(self):
        for field in (
            "component_lock_identity",
            "root_component_revision",
            "artifact_graph_identity",
            "link_plan_identity",
        ):
            with self.subTest(field=field), self.assertRaises(ActionWireError):
                self.admit(
                    replace(
                        self.value,
                        plan=replace(
                            self.value.plan, **{field: canonical_identity("foreign")}
                        ),
                    )
                )

    def test_substituted_artifact_bytes_refuse(self):
        original = self.value.plan.inputs[0]
        changed = replace(original, blob=replace(original.blob, digest="0" * 64))
        with self.assertRaises(ActionWireError):
            self.admit(
                replace(
                    self.value,
                    plan=replace(
                        self.value.plan, inputs=(changed, *self.value.plan.inputs[1:])
                    ),
                )
            )

    def test_unknown_fields_noncanonical_bytes_and_expiry_refuse(self):
        doc = json.loads(self.value.to_bytes())
        with self.assertRaises(ActionWireError):
            self.admit(raw=canonical_json_bytes(doc | {"extra": True}))
        with self.assertRaises(ActionWireError):
            self.admit(raw=json.dumps(doc, indent=2).encode())
        expired = ActionDispatchDeadline(datetime.now(UTC) - timedelta(seconds=1))
        raw = self.value.to_bytes()
        with self.assertRaises(ActionWireError):
            PackageWorkerInput.admit(raw, record_identity(raw), expired)

    def test_explicit_resource_and_executable_intent_survive_roundtrip(self):
        from literate_ai.contracts import BlobRef
        from literate_ai.contracts.executable_components import (
            PackageFileKind,
            PackageInput,
        )

        plan = self.value.plan
        resource = PackageInput(
            "share/helper",
            "helper",
            PackageFileKind.RESOURCE,
            canonical_identity("resource"),
            plan.target_identity,
            BlobRef("1" * 64, 12),
            executable=True,
        )
        changed = replace(
            self.value,
            plan=replace(
                plan,
                inputs=tuple(
                    sorted((*plan.inputs, resource), key=lambda item: item.path)
                ),
            ),
        )
        self.assertEqual(self.admit(changed), changed)
