"""Real isolated node verifiers with injected package/tool admission boundaries.

Project/lock/oracle reads and Python child execution are real. Package custody
and native tool observations are fixtures; this is not full Standard generation,
installed acceptance or retained consumption.
"""

import hashlib
import json
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters.component_acceptance import load_versioned_library_acceptance
from literate_ai.adapters.lifecycle import (
    LocalSourceTreeRegistry,
    LocalStandardLifecyclePorts,
    local_tree_identity,
)
from literate_ai.adapters.lifecycle.standard_local import LocalStandardLifecycleError
from literate_ai.adapters.standard_project import (
    project_locked_standard_toolchain_closure,
)
from literate_ai.application.component_execution_planning import (
    plan_component_execution,
)
from literate_ai.contracts.authored_qualification_acceptance import (
    AUTHORED_GRAPH_ACCEPTANCE_SCHEMA,
    AuthoredGraphIndependentAcceptance,
)
from literate_ai.contracts.authored_qualification_profiles import (
    AuthoredQualificationOracleSelection,
)
from literate_ai.contracts.identity import canonical_identity
from tests.critical import test_authored_qualification_oracles as oracle_fixtures
from tests.support.fixtures_test_schema_catalog import SchemaCatalog
from tests.support.fixtures_test_standard_command_projection import _observation, _tool

HARNESS = b"""import importlib,json,pathlib,sys
assert sys.flags.isolated and sys.flags.no_site and sys.flags.dont_write_bytecode
export=pathlib.Path(sys.argv[1]).resolve()
surface=json.loads(sys.argv[2]);cases=json.loads(sys.argv[3])
sys.path.insert(0,str(export/'source'))
observed=[]
for case in cases:
 capability=next(c for c in surface['capabilities']
                 if c['capability']==case['capability'])
 module=importlib.import_module(capability['module'])
 assert pathlib.Path(module.__file__).resolve().is_relative_to(export)
 value=getattr(module,capability['symbols'][0])(*case['arguments'])
 observed.append({'case_id':case['case_id'],'capability':case['capability'],'result':value})
print(json.dumps({'schema':'literate-ai/library-acceptance-results@1','cases':observed}))
"""


