"""Canonical root authority excludes local state and preserves child boundaries."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

from jsonschema import Draft202012Validator, ValidationError
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from literate_ai.contracts.generation_cache import (
    SourceCacheConfiguration,
    SourceCacheMode,
    SourceCacheRootKind,
    SourceCacheTarget,
)
from literate_ai.contracts.projects import ProjectDefinition
from literate_ai.contracts.repository_orchestration import (
    RepositoryOrchestration,
    RepositoryPin,
    RepositoryRelationship,
)
from literate_ai.projects import (
    ProjectConfigurationStore,
    serialize_project_configuration,
)
from literate_ai.schema_catalog import verify_schema_catalog
from tests.support.fixtures_test_schema_catalog import SchemaCatalog

ROOT = Path(__file__).resolve().parents[2]


def authority():
    return RepositoryOrchestration(
        "sha256:" + "c" * 64,
        (
            RepositoryPin("app", "services/app", "../app.git", "a" * 40, "."),
            RepositoryPin(
                "lib", "libraries/core", "git@example.test:group/lib.git", "b" * 40
            ),
        ),
        (RepositoryRelationship("services/app", "libraries/core"),),
    )


def project():
    return ProjectDefinition.from_dict(
        json.loads((ROOT / "literate.project.json").read_text())
    )


class RepositoryOrchestrationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        schemas = SchemaCatalog()
        registry = Registry().with_resources(
            (uri, Resource.from_contents(value, default_specification=DRAFT202012))
            for uri, value in schemas.resources.items()
        )
        cls.validator = Draft202012Validator(
            {"$ref": ProjectDefinition.SCHEMA}, registry=registry
        )

    def test_default_project_wire_and_identity_are_unchanged(self):
        original = project()
        self.assertNotIn("repository_orchestration", original.to_dict())
        self.assertEqual(
            replace(original, repository_orchestration=None).identity, original.identity
        )

    def test_typed_project_and_schema_round_trip_persistent_authority(self):
        original = project()
        declared = replace(original, repository_orchestration=authority())
        self.validator.validate(declared.to_dict())
        self.assertEqual(ProjectDefinition.from_dict(declared.to_dict()), declared)
        self.assertNotEqual(original.identity, declared.identity)
        self.assertEqual(declared.repository_orchestration, authority())

    def test_records_are_immutable_and_serialized_arrays_do_not_alias(self):
        value = authority()
        with self.assertRaises(FrozenInstanceError):
            value.repositories[0].commit = "e" * 40
        wire = value.to_dict()
        wire["repositories"].clear()
        self.assertEqual(len(value.repositories), 2)

    def test_real_project_manifest_store_preserves_typed_binding(self):
        declared = replace(project(), repository_orchestration=authority())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            content = serialize_project_configuration(declared)
            (root / "literate.project.json").write_bytes(content)
            snapshot = ProjectConfigurationStore(root).read()
            self.assertEqual(snapshot.definition, declared)
            self.assertEqual(snapshot.content, content)

    def test_order_normalizes_but_pin_changes_change_authority(self):
        value = authority()
        reordered = replace(value, repositories=tuple(reversed(value.repositories)))
        self.assertEqual(value.identity, reordered.identity)
        changed = replace(
            value,
            repositories=(
                replace(value.repositories[0], commit="d" * 40),
                value.repositories[1],
            ),
        )
        self.assertNotEqual(value.identity, changed.identity)
        self.assertNotEqual(
            replace(project(), repository_orchestration=value).identity,
            replace(project(), repository_orchestration=changed).identity,
        )

    def test_pin_shapes_refuse_ambiguous_or_credential_bearing_inputs(self):
        pin = authority().repositories[0]
        for changes in (
            {"commit": "main"},
            {"commit": "0" * 40},
            {"commit": "A" * 40},
            {"name": ""},
            {"name": "a\nb"},
            {"name": "a" * 257},
            {"path": "../child"},
            {"path": "CON"},
            {"path": ".literate/child"},
            {"path": "SKILL.md"},
            {"url": "https://secret-value@example.test/a.git"},
            {"url": "ssh://git:secret-value@example.test/a.git"},
            {"branch": ""},
            {"branch": "bad..branch"},
            {"branch": "-option"},
            {"branch": "refs/a.lock"},
            {"branch": "branch name"},
        ):
            with (
                self.subTest(changes=changes),
                self.assertRaises((TypeError, ValueError)) as caught,
            ):
                replace(pin, **changes)
            self.assertNotIn("secret-value", str(caught.exception))

    def test_sha256_pin_and_configured_relative_branch_are_retained(self):
        pin = replace(authority().repositories[0], commit="e" * 64, branch=".")
        self.assertEqual(RepositoryPin.from_dict(pin.to_dict()), pin)

    def test_child_paths_are_unique_portable_and_nonoverlapping(self):
        value = authority()
        for path in (
            value.repositories[0].path,
            value.repositories[0].path.upper(),
            value.repositories[0].path + "/nested",
        ):
            with self.subTest(path=path), self.assertRaises(ValueError):
                replace(
                    value,
                    repositories=(
                        value.repositories[0],
                        replace(value.repositories[1], path=path),
                    ),
                )
        with self.assertRaises(ValueError):
            replace(
                value,
                repositories=(
                    value.repositories[0],
                    replace(value.repositories[1], name=value.repositories[0].name),
                ),
            )

    def test_relationships_require_known_distinct_endpoints_and_unique_edges(self):
        value = authority()
        for relationships in (
            value.relationships * 2,
            (RepositoryRelationship("services/app", "unknown"),),
            (RepositoryRelationship("services/app", "LIBRARIES/core"),),
        ):
            with (
                self.subTest(relationships=relationships),
                self.assertRaises(ValueError),
            ):
                replace(value, relationships=relationships)
        with self.assertRaises(ValueError):
            RepositoryRelationship("same", "same")

    def test_dependencies_do_not_imply_an_execution_order(self):
        value = authority()
        both = replace(
            value,
            relationships=(
                *value.relationships,
                RepositoryRelationship("libraries/core", "services/app"),
            ),
        )
        self.assertEqual(len(both.relationships), 2)

    def test_untyped_mutable_and_excessive_collections_refuse(self):
        value = authority()
        for changes in (
            {"repositories": []},
            {"repositories": ()},
            {"repositories": value.repositories * 65},
            {"relationships": []},
            {"relationships": value.relationships * 1025},
            {"gitmodules_identity": "not-an-identity"},
        ):
            with (
                self.subTest(changes=changes),
                self.assertRaises((ValueError, TypeError)),
            ):
                replace(value, **changes)
        with self.assertRaises((TypeError, ValueError)):
            replace(project(), repository_orchestration=value.to_dict())

    def test_root_catalog_and_output_overlap_refuses(self):
        base = replace(project(), repository_orchestration=authority())
        for field in (
            "component_roots",
            "flavor_roots",
            "skill_roots",
            "workflow_roots",
            "routing_roots",
            "documentation_roots",
            "mcp_roots",
        ):
            for path in ("services", "services/app", "SERVICES/app/docs"):
                with (
                    self.subTest(field=field, path=path),
                    self.assertRaises(ValueError),
                ):
                    replace(base, **{field: (path,)})
        for field in ("test_receipt", "log_dir"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                replace(base, **{field: "services/app/output"})
        cache = SourceCacheConfiguration(
            SourceCacheMode.READ_WRITE,
            (
                SourceCacheTarget(
                    "local", SourceCacheRootKind.PROJECT_RELATIVE, "services/app/cache"
                ),
            ),
            write_target_id="local",
        )
        with self.assertRaises(ValueError):
            replace(base, source_cache=cache)
        from tests.support.fixtures_test_html_emitter import request

        output = replace(request("literate-ai"), output_path="services/app/graph.html")
        with self.assertRaises(ValueError):
            replace(base, html_render_requests=(output,))

    def test_legacy_project_cannot_smuggle_new_authority(self):
        wire = replace(project(), repository_orchestration=authority()).to_dict()
        wire["schema"] = "urn:literate-ai:schema:v1:project-definition"
        with self.assertRaisesRegex(ValueError, "legacy project"):
            ProjectDefinition.from_dict(wire)

    def test_closed_schema_and_python_reject_local_observations_and_extra_fields(self):
        original = replace(project(), repository_orchestration=authority()).to_dict()
        for scope, key, value in (
            ("binding", "initialized", True),
            ("pin", "checked_out_commit", "a" * 40),
            ("pin", "state", "initialized"),
            ("edge", "execute", "command"),
            ("pin", "commit", "main"),
            ("binding", "gitmodules_identity", "invalid"),
        ):
            wire = copy.deepcopy(original)
            binding = wire["repository_orchestration"]
            target = (
                binding
                if scope == "binding"
                else binding["repositories" if scope == "pin" else "relationships"][0]
            )
            target[key] = value
            with self.subTest(scope=scope, key=key):
                with self.assertRaises((TypeError, ValueError)):
                    ProjectDefinition.from_dict(wire)
                with self.assertRaises(ValidationError):
                    self.validator.validate(wire)

    def test_current_schema_catalog_remains_valid(self):
        verify_schema_catalog("v2", ROOT / "schemas/v2")
