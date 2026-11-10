"""Real current project/lock admission stays separate from origin decoding."""

import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from literate_ai.adapters.authored_qualification_authority import (
    AuthoredQualificationAuthorityError,
    read_current_authored_qualification_authority,
)
from literate_ai.adapters.authority import (
    AuthorityProjectionStoreError,
    FileAuthorityProjectionStore,
)
from literate_ai.adapters.locked_generation_authority import (
    LockedGenerationAuthorityReaderError,
)
from literate_ai.adapters.repository_lineage import FilesystemRepositoryLineageStore
from literate_ai.application.generation_preparation import GenerationPreparationRequest
from literate_ai.authority import ComponentAuthorityLifecycle
from literate_ai.contracts import RepositoryLineage, RepositoryParentSelection
from literate_ai.contracts.authoring_markdown import (
    parse_authoring_markdown,
    render_authoring_markdown,
)
from literate_ai.contracts.identity import canonical_identity
from literate_ai.contracts.qualification_origins import qualification_origin_from_dict
from literate_ai.projects import PinnedInputClosureError, ProjectConfigurationStore
from tests.support.fixtures_test_component_lock_planning import _fixture
from tests.support.fixtures_test_locked_generation_authority import (
    _SELECTORS,
    _TARGET,
    _write_lock,
)
from tests.support.fixtures_test_project_agent_development_workflow import _definition


class AuthoredQualificationAuthorityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.component, self.flavors = _fixture(self.root)
        ProjectConfigurationStore(self.root).create(
            _definition(component_roots=("greeting",), routing_roots=("routing",))
        )
        parent = RepositoryParentSelection.root()
        FilesystemRepositoryLineageStore(self.root).replace(
            parent, RepositoryLineage(parent, (), ()), expected_absent=True
        )
        _write_lock(self.component, self.flavors)
        self.request = GenerationPreparationRequest(
            self.component, _TARGET, _SELECTORS, (self.flavors,)
        )

    def read(self):
        return read_current_authored_qualification_authority(self.root, self.request)

    def projection(self, coordinate):
        return ComponentAuthorityLifecycle.inventory(
            component_coordinate=coordinate,
            source_snapshot_identity=canonical_identity({"source": "inventory"}),
            provenance_reference_identity=canonical_identity({"source": "provenance"}),
            evidence_identities=(canonical_identity({"source": "evidence"}),),
        )

    def test_current_owner_is_read_only_and_binds_the_real_graph(self):
        def files():
            return {
                path.relative_to(self.root).as_posix(): path.read_bytes()
                for path in self.root.rglob("*")
                if path.is_file()
            }

        before = files()
        current = self.read()
        current.require_unchanged()
        self.assertEqual(before, files())
        self.assertEqual(
            current.origin.component_lock_identity,
            current.locked.authority.lock.identity,
        )
        self.assertEqual(
            current.origin.locked_generation_authority_identity,
            current.locked.authority.identity,
        )
        self.assertEqual(
            current.origin.project_configuration_identity,
            ProjectConfigurationStore(self.root).read().content_identity,
        )
        reference = qualification_origin_from_dict(current.origin.to_dict())
        self.assertEqual(reference, current.origin)
        with self.assertRaises(TypeError):
            read_current_authored_qualification_authority(self.root, reference)

    def test_configuration_bytes_cannot_change_under_a_saved_owner(self):
        current = self.read()
        saved = current.origin
        path = self.root / "literate.project.json"
        path.write_bytes(path.read_bytes() + b"\n")
        with self.assertRaises(PinnedInputClosureError):
            current.require_unchanged()
        self.assertNotEqual(saved.identity, self.read().origin.identity)

    def test_selected_specification_drift_refuses_existing_and_new_admission(self):
        current = self.read()
        path = self.component / "specs/spec.md"
        path.write_bytes(path.read_bytes() + b"\nChanged reviewed intent.\n")
        with self.assertRaises(
            (PinnedInputClosureError, LockedGenerationAuthorityReaderError)
        ):
            current.require_unchanged()
        with self.assertRaises(LockedGenerationAuthorityReaderError):
            self.read()

    def test_source_authority_cannot_be_bypassed_by_authored_selection(self):
        current = self.read()
        coordinate = current.locked.authority.root_authoring.coordinate.uri
        FileAuthorityProjectionStore(self.root).append(self.projection(coordinate))
        for operation in (current.require_unchanged, self.read):
            with self.assertRaises(AuthoredQualificationAuthorityError) as refused:
                operation()
            self.assertEqual(
                refused.exception.code,
                "qualification.authored.source_authority_present",
            )

    def test_unrelated_projection_is_not_part_of_the_selected_authored_graph(self):
        current = self.read()
        FileAuthorityProjectionStore(self.root).append(
            self.projection("component://unselected/retained")
        )
        current.require_unchanged()
        self.assertEqual(current.origin, self.read().origin)

    def test_source_authority_on_a_dependency_also_refuses_authored_admission(self):
        catalog = self.root / "components"
        catalog.mkdir()
        self.component = Path(shutil.move(self.component, catalog / "greeting"))
        self.request = replace(self.request, component_root=self.component)
        provider = catalog / "provider"
        shutil.copytree(
            self.component,
            provider,
            ignore=shutil.ignore_patterns(
                "component.lock.json", "component-resolution*"
            ),
        )
        provider_path = provider / "component.md"
        provider_document, provider_body = parse_authoring_markdown(
            provider_path.read_bytes(), source=provider_path.as_posix()
        )
        provider_document["provides"].append(
            {
                "name": "sample.provider",
                "version": "1.0.0",
                "interface": {"uri": "interfaces/provider.md", "pin": None},
            }
        )
        (provider / "interfaces").mkdir()
        (provider / "interfaces/provider.md").write_text(
            "# Provider interface\n\nThe provider SHALL expose a greeting.\n"
        )
        provider_path.write_bytes(
            render_authoring_markdown(provider_document, provider_body)
        )
        root_path = self.component / "component.md"
        document, body = parse_authoring_markdown(
            root_path.read_bytes(), source=root_path.as_posix()
        )
        document["requires"] = [
            {
                "requirement_id": "provider",
                "capability": "sample.provider",
                "version_range": ">=1,<2",
                "dependency_kind": "generation",
                "optional": False,
                "constraints": [],
            }
        ]
        root_path.write_bytes(render_authoring_markdown(document, body))
        store = ProjectConfigurationStore(self.root)
        configuration = store.read()
        store.update(
            configuration,
            replace(configuration.definition, component_roots=("components",)),
        )
        _write_lock(self.component, self.flavors)
        current = self.read()
        self.assertEqual(len(current.locked.authority.lock.nodes), 2)
        dependency = next(
            node
            for node in current.locked.authority.lock.nodes
            if node.revision.identity != current.locked.authority.lock.root_revision
        )
        FileAuthorityProjectionStore(self.root).append(
            self.projection(dependency.revision.coordinate.uri)
        )
        for operation in (current.require_unchanged, self.read):
            with self.assertRaises(AuthoredQualificationAuthorityError) as refused:
                operation()
            self.assertEqual(
                refused.exception.code,
                "qualification.authored.source_authority_present",
            )

    def test_corrupt_source_authority_is_not_reinterpreted_as_absence(self):
        current = self.read()
        base = self.root / "provenance/component-authority"
        base.mkdir(parents=True)
        (base / ("a" * 64)).mkdir()
        for operation in (current.require_unchanged, self.read):
            with self.assertRaises(AuthorityProjectionStoreError):
                operation()

    def test_new_nested_project_changes_the_component_owner(self):
        current = self.read()
        ProjectConfigurationStore(self.component).create(
            _definition(project_id="different-owner")
        )
        for operation in (current.require_unchanged, self.read):
            with self.assertRaises(AuthoredQualificationAuthorityError):
                operation()

    def test_unselected_authoring_location_cannot_come_from_a_caller_hash(self):
        current = self.read()
        with self.assertRaises(LockedGenerationAuthorityReaderError) as refused:
            current.locked.component_root(canonical_identity({"invented": "authoring"}))
        self.assertEqual(refused.exception.code, "component_lock.component_unselected")
