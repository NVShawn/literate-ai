"""Native JavaScript function-oracle execution and exact launcher custody."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from dataclasses import fields
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
    LocalStandardLifecycleError,
    run_with_tree_kill,
)
from literate_ai.adapters.standard_project import (
    project_locked_standard_toolchain_closure,
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
from tests.support import fixtures_test_artifact_graph_contracts as graph_fixtures
from tests.support import fixtures_test_package_release_contracts as package_fixtures
from tests.support.fixtures_test_standard_command_projection import (
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

    def test_incorrect_function_result_is_rejected(self):
        with self.assertRaisesRegex(LocalStandardLifecycleError, "result differs"):
            self.accept(self.ports(), "return a+b+1;")

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