class AuthoredGraphAcceptanceTests(unittest.TestCase):
    def setUp(self):
        fixture = getattr(self, "provided_fixture", None)
        if fixture is None:
            fixture = oracle_fixtures.AuthoredQualificationOracleTests("runTest")
            fixture.setUp()
            self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.root = fixture.root
        harness = fixture.oracle_root / "harness.py"
        harness.write_bytes(HARNESS)
        selections = []
        for selected in fixture.profile.oracles:
            path = fixture.oracle_root / (selected.component.name + ".json")
            document = json.loads(path.read_bytes())
            document["harness_identity"] = (
                "sha256:" + hashlib.sha256(HARNESS).hexdigest()
            )
            path.write_text(json.dumps(document))
            oracle = load_versioned_library_acceptance(path, selected.component.name)
            selections.append(
                AuthoredQualificationOracleSelection(
                    selected.component, oracle.identity
                )
            )
        fixture.write_profile(replace(fixture.profile, oracles=tuple(selections)))
        self.oracles = fixture.read()
        self.snapshot = fixture.authority.locked
        self.execution = plan_component_execution(
            self.snapshot.authority.lock,
            model_identities={
                node.revision.identity.uri: canonical_identity(
                    {"fixture-model": node.revision.identity.uri}
                )
                for node in self.snapshot.authority.lock.nodes
            },
        )
        closure = project_locked_standard_toolchain_closure(
            self.snapshot,
            self.execution,
            host_platform="macos",
            toolchain_discoverer=lambda name, *_: _tool(name),
            dependency_observer=_observation,
        )
        self.ports = LocalStandardLifecyclePorts(
            source_trees=LocalSourceTreeRegistry(),
            object_root=self.root / "objects",
            contracts=closure.contracts,
            tool_bindings=closure.tool_bindings,
            independent_acceptance_oracle=self.oracles,
        )
        self.package_root = self.root / "sealed"
        self.artifact_paths = {}
        self.modules = {}
        self.contracts = {c.component_revision: c for c in closure.contracts}
        for contract in closure.contracts:
            capability = contract.library_import_surface.capabilities[0]
            export = self.package_root / contract.component_revision.digest
            module = (
                export
                / "source"
                / Path(*capability.module.split(".")).with_suffix(".py")
            )
            module.parent.mkdir(parents=True)
            module.write_text(
                "def value(argument):\n return "
                + repr({"private": oracle_fixtures.PRIVATE})
                + "\n"
            )
            identity = canonical_identity(
                {"fixture-export": contract.component_revision.uri}
            )
            self.ports._planned_exports[contract.component_revision.uri] = (
                SimpleNamespace(identity=identity)
            )
            self.artifact_paths[identity.uri] = export
            self.modules[contract.component_revision] = module
        self.plan = SimpleNamespace(
            identity=canonical_identity({"fixture-package-plan": True}),
            component_lock_identity=self.snapshot.authority.lock.identity,
        )
        self.result = SimpleNamespace(
            identity=canonical_identity({"fixture-package-result": True})
        )
        self.records = {}

    def accept(self, *, evidence_recorder=None):
        if evidence_recorder is not None:
            self.ports.retain_evidence_with(evidence_recorder)
        custody = SimpleNamespace(
            root=self.package_root,
            artifact_paths=self.artifact_paths,
            tree_identity=local_tree_identity(self.package_root),
        )

        def record(value):
            identity = canonical_identity(value)
            self.records[identity] = value
            if evidence_recorder is not None:
                self.assertEqual(evidence_recorder.remember_json(value), identity)
            return identity

        with (
            patch.object(self.ports, "project_package_custody", return_value=custody),
            patch.object(self.ports, "_record_evidence", side_effect=record),
        ):
            return self.ports.accept_project_independently(
                self.snapshot.authority.lock,
                self.execution,
                None,
                self.plan,
                self.result,
                canonical_identity({"root-test": True}),
                canonical_identity({"packaged-execution": True}),
            )

    def test_every_node_runs_its_real_isolated_verifier_under_the_original_lock(self):
        original_lock = self.snapshot.authority.lock.to_dict()
        evidence = self.accept()
        graph = AuthoredGraphIndependentAcceptance.from_dict(self.records[evidence])
        SchemaCatalog().validate(AUTHORED_GRAPH_ACCEPTANCE_SCHEMA, graph.to_dict())
        self.assertEqual(graph.identity, evidence)
        self.assertEqual(len(graph.nodes), 2)
        self.assertEqual(sum(n.case_count for n in graph.nodes), 2)
        self.assertEqual(self.snapshot.authority.lock.to_dict(), original_lock)
        self.assertEqual(graph.origin, self.fixture.authority.origin)
        for node in graph.nodes:
            observed = self.records[node.observation_identity]
            self.assertEqual(observed["oracle_identity"], node.oracle_identity.uri)
            self.assertEqual(len(observed["observations"]), node.case_count)
            self.assertEqual(observed["package_plan_identity"], self.plan.identity.uri)
        self.assertFalse(list(self.package_root.rglob("__pycache__")))

    def test_passive_evidence_rejects_foreign_origin_and_source_promotion_fields(self):
        evidence = self.accept()
        document = self.records[evidence]
        catalog = SchemaCatalog()
        for replacement in (
            {**document, "source_promotion_identity": self.plan.identity.to_dict()},
            {**document, "origin": {"kind": "source-derived"}},
            {**document, "nodes": []},
            {
                **document,
                "nodes": [{**document["nodes"][0], "case_count": True}],
            },
        ):
            with self.subTest(value=replacement):
                with self.assertRaises(ValueError):
                    AuthoredGraphIndependentAcceptance.from_dict(replacement)
                with self.assertRaises(AssertionError):
                    catalog.validate(AUTHORED_GRAPH_ACCEPTANCE_SCHEMA, replacement)

    def test_successful_root_cannot_hide_a_failed_provider(self):
        root_revision = self.snapshot.authority.lock.root_revision
        provider = next(rev for rev in self.modules if rev != root_revision)
        self.modules[provider].write_text("def value(argument):\n return 'wrong'\n")
        with self.assertRaisesRegex(LocalStandardLifecycleError, "result differs"):
            self.accept()
        self.assertFalse(
            any(
                r.get("schema") == AUTHORED_GRAPH_ACCEPTANCE_SCHEMA
                for r in self.records.values()
            )
        )

    def test_missing_or_duplicate_binding_refuses_before_any_native_case(self):
        original = self.oracles
        for bindings in (
            original.bindings[:1],
            original.bindings + original.bindings[:1],
        ):
            with self.subTest(count=len(bindings)):
                self.ports.independent_acceptance_oracle = replace(
                    original, bindings=bindings
                )
                with self.assertRaisesRegex(ValueError, "membership_mismatch"):
                    self.accept()
                self.assertEqual(self.records, {})

    def test_stale_private_oracle_cannot_publish_graph_success(self):
        path = self.fixture.oracle_root / "harness.py"
        path.write_bytes(path.read_bytes() + b"\n")
        with self.assertRaises(ValueError):
            self.accept()
        self.assertFalse(
            any(
                r.get("schema") == AUTHORED_GRAPH_ACCEPTANCE_SCHEMA
                for r in self.records.values()
            )
        )
