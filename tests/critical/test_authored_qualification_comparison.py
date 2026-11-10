"""Closed native observations with real Python verifiers and typed package data.

Project/lock/oracle/model planning, record capture, package-byte verification and
isolated verifier children are real. Project review, installed framework/provider,
model generation and package/tool custody admissions remain explicit fixtures.
"""

import json
import unittest
from dataclasses import replace

from literate_ai.adapters.authored_qualification_authority import (
    read_current_authored_qualification_authority,
)
from literate_ai.adapters.authored_qualification_comparison import (
    compare_authored_qualification,
)
from literate_ai.adapters.directory_artifacts import directory_export_bytes
from literate_ai.adapters.lifecycle import local_tree_identity
from literate_ai.adapters.packaging import DirectoryPackageAdapter
from literate_ai.adapters.qualification_capture import (
    QualificationCaptureError,
    QualificationEvidenceReader,
    QualificationEvidenceRecorder,
)
from literate_ai.application.artifact_graph import (
    create_artifact_build_graph,
    create_package_plan,
)
from literate_ai.contracts.authored_qualification_acceptance import (
    AuthoredGraphIndependentAcceptance,
)
from literate_ai.contracts.authored_qualification_comparisons import (
    AuthoredQualificationComparison,
)
from literate_ai.contracts.authored_qualification_profiles import (
    AuthoredQualificationProfile,
)
from literate_ai.contracts.executable_components.packages import PackageKind
from literate_ai.contracts.identity import canonical_identity
from literate_ai.contracts.product_json import product_json_bytes
from literate_ai.contracts.standard_root_integration import (
    StandardRootIntegrationEvidence,
)
from tests.critical import test_authored_graph_acceptance as native_fixtures
from tests.critical import test_authored_qualification_planning as plan_fixtures
from tests.support import fixtures_test_artifact_graph_contracts as artifact_fixtures
from tests.support.fixtures_test_schema_catalog import SchemaCatalog


