"""CLI migration from legacy Component JSON to readable component.md."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import literate_ai.cli.component_authoring as component_authoring_cli
from literate_ai.adapters.component_markdown import (
    parse_component_markdown,
    render_component_markdown,
)
from literate_ai.application.component_authoring_migration import (
    project_component_authoring,
)
from literate_ai.cli import main
from literate_ai.contracts.component_locking import ComponentContentSelector
from tests.support.fixtures_test_component_authoring_migration import (
    definition,
    pretty_repository_bytes,
    raw_identity,
    repository_dependency,
)

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def invoke(*arguments: str) -> tuple[int, dict[str, object]]:
    output = io.StringIO()
    errors = io.StringIO()
    status = main(arguments, stdout=output, stderr=errors)
    payload = output.getvalue() if status in {0, 1} else errors.getvalue()
    return status, json.loads(payload)


def fixture(
    root: Path, *, name: str = "migration-fixture"
) -> tuple[Path, object, object]:
    component = root / name
    component.mkdir()
    dependency = replace(repository_dependency(), integration_contract=None)
    dependencies = component / "dependencies"
    dependencies.mkdir()
    dependency_bytes = pretty_repository_bytes(dependency)
    (dependencies / "portable-json.json").write_bytes(dependency_bytes)
    legacy = definition()
    legacy = replace(legacy, coordinate=replace(legacy.coordinate, name=name))
    pinned_inputs = []
    for index, reference in enumerate(legacy.authoring_inputs):
        content = f"skill-{index}\n".encode()
        path = component.joinpath(*Path(reference.uri).parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        pinned_inputs.append(replace(reference, identity=raw_identity(content)))
    workflow_content = b"workflow\n"
    workflow_path = component.joinpath(*Path(legacy.workflow_definition.uri).parts)
    workflow_path.parent.mkdir(parents=True, exist_ok=True)
    workflow_path.write_bytes(workflow_content)
    routing_content = b"routing\n"
    routing_path = component.joinpath(*Path(legacy.routing_policy.uri).parts)
    routing_path.parent.mkdir(parents=True, exist_ok=True)
    routing_path.write_bytes(routing_content)
    acceptance_content = b"acceptance\n"
    acceptance_path = component.joinpath(
        *Path(legacy.acceptance_contracts[0].uri).parts
    )
    acceptance_path.parent.mkdir(parents=True, exist_ok=True)
    acceptance_path.write_bytes(acceptance_content)
    for specification in legacy.specification_roots:
        path = component.joinpath(*Path(specification).parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {path.stem}\n", encoding="utf-8")
    legacy = replace(
        legacy,
        authoring_inputs=tuple(pinned_inputs),
        workflow_definition=replace(
            legacy.workflow_definition, identity=raw_identity(workflow_content)
        ),
        routing_policy=replace(
            legacy.routing_policy, identity=raw_identity(routing_content)
        ),
        acceptance_contracts=(
            replace(
                legacy.acceptance_contracts[0],
                identity=raw_identity(acceptance_content),
            ),
        ),
        source_dependencies=(
            replace(
                legacy.source_dependencies[0],
                identity=raw_identity(dependency_bytes),
            ),
        ),
    )
    (component / "component.json").write_text(
        json.dumps(legacy.to_dict(), indent=2) + "\n",
        encoding="utf-8",
    )
    return component, legacy, dependency


def project_fixture(root: Path) -> tuple[Path, ...]:
    components = root / "components"
    components.mkdir()
    roots = tuple(fixture(components, name=name)[0] for name in ("alpha", "beta"))
    for catalog in ("flavors", "skills", "workflows", "routing", "docs"):
        (root / catalog).mkdir(exist_ok=True)
    (root / "literate.project.json").write_text(
        json.dumps(
            {
                "schema": "urn:literate-ai:schema:v2:project-definition",
                "project_id": "component-migration-test",
                "version": "1.0.0",
                "profile": "canonical",
                "agent_skill": "SKILL.md",
                "component_roots": ["components"],
                "flavor_roots": ["flavors"],
                "skill_roots": ["skills"],
                "workflow_roots": ["workflows"],
                "routing_roots": ["routing"],
                "documentation_roots": ["docs"],
                "source_intelligence": {
                    "schema": (
                        "urn:literate-ai:schema:v1:project-source-intelligence-policy"
                    ),
                    "provider_id": "none",
                    "command": None,
                    "minimum_version": None,
                    "artifact_path": None,
                    "stages": {
                        "project-maintenance": "off",
                        "source-generation": "off",
                        "cache-consumption": "off",
                        "source-to-specification": "off",
                        "repository-source-admission": "off",
                        "structural-review": "off",
                    },
                    "artifact_publication": "metadata-only",
                },
            }
        ),
        encoding="utf-8",
    )
    return roots


def interface_fixture(root: Path, *, pin: bool = False) -> tuple[Path, Path, object]:
    component, legacy, _dependency = fixture(root)
    status, envelope = invoke("component", "migrate", str(component))
    if status != 0:
        raise AssertionError(envelope)
    authoring_path = component / "component.md"
    authoring = parse_component_markdown(
        authoring_path,
        authoring_path.read_text(encoding="utf-8"),
        project_root=root,
    )
    interface = component / "interfaces" / "alpha.md"
    interface.parent.mkdir()
    content = b"# Alpha public interface\n"
    interface.write_bytes(content)
    selector = ComponentContentSelector(
        "public-interface-contract",
        "interfaces/alpha.md",
        raw_identity(content) if pin else None,
    )
    legacy = replace(
        legacy,
        provides=tuple(
            replace(item, contract=raw_identity(content))
            if item.name == "alpha"
            else item
            for item in legacy.provides
        ),
    )
    (component / "component.json").write_text(
        json.dumps(legacy.to_dict(), indent=2) + "\n", encoding="utf-8"
    )
    authoring = replace(
        authoring,
        provides=tuple(
            replace(item, interface=selector) if item.name == "alpha" else item
            for item in authoring.provides
        ),
    )
    authoring_path.write_text(
        render_component_markdown(authoring, authoring_path, project_root=root),
        encoding="utf-8",
    )
    return component, interface, authoring


class ComponentAuthoringCliTests(unittest.TestCase):
    def test_repository_samples_use_component_markdown_only(self) -> None:
        samples = _REPOSITORY_ROOT / "samples"
        authored = tuple(sorted(samples.rglob("component.md")))

        self.assertTrue(authored)
        self.assertEqual(tuple(samples.rglob("component.json")), ())
        self.assertTrue(all(path.is_file() for path in authored))

    def test_authored_capability_interface_round_trips_into_legacy_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            component, _interface, _authoring = interface_fixture(
                Path(temporary).resolve(), pin=True
            )

            status, envelope = invoke("component", "migrate", str(component), "--check")

        self.assertEqual(status, 0, envelope)
        self.assertEqual(envelope["result"]["state"], "current")
        self.assertEqual(
            envelope["result"]["compatibility_exit"]["removed_in"],
            "0.3.0",
        )

    def test_missing_authored_capability_interface_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            component, interface, _authoring = interface_fixture(
                Path(temporary).resolve()
            )
            interface.unlink()

            status, envelope = invoke("component", "migrate", str(component), "--check")

        self.assertEqual(status, 2)
        self.assertEqual(
            envelope["error"]["code"],
            "component_migration.inputs.closure_unavailable",
        )

    def test_tampered_pinned_capability_interface_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            component, interface, _authoring = interface_fixture(
                Path(temporary).resolve(), pin=True
            )
            interface.write_text("# Changed interface\n", encoding="utf-8")

            status, envelope = invoke("component", "migrate", str(component), "--check")

        self.assertEqual(status, 2)
        self.assertEqual(
            envelope["error"]["code"],
            "component_migration.inputs.closure_identity_changed",
        )

    def test_out_of_root_capability_interface_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            component, _interface, _authoring = interface_fixture(
                Path(temporary).resolve()
            )
            authoring_path = component / "component.md"
            authoring_path.write_text(
                authoring_path.read_text(encoding="utf-8").replace(
                    "uri: interfaces/alpha.md", "uri: ../outside.md"
                ),
                encoding="utf-8",
            )

            status, envelope = invoke("component", "migrate", str(component), "--check")

        self.assertEqual(status, 2)
        self.assertTrue(envelope["error"]["code"].startswith("component_migration."))

    def test_ambiguous_capability_interface_binding_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            component, _interface, _authoring = interface_fixture(
                Path(temporary).resolve()
            )
            authoring_path = component / "component.md"
            text = authoring_path.read_text(encoding="utf-8")
            authoring_path.write_text(
                text.replace(
                    "name: alpha\n    version: 1.0.0",
                    "name: alpha\n    version: 1.1.0",
                ),
                encoding="utf-8",
            )

            status, envelope = invoke("component", "migrate", str(component), "--check")

        self.assertEqual(status, 2)
        self.assertEqual(
            envelope["error"]["code"],
            "component_migration.capability-interface-ambiguous",
        )

    def test_project_migration_is_atomic_and_check_is_nonmutating(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            components = project_fixture(project)
            legacy = {
                component: (component / "component.json").read_bytes()
                for component in components
            }

            status, envelope = invoke("component", "migrate", str(project), "--check")
            self.assertEqual(status, 1, envelope)
            self.assertEqual(
                [item["state"] for item in envelope["result"]["components"]],
                ["missing", "missing"],
            )
            self.assertFalse((project / ".component.migration-set.write.lock").exists())

            status, envelope = invoke("component", "migrate", str(project))
            self.assertEqual(status, 0, envelope)
            self.assertEqual(envelope["result"]["updated_count"], 2)
            for component in components:
                self.assertTrue((component / "component.md").is_file())
                self.assertEqual(
                    (component / "component.json").read_bytes(), legacy[component]
                )

            status, envelope = invoke("component", "migrate", str(project), "--check")
            self.assertEqual(status, 0, envelope)
            self.assertTrue(envelope["result"]["current"])

    def test_project_migration_rolls_back_created_documents_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            components = project_fixture(project)
            legacy = {
                component: (component / "component.json").read_bytes()
                for component in components
            }
            publish = component_authoring_cli._publish_new
            calls = 0

            def fail_second(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise component_authoring_cli.ComponentAuthoringMigrationError(
                        "injected-failure", "injected project publication failure"
                    )
                return publish(*args, **kwargs)

            with mock.patch.object(
                component_authoring_cli, "_publish_new", side_effect=fail_second
            ):
                status, envelope = invoke("component", "migrate", str(project))

            self.assertEqual(status, 2, envelope)
            self.assertEqual(
                envelope["error"]["code"],
                "component_migration.injected-failure",
            )
            for component in components:
                self.assertFalse((component / "component.md").exists())
                self.assertEqual(
                    (component / "component.json").read_bytes(), legacy[component]
                )

    def test_check_migrate_and_idempotent_repeat_preserve_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()
            component, legacy, dependency = fixture(project)

            status, envelope = invoke("component", "migrate", str(component), "--check")
            self.assertEqual(status, 1)
            self.assertEqual(envelope["result"]["state"], "missing")
            self.assertFalse((component / "component.md").exists())
            self.assertFalse((component / ".component.migrate.write.lock").exists())

            status, envelope = invoke("component", "migrate", str(component))
            self.assertEqual(status, 0, envelope)
            result = envelope["result"]
            self.assertEqual(result["state"], "created")
            self.assertTrue(result["updated"])
            self.assertTrue(result["legacy_preserved"])
            self.assertTrue((component / "component.json").is_file())
            authored = parse_component_markdown(
                component / "component.md",
                (component / "component.md").read_text(encoding="utf-8"),
                project_root=project,
            )
            expected = project_component_authoring(
                legacy,
                repository_dependencies=(dependency,),
            )
            self.assertEqual(
                replace(
                    authored,
                    authoring_inputs=expected.authoring_inputs,
                    workflow_definition=expected.workflow_definition,
                    routing_policy=expected.routing_policy,
                ),
                expected,
            )
            self.assertTrue(
                all(
                    item.uri.startswith("migration-fixture/")
                    for item in authored.authoring_inputs
                )
            )

            first_bytes = (component / "component.md").read_bytes()
            equivalent_bytes = first_bytes.replace(
                b"namespace: samples\n",
                b"namespace: samples\nname: migration-fixture\n",
                1,
            )
            (component / "component.md").write_bytes(equivalent_bytes)
            status, envelope = invoke("component", "migrate", str(component))
            self.assertEqual(status, 0, envelope)
            self.assertEqual(envelope["result"]["state"], "current")
            self.assertFalse(envelope["result"]["updated"])
            self.assertTrue(envelope["result"]["semantic_match"])
            self.assertEqual(
                (component / "component.md").read_bytes(), equivalent_bytes
            )

    def test_migration_never_overwrites_authored_component(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            component, _legacy, _dependency = fixture(Path(temporary).resolve())
            authored = b"human-owned component prose\n"
            (component / "component.md").write_bytes(authored)

            status, envelope = invoke("component", "migrate", str(component))

            self.assertEqual(status, 1)
            self.assertEqual(envelope["result"]["state"], "conflict")
            self.assertFalse(envelope["result"]["updated"])
            self.assertEqual((component / "component.md").read_bytes(), authored)

    def test_repository_raw_identity_mismatch_is_a_stable_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            component, _legacy, _dependency = fixture(Path(temporary).resolve())
            dependency = component / "dependencies" / "portable-json.json"
            dependency.write_bytes(dependency.read_bytes() + b" ")

            status, envelope = invoke("component", "migrate", str(component))

            self.assertEqual(status, 2)
            self.assertEqual(
                envelope["error"]["code"],
                "component_migration.inputs.closure_identity_changed",
            )
            self.assertFalse((component / "component.md").exists())


if __name__ == "__main__":
    unittest.main()
