"""All-node private oracle custody with real authored project/lock readers."""

import hashlib
import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from literate_ai.adapters.authored_qualification_authority import (
    read_current_authored_qualification_authority,
)
from literate_ai.adapters.authored_qualification_oracles import (
    AuthoredQualificationOracleError,
    read_current_authored_qualification_oracles,
)
from literate_ai.adapters.component_acceptance import load_versioned_library_acceptance
from literate_ai.adapters.repository_lineage import FilesystemRepositoryLineageStore
from literate_ai.application.generation_preparation import GenerationPreparationRequest
from literate_ai.contracts import RepositoryLineage, RepositoryParentSelection
from literate_ai.contracts._validation import ContractValidationError
from literate_ai.contracts.authored_qualification_profiles import (
    AuthoredQualificationOracleSelection,
    AuthoredQualificationProfile,
)
from literate_ai.contracts.authoring_markdown import (
    parse_authoring_markdown,
    render_authoring_markdown,
)
from literate_ai.contracts.executable_components.commands import (
    LibraryCapabilityImport,
    LibraryImportSurface,
)
from literate_ai.projects import PinnedInputClosureError, ProjectConfigurationStore
from literate_ai.schema_catalog import verify_schema_catalog
from tests.support.fixtures_test_component_lock_planning import _fixture
from tests.support.fixtures_test_locked_generation_authority import (
    _SELECTORS,
    _TARGET,
    _write_lock,
)
from tests.support.fixtures_test_project_agent_development_workflow import _definition

PRIVATE = "verifier-private-answer-5731"


class AuthoredQualificationOracleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        component, self.flavors = _fixture(self.root)
        catalog = self.root / "components"
        catalog.mkdir()
        self.component = Path(shutil.move(component, catalog / "greeting"))
        shutil.copytree(self.component, catalog / "provider")
        for name in ("greeting", "provider"):
            node_root = catalog / name
            path = node_root / "component.md"
            document, body = parse_authoring_markdown(
                path.read_bytes(), source=str(path)
            )
            document["kind"] = "library"
            document["entrypoints"] = []
            document["provides"] = [
                {
                    "name": name + ".value",
                    "version": "1.0.0",
                    "interface": {"uri": "interfaces/value.md", "pin": None},
                }
            ]
            if name == "greeting":
                document["requires"] = [
                    {
                        "requirement_id": "provider",
                        "capability": "provider.value",
                        "version_range": ">=1,<2",
                        "dependency_kind": "generation",
                        "optional": False,
                        "constraints": [],
                    }
                ]
            (node_root / "interfaces").mkdir()
            (node_root / "interfaces/value.md").write_text(
                "# Value\n\nThe library SHALL expose a stable value.\n"
            )
            path.write_bytes(render_authoring_markdown(document, body))
        ProjectConfigurationStore(self.root).create(
            _definition(component_roots=("components",), routing_roots=("routing",))
        )
        parent = RepositoryParentSelection.root()
        FilesystemRepositoryLineageStore(self.root).replace(
            parent, RepositoryLineage(parent, (), ()), expected_absent=True
        )
        _write_lock(self.component, self.flavors)
        self.authority = read_current_authored_qualification_authority(
            self.root,
            GenerationPreparationRequest(
                self.component, _TARGET, _SELECTORS, (self.flavors,)
            ),
        )
        self.oracle_root = self.root / "litai/verification/acceptance/examples"
        self.oracle_root.mkdir(parents=True)
        harness = (
            b"raise RuntimeError('planning must not execute the private harness')\n"
        )
        (self.oracle_root / "harness.py").write_bytes(harness)
        selections = []
        for node in sorted(
            self.authority.locked.authority.lock.nodes,
            key=lambda n: n.revision.coordinate.uri,
        ):
            coordinate = node.revision.coordinate
            interface = node.interface_bindings[0]
            surface = LibraryImportSurface(
                "python",
                coordinate.name,
                (
                    LibraryCapabilityImport(
                        interface.capability,
                        interface.interface_identity,
                        coordinate.name + ".value",
                        ("value",),
                    ),
                ),
            )
            record = {
                "schema": "literate-ai/library-acceptance-oracle@1",
                "component": coordinate.name,
                "specification_set_identity": (
                    node.revision.specification_set_identity.uri
                ),
                "public_interface_identities": sorted(
                    item.identity.uri for item in node.revision.public_interfaces
                ),
                "import_surface_identity": surface.identity.uri,
                "language": "python",
                "harness": "harness.py",
                "harness_identity": "sha256:" + hashlib.sha256(harness).hexdigest(),
                "cases": [
                    {
                        "case_id": "identity",
                        "capability": interface.capability,
                        "arguments": [1],
                        "expected_result": {"private": PRIVATE},
                    }
                ],
            }
            path = self.oracle_root / (coordinate.name + ".json")
            path.write_text(json.dumps(record))
            oracle = load_versioned_library_acceptance(path, coordinate.name)
            selections.append(
                AuthoredQualificationOracleSelection(coordinate, oracle.identity)
            )
        self.profile = AuthoredQualificationProfile(
            "authored-graph@1", tuple(selections)
        )
        self.profile_path = self.root / "litai/qualification.json"
        self.write_profile()

    def write_profile(self, profile=None):
        self.profile_path.write_text(json.dumps((profile or self.profile).to_dict()))

    def read(self):
        return read_current_authored_qualification_oracles(self.authority)

    def test_all_nodes_are_bound_read_only_and_answers_are_not_in_profile_or_map(self):
        def files():
            return {
                p.relative_to(self.root).as_posix(): p.read_bytes()
                for p in self.root.rglob("*")
                if p.is_file()
            }

        before = files()
        current = self.read()
        current.require_unchanged()
        self.assertEqual(before, files())
        self.assertEqual(len(current.bindings), 2)
        self.assertEqual(
            {b.component_revision_identity for b in current.bindings},
            {n.revision.identity for n in self.authority.locked.authority.lock.nodes},
        )
        self.assertNotEqual(
            current.bindings[0].case_map.identity, current.bindings[1].case_map.identity
        )
        self.assertNotIn(PRIVATE, repr(current))
        self.assertNotIn(PRIVATE, json.dumps(self.profile.to_dict()))
        for binding in current.bindings:
            self.assertNotIn(PRIVATE, json.dumps(binding.case_map.to_dict()))
        self.assertEqual(current.identity, self.read().identity)

    def test_root_only_or_extra_oracle_selection_refuses(self):
        self.write_profile(replace(self.profile, oracles=self.profile.oracles[:1]))
        with self.assertRaises(AuthoredQualificationOracleError) as refused:
            self.read()
        self.assertEqual(
            refused.exception.code, "qualification.authored.oracle_membership_mismatch"
        )

    def test_profile_or_harness_byte_drift_invalidates_the_saved_owner(self):
        for path in (self.profile_path, self.oracle_root / "harness.py"):
            with self.subTest(path=path.name):
                original = path.read_bytes()
                current = self.read()
                path.write_bytes(original + b"\n")
                with self.assertRaises(PinnedInputClosureError):
                    current.require_unchanged()
                path.write_bytes(original)

    def test_mutating_decoded_expected_results_does_not_replace_the_sealed_oracle(self):
        current = self.read()
        current.bindings[0].oracle.cases[0].expected_result["private"] = "substitute"
        with self.assertRaises(AuthoredQualificationOracleError) as refused:
            current.require_unchanged()
        self.assertEqual(
            refused.exception.code, "qualification.authored.oracle_changed"
        )

    def test_changed_oracle_and_profile_require_a_different_plan_identity(self):
        current = self.read()
        identity = current.identity
        path = self.oracle_root / "greeting.json"
        document = json.loads(path.read_bytes())
        document["cases"][0]["expected_result"] = {"private": "changed intent"}
        path.write_text(json.dumps(document))
        oracle = load_versioned_library_acceptance(path, "greeting")
        selected = replace(self.profile.oracles[0], oracle_identity=oracle.identity)
        self.write_profile(
            replace(self.profile, oracles=(selected, self.profile.oracles[1]))
        )
        with self.assertRaises(PinnedInputClosureError):
            current.require_unchanged()
        self.assertNotEqual(identity, self.read().identity)

    def test_duplicate_profile_or_oracle_keys_cannot_choose_private_answers(self):
        for path, prefix in (
            (self.profile_path, b'{"profile_id":"invented",'),
            (self.oracle_root / "greeting.json", b'{"component":"invented",'),
        ):
            with self.subTest(path=path.name):
                original = path.read_bytes()
                path.write_bytes(prefix + original[1:])
                with self.assertRaisesRegex(ValueError, "duplicate_authority_field"):
                    self.read()
                path.write_bytes(original)

    def test_changed_expected_answers_or_import_surface_refuse_new_admission(self):
        path = self.oracle_root / "greeting.json"
        original = path.read_bytes()
        for field, value in (
            (
                "cases",
                [
                    {
                        "case_id": "identity",
                        "capability": "greeting.value",
                        "arguments": [1],
                        "expected_result": 7,
                    }
                ],
            ),
            ("import_surface_identity", "sha256:" + "0" * 64),
        ):
            with self.subTest(field=field):
                document = json.loads(original)
                document[field] = value
                path.write_text(json.dumps(document))
                with self.assertRaises(AuthoredQualificationOracleError) as refused:
                    self.read()
                self.assertEqual(
                    refused.exception.code,
                    "qualification.authored.oracle_authority_mismatch",
                )
        path.write_bytes(original)

    def test_unknown_or_uncovered_capability_cannot_claim_independent_coverage(self):
        path = self.oracle_root / "greeting.json"
        document = json.loads(path.read_bytes())
        document["cases"][0]["capability"] = "undeclared.value"
        path.write_text(json.dumps(document))
        oracle = load_versioned_library_acceptance(path, "greeting")
        changed = replace(self.profile.oracles[0], oracle_identity=oracle.identity)
        self.write_profile(
            replace(self.profile, oracles=(changed, self.profile.oracles[1]))
        )
        with self.assertRaises(AuthoredQualificationOracleError) as refused:
            self.read()
        self.assertEqual(
            refused.exception.code, "qualification.authored.oracle_coverage_incomplete"
        )

    def test_profile_must_be_under_litai_and_origin_dto_is_not_a_current_owner(self):
        with self.assertRaises(AuthoredQualificationOracleError):
            read_current_authored_qualification_oracles(
                self.authority, "qualification.json"
            )
        with self.assertRaises(TypeError):
            read_current_authored_qualification_oracles(self.authority.origin)

    def test_bounded_profile_reads_refuse_before_loading_or_running_a_harness(self):
        before = self.profile_path.read_bytes()
        with self.assertRaises(PinnedInputClosureError):
            read_current_authored_qualification_oracles(
                self.authority, maximum_file_bytes=32
            )
        self.assertEqual(before, self.profile_path.read_bytes())

    def test_profile_is_closed_and_requires_two_real_clean_runs(self):
        from jsonschema import Draft202012Validator
        from referencing import Registry, Resource

        self.assertEqual(
            AuthoredQualificationProfile.from_dict(self.profile.to_dict()), self.profile
        )
        for count in (0, 1, True, "2", 2.0):
            with self.subTest(count=count):
                with self.assertRaises(ContractValidationError):
                    replace(self.profile, minimum_clean_runs=count)
        for field in (
            "source_command",
            "source_snapshot_identity",
            "promotion_tree_identity",
            "authorized",
        ):
            with self.assertRaises(ContractValidationError):
                AuthoredQualificationProfile.from_dict(
                    {**self.profile.to_dict(), field: "invented"}
                )
        with self.assertRaises(ContractValidationError):
            replace(self.profile, oracles=tuple(reversed(self.profile.oracles)))
        with self.assertRaises(ContractValidationError):
            replace(
                self.profile, oracles=(self.profile.oracles[0], self.profile.oracles[0])
            )
        root = Path(__file__).parents[2] / "schemas"
        verify_schema_catalog("v2", root / "v2")
        profile_schema = json.loads(
            (root / "v2/authored-qualification-profile.schema.json").read_bytes()
        )
        identity_schema = json.loads((root / "v1/identity.schema.json").read_bytes())
        registry = Registry().with_resources(
            (document["$id"], Resource.from_contents(document))
            for document in (profile_schema, identity_schema)
        )
        validator = Draft202012Validator(profile_schema, registry=registry)
        validator.validate(self.profile.to_dict())
        for count in (0, 1, True, "2"):
            self.assertTrue(
                list(
                    validator.iter_errors(
                        {**self.profile.to_dict(), "minimum_clean_runs": count}
                    )
                )
            )
        self.assertTrue(
            list(
                validator.iter_errors(
                    {
                        **self.profile.to_dict(),
                        "source_command": ["fabricated-baseline"],
                    }
                )
            )
        )