class AuthoredQualificationComparisonTests(unittest.TestCase):
    def setUp(self):
        planning = plan_fixtures.AuthoredQualificationPlanningTests("runTest")
        planning.setUp()
        self.addCleanup(planning.doCleanups)
        self.planning = planning
        f = planning.fixture
        f.authority = read_current_authored_qualification_authority(
            f.root, planning.request
        )
        f.profile = AuthoredQualificationProfile.from_dict(
            json.loads(f.profile_path.read_bytes())
        )
        native = native_fixtures.AuthoredGraphAcceptanceTests("runTest")
        native.provided_fixture = f
        native.setUp()
        self.addCleanup(native.doCleanups)
        self.native = native
        self.current = planning.plan(pipeline_model="fixture-model")
        native.execution = self.current.plan.execution_plan
        native.ports.independent_acceptance_oracle = self.current.oracles
        lock = self.current.oracles.authority.locked.authority.lock
        factory = artifact_fixtures.ArtifactGraphTests("runTest")
        exports = {}
        payloads = {}
        manifests = []
        artifact_paths = {}
        for node in sorted(
            lock.nodes,
            key=lambda n: (
                n.revision.identity == lock.root_revision,
                n.revision.identity.uri,
            ),
        ):
            revision = node.revision.identity
            name = node.revision.coordinate.name
            dependencies = tuple(
                exports[e.provider_revision]
                for e in lock.edges
                if e.consumer_revision == revision
            )
            old_export = native.ports._planned_exports[revision.uri]
            path = native.artifact_paths[old_export.identity.uri]
            payload = directory_export_bytes(path)
            media_type = "application/vnd.literate-ai.python-package-tree"
            export = replace(
                factory.export(name, dependencies),
                component_revision=revision,
                role="library",
                target_identity=lock.target_profile_identity,
                source_tree_identity=local_tree_identity(path),
                media_type=media_type,
                blob=artifact_fixtures.blob(payload, media_type),
            )
            exports[revision] = export
            payloads[export.blob.identity] = payload
            artifact_paths[export.identity.uri] = path
            manifests.append(factory.manifest(export, dependencies))
            native.ports._planned_exports[revision.uri] = export
        native.artifact_paths = artifact_paths
        graph = create_artifact_build_graph(
            build_system_driver_identity=factory.driver,
            manifests=manifests,
            link_roots=(exports[lock.root_revision].identity,),
        )
        native.plan = create_package_plan(
            graph,
            root_component_revision=lock.root_revision,
            component_lock_identity=lock.identity,
            target_identity=lock.target_profile_identity,
            root_artifact_identity=exports[lock.root_revision].identity,
            package_kind=PackageKind.DIRECTORY,
            packager_identity=canonical_identity({"fixture-directory-packager": True}),
            destinations={
                e.identity.uri: "libraries/" + e.export_id + ".zip"
                for e in exports.values()
            },
            entrypoints=(),
        )
        native.result = DirectoryPackageAdapter().package(
            native.plan, read_blob=lambda blob: payloads[blob.identity]
        )
        self.recorder = QualificationEvidenceRecorder(
            max_bytes=2_000_000, max_records=500
        )
        accepted = native.accept(evidence_recorder=self.recorder)
        self.graph = AuthoredGraphIndependentAcceptance.from_dict(
            native.records[accepted]
        )
        self.root = StandardRootIntegrationEvidence(
            lock.identity,
            native.execution.identity,
            canonical_identity({"fixture-project-build": True}),
            graph,
            graph.link_plans[0],
            native.plan,
            native.result,
            canonical_identity({"root-test": True}),
            canonical_identity({"packaged-execution": True}),
            accepted,
        )
        self.run = canonical_identity({"fixture-run": 1})

    def reader(self, entries=None):
        return QualificationEvidenceReader(
            self.recorder.entries if entries is None else entries,
            max_bytes=2_000_000,
            max_records=500,
        )

    def compare(self, root=None, reader=None):
        return compare_authored_qualification(
            self.current,
            run_identity=self.run,
            root=root or self.root,
            reader=reader or self.reader(),
        )

    def replace_record(self, old, value):
        identity = self.recorder.remember_json(value)
        graph = replace(
            self.graph,
            nodes=tuple(
                replace(n, observation_identity=identity)
                if n.observation_identity == old
                else n
                for n in self.graph.nodes
            ),
        )
        graph_id = self.recorder.remember_json(graph.to_dict())
        return replace(self.root, independent_acceptance_identity=graph_id)

    def test_every_node_reopens_real_native_results_under_the_original_root(self):
        before = self.root.to_dict()
        comparison = self.compare()
        self.assertEqual(comparison.case_count, 2)
        self.assertEqual(comparison.nodes, self.graph.nodes)
        self.assertEqual(comparison.plan_identity, self.current.plan.identity)
        self.assertEqual(comparison.run_identity, self.run)
        observations = [
            self.reader().read_json(n.observation_identity)["observations"][0]
            for n in comparison.nodes
        ]
        self.assertEqual(len({o["case_identity"] for o in observations}), 1)
        self.assertEqual(len({n.observation_identity for n in comparison.nodes}), 2)
        self.assertEqual(self.root.to_dict(), before)
        self.assertEqual(
            AuthoredQualificationComparison.from_dict(comparison.to_dict()), comparison
        )
        SchemaCatalog().validate(comparison.SCHEMA, comparison.to_dict())
        self.assertNotIn("private-answer", json.dumps(comparison.to_dict()))

    def test_missing_provider_observation_cannot_be_replaced_by_aggregate_counts(self):
        root_revision = self.current.plan.execution_plan.root_revision
        provider = next(
            n
            for n in self.graph.nodes
            if n.component_revision_identity != root_revision
        )
        entries = tuple(
            e for e in self.recorder.entries if e[0] != provider.observation_identity
        )
        root_observation = next(
            n.observation_identity
            for n in self.graph.nodes
            if n.component_revision_identity == root_revision
        )
        self.assertTrue(self.reader(entries).read_json(root_observation))
        with self.assertRaises(QualificationCaptureError):
            self.compare(reader=self.reader(entries))

    def test_rehashed_wrong_result_or_missing_case_refuses_comparison(self):
        node = self.graph.nodes[0]
        record = self.reader().read_json(node.observation_identity)
        bad = self.recorder.remember_bytes(product_json_bytes({"wrong": True}))
        altered = {
            **record,
            "observations": [{**record["observations"][0], "result_identity": bad.uri}],
        }
        with self.assertRaises(QualificationCaptureError):
            self.compare(root=self.replace_record(node.observation_identity, altered))
        with self.assertRaises(QualificationCaptureError):
            self.compare(
                root=self.replace_record(
                    node.observation_identity, {**record, "observations": []}
                )
            )

        forged = replace(
            self.graph, nodes=tuple(replace(n, case_count=42) for n in self.graph.nodes)
        )
        forged_id = self.recorder.remember_json(forged.to_dict())
        with self.assertRaisesRegex(QualificationCaptureError, "case_map_mismatch"):
            self.compare(
                root=replace(self.root, independent_acceptance_identity=forged_id)
            )

    def test_provider_cannot_borrow_another_nodes_artifact(self):
        left, right = self.graph.nodes
        left_record = self.reader().read_json(left.observation_identity)
        right_record = self.reader().read_json(right.observation_identity)
        altered = {
            **left_record,
            "artifact_identity": right_record["artifact_identity"],
        }
        with self.assertRaisesRegex(QualificationCaptureError, "export_mismatch"):
            self.compare(root=self.replace_record(left.observation_identity, altered))

    def test_stale_original_oracle_and_dto_only_owner_refuse(self):
        with self.assertRaises(TypeError):
            compare_authored_qualification(
                self.current.plan,
                run_identity=self.run,
                root=self.root,
                reader=self.reader(),
            )
        path = self.planning.fixture.oracle_root / "harness.py"
        path.write_bytes(path.read_bytes() + b"\n")
        with self.assertRaises(ValueError):
            self.compare()

    def test_closed_comparison_does_not_accept_source_history_or_empty_nodes(self):
        comparison = self.compare()
        for field in (
            "source_snapshot_identity",
            "source_baseline_identity",
            "promotion_tree_identity",
            "passed",
        ):
            with self.assertRaises(ValueError):
                AuthoredQualificationComparison.from_dict(
                    {**comparison.to_dict(), field: True}
                )
        with self.assertRaises(ValueError):
            replace(comparison, nodes=())
