"""Native JavaScript function-oracle execution and exact launcher custody."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from dataclasses import fields, replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from literate_ai.adapters.component_acceptance import (
    DeclaredLibraryAcceptanceCase,
    LibraryAcceptance,
)
from literate_ai.adapters.directory_artifacts import directory_export_bytes
from literate_ai.adapters.lifecycle import (
    LocalSourceTreeRegistry,
    LocalStandardLifecyclePorts,
    local_tree_identity,
)
from literate_ai.adapters.lifecycle.standard_local import (
    LocalComponentToolBinding,
    LocalStandardLifecycleError,
    run_with_tree_kill,
)
from literate_ai.adapters.qualification_capture import (
    QualificationCaptureError,
    QualificationEvidenceReader,
    QualificationEvidenceRecorder,
    verify_qualification_library_acceptance,
)
from literate_ai.adapters.standard_project import (
    project_locked_standard_toolchain_closure,
    project_standard_toolchain_closure,
)
from literate_ai.application.artifact_graph import (
    create_artifact_build_graph,
    create_package_plan,
)
from literate_ai.contracts import BlobRef, ContentIdentity, canonical_identity
from literate_ai.contracts.executable_components import (
    ArtifactExport,
    PackageKind,
)
from literate_ai.contracts.library_products import LibraryArtifactProduct
from literate_ai.contracts.standard_root_integration import (
    StandardRootIntegrationEvidence,
)
from tests.unit import test_artifact_graph_contracts as graph_fixtures
from tests.unit import test_package_release_contracts as package_fixtures
from tests.unit.test_standard_command_projection import (
    _locked_snapshot,
    _observation,
    _tool,
)

_HARNESS = b"""const path=require('node:path');
const [artifact,surfaceText,casesText]=process.argv.slice(2);
const surface=JSON.parse(surfaceText), cases=JSON.parse(casesText);
const observed=cases.map(c=>{
  const cap=surface.capabilities.find(x=>x.capability===c.capability);
  const library=require(path.join(artifact,'source',cap.module+'.js'));
  const result=library[cap.symbols[0]](...c.arguments);
  return {case_id:c.case_id,capability:c.capability,result};
});
process.stdout.write(JSON.stringify({schema:'literate-ai/library-acceptance-results@1',cases:observed}));
"""


class JavaScriptLibraryAcceptanceTests(unittest.TestCase):
    def setUp(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is unavailable")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        _, self.snapshot, self.execution = _locked_snapshot(
            self.root, language="javascript", no_entrypoint=True
        )
        self.node_changed = False

        def require_node_unchanged():
            if self.node_changed:
                raise LocalStandardLifecycleError("Node observation changed")

        def discover(name, _constraint, _environment):
            tool = _tool(name)
            if name == "node":
                tool.command = (str(Path(node).resolve()),)
                tool.require_unchanged = require_node_unchanged
            return tool

        self.closure = project_locked_standard_toolchain_closure(
            self.snapshot,
            self.execution,
            host_platform="macos",
            toolchain_discoverer=discover,
            dependency_observer=_observation,
        )
        self.contract = self.closure.contracts[0]
        self.surface = self.contract.library_import_surface
        assert self.surface is not None
        self.capability = self.surface.capabilities[0]
        self.custody_root = self.root / "sealed-package"
        self.export = self.custody_root / self.contract.artifact_export.export_id
        self.module = self.export / "source" / (self.capability.module + ".js")
        self.module.parent.mkdir(parents=True)
        root_node = next(
            node
            for node in self.snapshot.authority.lock.nodes
            if node.revision.identity == self.snapshot.authority.lock.root_revision
        )
        self.oracle = LibraryAcceptance(
            root_node.revision.coordinate.name,
            root_node.revision.specification_set_identity,
            tuple(
                sorted(
                    (item.identity for item in root_node.revision.public_interfaces),
                    key=lambda item: item.uri,
                )
            ),
            self.surface.identity,
            "javascript",
            ContentIdentity.parse_uri("sha256:" + hashlib.sha256(_HARNESS).hexdigest()),
            _HARNESS,
            (
                DeclaredLibraryAcceptanceCase(
                    "adds-values", self.capability.capability, [2, 3], 5
                ),
            ),
        )

    def ports(self, bindings=None):
        return LocalStandardLifecyclePorts(
            source_trees=LocalSourceTreeRegistry(),
            object_root=self.root / "objects",
            contracts=self.closure.contracts,
            tool_bindings=self.closure.tool_bindings if bindings is None else bindings,
            independent_acceptance_oracle=self.oracle,
        )

    def accept(self, ports, implementation="return a+b;"):
        self.module.write_text(
            "exports["
            + json.dumps(self.capability.symbols[0])
            + "] = (a,b) => { "
            + implementation
            + " };\n",
            encoding="utf-8",
        )
        declaration = self.contract.artifact_export
        content = directory_export_bytes(self.export)
        artifact = ArtifactExport(
            **{
                field.name: getattr(declaration, field.name)
                for field in fields(declaration)
            },
            component_revision=self.contract.component_revision,
            source_tree_identity=local_tree_identity(self.export),
            toolchain_identity=self.contract.language_compiler_identity,
            authorization_identity=canonical_identity("authored-oracle-fixture"),
            dependency_artifact_identities=(),
            blob=BlobRef(
                hashlib.sha256(content).hexdigest(),
                len(content),
                media_type=declaration.media_type,
            ),
        )
        artifact_identity = artifact.identity
        fixture = graph_fixtures.ArtifactGraphTests()
        graph = create_artifact_build_graph(
            build_system_driver_identity=fixture.driver,
            manifests=(fixture.manifest(artifact),),
            link_roots=(artifact.identity,),
        )
        plan = create_package_plan(
            graph,
            root_component_revision=artifact.component_revision,
            component_lock_identity=self.execution.component_lock_identity,
            target_identity=artifact.target_identity,
            root_artifact_identity=artifact.identity,
            package_kind=PackageKind.DIRECTORY,
            packager_identity=canonical_identity("native-oracle-fixture-packager"),
            destinations={artifact.identity.uri: artifact.export_id},
            entrypoints=(),
            runtime_requirements=(),
        )
        package = package_fixtures.PackageReleaseContractTests().result(plan)
        ports._planned_exports[self.snapshot.authority.lock.root_revision.uri] = (
            artifact
        )
        custody = SimpleNamespace(
            root=self.custody_root,
            artifact_paths={artifact_identity.uri: self.export},
            tree_identity=local_tree_identity(self.custody_root),
        )
        with mock.patch.object(ports, "project_package_custody", return_value=custody):
            evidence = ports.accept_project_independently(
                self.snapshot.authority.lock,
                self.execution,
                None,
                plan,
                package,
                canonical_identity({"root-test": True}),
                canonical_identity({"packaged-execution": True}),
            )
        self.assertEqual(local_tree_identity(self.custody_root), custody.tree_identity)
        self.accepted_product = LibraryArtifactProduct(artifact, self.surface)
        self.accepted_root = StandardRootIntegrationEvidence(
            component_lock_identity=plan.component_lock_identity,
            execution_plan_identity=self.execution.identity,
            project_build_plan_identity=canonical_identity(
                "native-oracle-fixture-build-plan"
            ),
            artifact_graph=graph,
            link_plan=graph.link_plans[0],
            package_plan=plan,
            package_result=package,
            root_generated_integration_test_identity=canonical_identity(
                {"root-test": True}
            ),
            packaged_execution_identity=canonical_identity(
                {"packaged-execution": True}
            ),
            independent_acceptance_identity=evidence,
        )
        return evidence

    def test_native_node_calls_function_in_independent_verifier(self):
        self.assertTrue(self.accept(self.ports()).uri.startswith("sha256:"))

    def test_fractional_library_inputs_results_and_evidence(self):
        self.oracle = replace(
            self.oracle,
            cases=(
                DeclaredLibraryAcceptanceCase(
                    "fraction",
                    self.capability.capability,
                    [1.25, 0.25],
                    1.5,
                ),
            ),
        )
        ports = self.ports()
        recorder = QualificationEvidenceRecorder(max_bytes=256_000, max_records=100)
        ports.retain_evidence_with(recorder)
        evidence = self.accept(ports)
        reader = QualificationEvidenceReader(
            recorder.entries, max_bytes=256_000, max_records=100
        )
        verify_qualification_library_acceptance(
            reader,
            root=self.accepted_root,
            product=self.accepted_product,
            component_lock=self.snapshot.authority.lock,
            oracle=self.oracle,
        )
        document = reader.read_json(evidence)
        result_identity = ContentIdentity.parse_uri(
            document["observations"][0]["result_identity"]
        )
        self.assertEqual(json.loads(reader.read_bytes(result_identity)), 1.5)
        with self.assertRaises(QualificationCaptureError):
            reader.read_json(result_identity)
        with self.assertRaisesRegex(
            LocalStandardLifecycleError, "differs for 'fraction'"
        ):
            self.accept(self.ports(), "return a+b+0.25;")

    def test_mutated_nonfinite_oracle_refuses_before_harness(self):
        self.oracle.cases[0].arguments.append(float("nan"))
        with mock.patch(
            "literate_ai.adapters.lifecycle.standard_local.run_with_tree_kill"
        ) as execute:
            with self.assertRaisesRegex(ValueError, "must be finite"):
                self.accept(self.ports())
        execute.assert_not_called()

    def test_integral_numeric_result_retains_and_reopens_its_actual_encoding(self):
        self.oracle = replace(
            self.oracle,
            cases=(
                DeclaredLibraryAcceptanceCase(
                    "integral-result", self.capability.capability, [0.75, 0.25], 1.0
                ),
            ),
        )
        ports = self.ports()
        recorder = QualificationEvidenceRecorder(max_bytes=256_000, max_records=100)
        ports.retain_evidence_with(recorder)
        evidence = self.accept(ports)
        reader = QualificationEvidenceReader(
            recorder.entries, max_bytes=256_000, max_records=100
        )
        verify_qualification_library_acceptance(
            reader,
            root=self.accepted_root,
            product=self.accepted_product,
            component_lock=self.snapshot.authority.lock,
            oracle=self.oracle,
        )
        document = reader.read_json(evidence)
        actual_identity = ContentIdentity.parse_uri(
            document["observations"][0]["result_identity"]
        )
        self.assertEqual(reader.read_bytes(actual_identity), b"1")
        self.assertEqual(actual_identity, canonical_identity(1))
        for implementation in ("return a+b+Number.EPSILON;", "return true;"):
            with (
                self.subTest(implementation=implementation),
                self.assertRaisesRegex(
                    LocalStandardLifecycleError, "differs for 'integral-result'"
                ),
            ):
                self.accept(self.ports(), implementation)

    def test_native_acceptance_retains_oracle_harness_and_observed_result(self):
        ports = self.ports()
        recorder = QualificationEvidenceRecorder(max_bytes=256_000, max_records=100)
        ports.retain_evidence_with(recorder)
        evidence = self.accept(ports)
        records = dict(recorder.entries)
        document = json.loads(records[evidence])
        self.assertEqual(document["oracle_identity"], self.oracle.identity.uri)
        self.assertEqual(records[self.oracle.harness_identity], _HARNESS)
        self.assertEqual(
            json.loads(records[self.oracle.identity]), self.oracle.identity_document()
        )
        self.assertEqual(
            json.loads(records[self.surface.identity]), self.surface.to_dict()
        )
        observed = document["observations"][0]
        result_identity = ContentIdentity.parse_uri(observed["result_identity"])
        self.assertEqual(json.loads(records[result_identity]), 5)

    def test_reopened_native_oracle_requires_all_exact_records_and_observations(self):
        ports = self.ports()
        recorder = QualificationEvidenceRecorder(max_bytes=256_000, max_records=100)
        ports.retain_evidence_with(recorder)
        evidence = self.accept(ports)
        arguments = dict(
            root=self.accepted_root,
            product=self.accepted_product,
            component_lock=self.snapshot.authority.lock,
            oracle=self.oracle,
        )
        # The actual Node invocation has completed. Reopening works after the
        # package and temporary harness directories are removed, without tools.
        shutil.rmtree(self.custody_root)
        del ports
        reader = QualificationEvidenceReader(
            recorder.entries, max_bytes=256_000, max_records=100
        )
        verify_qualification_library_acceptance(reader, **arguments)
        document = reader.read_json(evidence)
        case = document["observations"][0]
        for missing in (
            self.oracle.identity,
            self.oracle.harness_identity,
            self.surface.identity,
            ContentIdentity.parse_uri(case["case_identity"]),
            ContentIdentity.parse_uri(case["result_identity"]),
            evidence,
        ):
            incomplete = QualificationEvidenceReader(
                tuple(item for item in recorder.entries if item[0] != missing),
                max_bytes=256_000,
                max_records=100,
            )
            with (
                self.subTest(missing=missing),
                self.assertRaisesRegex(QualificationCaptureError, "record-missing"),
            ):
                verify_qualification_library_acceptance(incomplete, **arguments)
        recorder.remember_json(6)
        for changes in (
            {"package_result_identity": canonical_identity("other-package").uri},
            {"observations": []},
            {"observations": [case, case]},
            {"observations": [{**case, "result_identity": canonical_identity(6).uri}]},
            {"accepted": True},
        ):
            forged = recorder.remember_json({**document, **changes})
            substituted = replace(
                self.accepted_root, independent_acceptance_identity=forged
            )
            reader = QualificationEvidenceReader(
                recorder.entries, max_bytes=256_000, max_records=100
            )
            with (
                self.subTest(changes=changes),
                self.assertRaisesRegex(
                    QualificationCaptureError, "oracle-observation-mismatch"
                ),
            ):
                verify_qualification_library_acceptance(
                    reader, **{**arguments, "root": substituted}
                )
        for raw in (b"5.0\n", b"5e0", b"NaN", b"true", b"5.000000000000001"):
            result_identity = recorder.remember_bytes(raw)
            forged = recorder.remember_json(
                {
                    **document,
                    "observations": [{**case, "result_identity": result_identity.uri}],
                }
            )
            reader = QualificationEvidenceReader(
                recorder.entries, max_bytes=256_000, max_records=100
            )
            substituted = replace(
                self.accepted_root, independent_acceptance_identity=forged
            )
            with self.subTest(raw=raw), self.assertRaises(QualificationCaptureError):
                verify_qualification_library_acceptance(
                    reader, **{**arguments, "root": substituted}
                )
        with self.assertRaisesRegex(
            QualificationCaptureError, "oracle-authority-mismatch"
        ):
            verify_qualification_library_acceptance(
                reader,
                **{
                    **arguments,
                    "oracle": replace(
                        self.oracle,
                        specification_set_identity=canonical_identity("other-spec"),
                    ),
                },
            )

    def test_capture_rejects_harness_identity_drift_before_execution(self):
        self.oracle = replace(self.oracle, harness_identity=canonical_identity("other"))
        ports = self.ports()
        ports.retain_evidence_with(
            QualificationEvidenceRecorder(max_bytes=256_000, max_records=100)
        )
        with mock.patch(
            "literate_ai.adapters.lifecycle.standard_local.run_with_tree_kill"
        ) as execute:
            with self.assertRaisesRegex(LocalStandardLifecycleError, "harness differs"):
                self.accept(ports)
        execute.assert_not_called()

    def test_incorrect_function_result_is_rejected(self):
        with self.assertRaisesRegex(LocalStandardLifecycleError, "result differs"):
            self.accept(self.ports(), "return a+b+1;")

    def test_correct_result_cannot_hide_package_mutation(self):
        with self.assertRaisesRegex(
            LocalStandardLifecycleError, "mutated immutable package custody"
        ):
            self.accept(
                self.ports(),
                "require('node:fs').writeFileSync(__dirname+'/mutated','x'); "
                "return a+b;",
            )

    def test_missing_substituted_or_extra_bindings_refuse_at_both_boundaries(self):
        bindings = self.closure.tool_bindings
        compiler = self.contract.language_compiler_identity
        selected = next(
            item for item in bindings if item.toolchain_identity == compiler
        )
        missing = tuple(item for item in bindings if item is not selected)
        substituted = (
            *missing,
            replace(
                selected, authority_identity=canonical_identity({"substituted": True})
            ),
        )
        extra = (
            *bindings,
            LocalComponentToolBinding.from_observed_toolchain(_tool("unexpected")),
        )
        for label, changed in (
            ("missing", missing),
            ("substituted", substituted),
            ("extra", extra),
        ):
            with self.subTest(label=label):
                with self.assertRaisesRegex(ValueError, "every and only"):
                    self.ports(changed)
                with self.assertRaisesRegex(ValueError, "every and only"):
                    project_standard_toolchain_closure(
                        self.execution,
                        contracts=self.closure.contracts,
                        tool_bindings=changed,
                        dependency_observation=self.closure.dependency_observation,
                        observer_identity=canonical_identity(
                            {"fixture-observer": True}
                        ),
                        toolchain_authorities=self.closure.toolchain_authorities,
                    )

    def test_changed_launcher_refuses_before_oracle_execution(self):
        ports = self.ports()
        self.node_changed = True
        with (
            mock.patch(
                "literate_ai.adapters.lifecycle.standard_local.run_with_tree_kill"
            ) as runner,
            self.assertRaisesRegex(
                LocalStandardLifecycleError,
                "observed local toolchain changed after binding",
            ),
        ):
            self.accept(ports)
        runner.assert_not_called()

    def test_launcher_drift_during_oracle_execution_cannot_issue_evidence(self):
        ports = self.ports()

        def run_then_change(*args, **kwargs):
            result = run_with_tree_kill(*args, **kwargs)
            self.node_changed = True
            return result

        with (
            mock.patch(
                "literate_ai.adapters.lifecycle.standard_local.run_with_tree_kill",
                side_effect=run_then_change,
            ),
            self.assertRaisesRegex(
                LocalStandardLifecycleError,
                "observed local toolchain changed after binding",
            ),
        ):
            self.accept(ports)
